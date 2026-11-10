"""Load-generating client.

Sends `count` messages of `size` bytes over TCP or UDP at a fixed rate
(messages/second, 0 = as fast as possible), then asks the server over the
control channel what actually arrived.
"""

import json
import random
import socket
import time

from . import protocol


def ctrl_request(host, port, req, timeout=10.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        f = s.makefile("rb")
        line = f.readline()
    if not line:
        raise ConnectionError("control channel closed")
    return json.loads(line)


def wait_for_server(host, port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if ctrl_request(host, port, {"cmd": "ping"}, timeout=1.0).get("ok"):
                return True
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server control port {host}:{port} not reachable")


def _paced(count, rate):
    """Yield seq numbers, sleeping so that message i leaves at start + i/rate."""
    if rate <= 0:
        yield from range(count)
        return
    interval = 1.0 / rate
    start = time.perf_counter()
    for i in range(count):
        target = start + i * interval
        while True:
            d = target - time.perf_counter()
            if d <= 0:
                break
            time.sleep(d - 0.0005 if d > 0.002 else 0)
        yield i


def run_test(proto, host, port, ctrl_host, ctrl_port, count=1000, size=1024, rate=500,
             settle=0.6, timeout=120.0, session=None):
    """Run one test. Returns a dict with everything metrics.compute() needs."""
    if not 1 <= size <= protocol.MAX_PAYLOAD:
        raise ValueError(f"size must be 1..{protocol.MAX_PAYLOAD}")
    session = session if session is not None else random.getrandbits(32)
    payload = protocol.make_payload(size)
    send_ts = [0.0] * count
    send_errors = 0

    t_start = time.time()
    if proto == "tcp":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for seq in _paced(count, rate):
            ts = time.time()
            send_ts[seq] = ts
            sock.sendall(protocol.encode(session, seq, ts, payload))
        t_sent = time.time()
        sock.shutdown(socket.SHUT_WR)
        # The server closes its side once it has read the whole stream.
        try:
            while sock.recv(4096):
                pass
        except socket.timeout:
            pass
        sock.close()
        res = _wait_results(ctrl_host, ctrl_port, session, need_complete=True,
                            settle=settle, timeout=timeout)
    elif proto == "udp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        addr = (socket.gethostbyname(host), port)
        for seq in _paced(count, rate):
            ts = time.time()
            send_ts[seq] = ts
            try:
                sock.sendto(protocol.encode(session, seq, ts, payload), addr)
            except OSError:
                send_errors += 1  # e.g. ENOBUFS: the local stack dropped it
        t_sent = time.time()
        sock.close()
        res = _wait_results(ctrl_host, ctrl_port, session, need_complete=False,
                            settle=settle, timeout=timeout)
    else:
        raise ValueError("proto must be 'tcp' or 'udp'")

    return {
        "proto": proto, "session": session, "count": count, "size": size, "rate": rate,
        "send_ts": send_ts, "records": res["records"], "send_errors": send_errors,
        "t_start": t_start, "t_sent": t_sent,
    }


def _wait_results(host, port, session, need_complete, settle, timeout):
    """Poll until the server's view is final, then fetch and drop the session.

    TCP: final when the server has seen the stream close.
    UDP: there is no 'end of stream', so wait until the received count has been
    stable for `settle` seconds (long enough for in-flight datagrams to land).
    """
    deadline = time.time() + timeout
    last, stable_since = -1, time.time()
    while time.time() < deadline:
        c = ctrl_request(host, port, {"cmd": "count", "session": session})
        if need_complete:
            if c["complete"]:
                break
        else:
            if c["count"] != last:
                last, stable_since = c["count"], time.time()
            elif time.time() - stable_since >= settle:
                break
        time.sleep(0.05)
    return ctrl_request(host, port, {"cmd": "results", "session": session, "pop": True}, timeout=30)
