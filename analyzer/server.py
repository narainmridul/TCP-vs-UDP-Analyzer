"""Measurement server.

Listens on three ports:
  * TCP data port  - length-prefixed message stream
  * UDP data port  - one message per datagram
  * control port   - newline-delimited JSON, used by the client to fetch what
                     the server received (never passes through the emulator)

For every message the server records (seq, receive timestamp, intact?). The
client pulls those records over the control channel and computes metrics.
"""

import asyncio
import json
import socket
import time

from . import protocol


class Session:
    __slots__ = ("records", "complete", "proto")

    def __init__(self, proto):
        self.records = []      # [seq, recv_ts, ok] in arrival order
        self.complete = False  # TCP: sender closed the stream
        self.proto = proto


class Store:
    def __init__(self):
        self.sessions = {}

    def get(self, sid, proto):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(proto)
        return s


class UDPReceiver(asyncio.DatagramProtocol):
    def __init__(self, store):
        self.store = store

    def connection_made(self, transport):
        sock = transport.get_extra_info("socket")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass

    def datagram_received(self, data, addr):
        ts = time.time()
        hdr = protocol.decode_header(data)
        if hdr is None:
            return
        sid, seq, _send_ts, length, crc = hdr
        payload = data[protocol.HEADER_SIZE:]
        ok = protocol.payload_ok(payload, length, crc)
        self.store.get(sid, "udp").records.append([seq, ts, ok])


class Server:
    def __init__(self, host="0.0.0.0", tcp_port=5001, udp_port=5001, ctrl_port=5000, verbose=True):
        self.host, self.tcp_port, self.udp_port, self.ctrl_port = host, tcp_port, udp_port, ctrl_port
        self.verbose = verbose
        self.store = Store()

    def log(self, msg):
        if self.verbose:
            print(f"[server] {msg}", flush=True)

    async def handle_tcp(self, reader, writer):
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        session = None
        n = 0
        try:
            while True:
                try:
                    head = await reader.readexactly(protocol.HEADER_SIZE)
                except asyncio.IncompleteReadError:
                    break
                hdr = protocol.decode_header(head)
                if hdr is None:
                    self.log(f"bad frame from {peer}, closing")
                    break
                sid, seq, _send_ts, length, crc = hdr
                payload = await reader.readexactly(length)
                ts = time.time()
                if session is None:
                    session = self.store.get(sid, "tcp")
                session.records.append([seq, ts, protocol.payload_ok(payload, length, crc)])
                n += 1
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if session is not None:
                session.complete = True
            self.log(f"tcp stream from {peer} closed after {n} messages")
            writer.close()

    async def handle_ctrl(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except ValueError:
                    resp = {"error": "bad json"}
                else:
                    resp = self.control(req)
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def control(self, req):
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        sid = req.get("session")
        s = self.store.sessions.get(sid)
        if cmd == "count":
            return {"count": len(s.records) if s else 0, "complete": bool(s and s.complete)}
        if cmd == "results":
            if s is None:
                return {"records": [], "complete": False}
            if req.get("pop"):
                del self.store.sessions[sid]
                self.log(f"{s.proto} session {sid}: reported {len(s.records)} messages")
            return {"records": s.records, "complete": s.complete}
        return {"error": f"unknown cmd {cmd!r}"}

    async def serve(self, ready=None):
        loop = asyncio.get_running_loop()
        tcp = await asyncio.start_server(self.handle_tcp, self.host, self.tcp_port)
        ctrl = await asyncio.start_server(self.handle_ctrl, self.host, self.ctrl_port)
        udp, _ = await loop.create_datagram_endpoint(
            lambda: UDPReceiver(self.store), local_addr=(self.host, self.udp_port))
        self.log(f"listening tcp:{self.tcp_port} udp:{self.udp_port} control:{self.ctrl_port} on {self.host}")
        if ready is not None:
            ready.set()
        try:
            await asyncio.gather(tcp.serve_forever(), ctrl.serve_forever())
        finally:
            udp.close()


def run_server(host="0.0.0.0", tcp_port=5001, udp_port=5001, ctrl_port=5000, verbose=True, ready=None):
    try:
        asyncio.run(Server(host, tcp_port, udp_port, ctrl_port, verbose).serve(ready))
    except KeyboardInterrupt:
        pass
