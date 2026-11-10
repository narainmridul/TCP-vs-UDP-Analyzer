"""Userspace network link emulator (an impairment proxy).

    client ──► emulator :6001 (tcp+udp) ──► server :5001 (tcp+udp)

It applies the same link profile to both protocols: propagation delay,
jitter, random loss, corruption, duplication, a bandwidth cap and a bounded
router queue. What differs is how each protocol *reacts*, which is the point:

UDP  Each datagram is handled independently and the emulator does to it exactly
     what a real path would: drop it, corrupt it, copy it, delay it by its own
     random amount (so datagrams can overtake each other) or tail-drop it when
     the queue is full. Nothing is ever recovered.

TCP  The kernel's TCP stack runs end to end on both hops, but a userspace proxy
     cannot drop a byte from a TCP stream. So the emulator chops the stream
     into MSS-sized segments and, for each one it decides to "lose" (or
     corrupt: TCP's checksum turns corruption into loss), it applies what TCP
     would do about it, delaying that segment by a retransmission:
       * 1st loss of a segment: fast retransmit, +1 RTT
       * every further loss of the same segment: retransmission timeout,
         RTO = max(200 ms, RTT + 4*jitter), doubling each time (Linux min RTO
         is 200 ms)
     Segments are released strictly in order, so a stalled segment holds back
     everything behind it (head-of-line blocking), exactly as a TCP receiver
     would. Unacknowledged data is capped at `window` bytes and a slot frees
     only when the ACK would be back (one more one-way delay), so throughput
     is bounded by window / RTT and the sender is back-pressured through real
     TCP flow control.

     Not modelled: congestion-window reduction after loss. For real kernel
     behaviour on Linux, use the netem backend (see netem.py).
"""

import asyncio
import random
import socket
from collections import deque
from dataclasses import asdict, dataclass

MSS = 1448

# Shared counter slots (multiprocessing.Array or a plain list)
STAT_NAMES = [
    "udp_in", "udp_out", "udp_lost", "udp_queue_drop", "udp_corrupted", "udp_duplicated",
    "tcp_segments", "tcp_fast_retx", "tcp_rto",
]
STAT = {n: i for i, n in enumerate(STAT_NAMES)}


@dataclass
class LinkProfile:
    delay_ms: float = 0.0      # one-way propagation delay
    jitter_ms: float = 0.0     # std-dev of per-packet extra delay (normal, truncated at 0)
    loss_pct: float = 0.0      # random loss probability per packet/segment
    corrupt_pct: float = 0.0   # bit-flip probability per packet/segment
    duplicate_pct: float = 0.0 # UDP only: probability a datagram is delivered twice
    bandwidth_mbps: float = 0.0  # 0 = unlimited
    queue_kb: float = 64.0     # router buffer; UDP tail-drops when it is full
    window_kb: float = 64.0    # TCP: max bytes in flight through the link

    @property
    def rtt_s(self):
        return 2 * self.delay_ms / 1000.0

    @classmethod
    def parse(cls, text):
        """'delay=50,jitter=10,loss=2,bw=10' -> LinkProfile."""
        alias = {"delay": "delay_ms", "jitter": "jitter_ms", "loss": "loss_pct",
                 "corrupt": "corrupt_pct", "duplicate": "duplicate_pct", "dup": "duplicate_pct",
                 "bw": "bandwidth_mbps", "bandwidth": "bandwidth_mbps",
                 "queue": "queue_kb", "window": "window_kb"}
        kw = {}
        for part in filter(None, (p.strip() for p in (text or "").split(","))):
            key, _, val = part.partition("=")
            key = alias.get(key.strip(), key.strip())
            if key not in cls.__dataclass_fields__:
                raise ValueError(f"unknown link parameter {key!r}")
            kw[key] = float(val)
        return cls(**kw)

    def describe(self):
        bits = []
        if self.delay_ms: bits.append(f"delay {self.delay_ms:g} ms")
        if self.jitter_ms: bits.append(f"jitter ±{self.jitter_ms:g} ms")
        if self.loss_pct: bits.append(f"loss {self.loss_pct:g}%")
        if self.corrupt_pct: bits.append(f"corrupt {self.corrupt_pct:g}%")
        if self.duplicate_pct: bits.append(f"dup {self.duplicate_pct:g}%")
        if self.bandwidth_mbps: bits.append(f"{self.bandwidth_mbps:g} Mbps, {self.queue_kb:g} KB queue")
        return ", ".join(bits) or "no impairment"

    def to_dict(self):
        return asdict(self)


class Link:
    """One direction of an emulated link: serialization + propagation."""

    def __init__(self, profile, rng):
        self.p = profile
        self.rng = rng
        self.tx_free_at = 0.0  # when the 'wire' finishes sending what is queued

    def prop_delay(self):
        d = self.p.delay_ms
        if self.p.jitter_ms:
            d += self.rng.gauss(0.0, self.p.jitter_ms)
        return max(0.0, d) / 1000.0

    def enqueue(self, now, nbytes, may_drop):
        """Return the time the packet has fully left the router, or None if tail-dropped."""
        bw = self.p.bandwidth_mbps
        if bw <= 0:
            return now
        bytes_per_s = bw * 1e6 / 8
        start = max(now, self.tx_free_at)
        backlog = (start - now) * bytes_per_s
        if may_drop and backlog + nbytes > self.p.queue_kb * 1024:
            return None
        self.tx_free_at = start + nbytes / bytes_per_s
        return self.tx_free_at

    def hit(self, pct):
        return pct > 0 and self.rng.random() * 100.0 < pct


# --------------------------------------------------------------------- UDP

class UDPRelay(asyncio.DatagramProtocol):
    def __init__(self, upstream, profile, rng, stats):
        self.upstream = upstream
        self.link = Link(profile, rng)
        self.p = profile
        self.rng = rng
        self.stats = stats
        self.client = None
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass

    def datagram_received(self, data, addr):
        if addr == self.upstream:  # reply from server: pass straight back
            if self.client:
                self.transport.sendto(data, self.client)
            return
        self.client = addr
        st, link = self.stats, self.link
        st[STAT["udp_in"]] += 1
        if link.hit(self.p.loss_pct):
            st[STAT["udp_lost"]] += 1
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        left = link.enqueue(now, len(data), may_drop=True)
        if left is None:
            st[STAT["udp_queue_drop"]] += 1
            return
        if link.hit(self.p.corrupt_pct) and len(data) > 24:
            b = bytearray(data)
            i = self.rng.randrange(24, len(b))  # flip a payload bit, keep header readable
            b[i] ^= 1 << self.rng.randrange(8)
            data = bytes(b)
            st[STAT["udp_corrupted"]] += 1
        copies = 2 if link.hit(self.p.duplicate_pct) else 1
        if copies == 2:
            st[STAT["udp_duplicated"]] += 1
        for _ in range(copies):
            loop.call_at(left + link.prop_delay(), self._send, data)

    def _send(self, data):
        self.stats[STAT["udp_out"]] += 1
        self.transport.sendto(data, self.upstream)


# --------------------------------------------------------------------- TCP

class TCPRelay:
    def __init__(self, upstream, profile, rng, stats):
        self.upstream = upstream
        self.p = profile
        self.rng = rng
        self.stats = stats

    async def handle(self, c_reader, c_writer):
        try:
            s_reader, s_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            c_writer.close()
            return
        for w in (c_writer, s_writer):
            try:
                w.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        await asyncio.gather(self._impaired(c_reader, s_writer), self._plain(s_reader, c_writer),
                             return_exceptions=True)

    async def _plain(self, reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def _impaired(self, reader, writer):
        loop = asyncio.get_running_loop()
        link = Link(self.p, self.rng)
        window = max(int(self.p.window_kb * 1024), MSS)
        q = deque()
        state = {"inflight": 0, "eof": False, "last": 0.0}
        space, ready = asyncio.Event(), asyncio.Event()
        st = self.stats

        async def deliver():
            while True:
                while not q:
                    if state["eof"]:
                        return
                    ready.clear()
                    await ready.wait()
                t, data = q[0]
                wait = t - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                q.popleft()
                writer.write(data)
                await writer.drain()
                # the window slot frees when the ACK gets back to the sender: one more delay
                loop.call_later(self.p.delay_ms / 1000.0, release, len(data))

        def release(n):
            state["inflight"] -= n
            space.set()

        sender = asyncio.ensure_future(deliver())
        rto_base = max(0.2, self.p.rtt_s + 4 * self.p.jitter_ms / 1000.0)
        try:
            while True:
                while state["inflight"] >= window:
                    space.clear()
                    await space.wait()
                data = await reader.read(min(MSS, window - state["inflight"]))
                if not data:
                    break
                now = loop.time()
                st[STAT["tcp_segments"]] += 1
                left = link.enqueue(now, len(data), may_drop=False)
                arrive = left + link.prop_delay()
                # every lost or corrupted copy of this segment costs a retransmission
                attempt = 0
                while link.hit(self.p.loss_pct) or link.hit(self.p.corrupt_pct):
                    if attempt == 0:
                        st[STAT["tcp_fast_retx"]] += 1
                        arrive += max(self.p.rtt_s, 0.001)
                    else:
                        st[STAT["tcp_rto"]] += 1
                        arrive += rto_base * (2 ** (attempt - 1))
                    attempt += 1
                arrive = max(arrive, state["last"])  # in-order delivery: head-of-line blocking
                state["last"] = arrive
                q.append((arrive, data))
                state["inflight"] += len(data)
                ready.set()
        finally:
            state["eof"] = True
            ready.set()
            try:
                await sender
                if writer.can_write_eof():
                    writer.write_eof()
            except (ConnectionError, OSError):
                writer.close()


# --------------------------------------------------------------------- entry

async def serve(listen_host, listen_port, target_host, target_port, profile, seed=None,
                stats=None, ready=None, verbose=True):
    rng = random.Random(seed)
    stats = stats if stats is not None else [0] * len(STAT_NAMES)
    loop = asyncio.get_running_loop()
    upstream = (socket.gethostbyname(target_host), target_port)
    tcp = await asyncio.start_server(TCPRelay(upstream, profile, rng, stats).handle,
                                     listen_host, listen_port)
    udp, _ = await loop.create_datagram_endpoint(
        lambda: UDPRelay(upstream, profile, rng, stats), local_addr=(listen_host, listen_port))
    if verbose:
        print(f"[emulator] {listen_host}:{listen_port} -> {target_host}:{target_port}  "
              f"[{profile.describe()}]", flush=True)
    if ready is not None:
        ready.set()
    try:
        await tcp.serve_forever()
    finally:
        udp.close()
        if verbose:
            print("[emulator] " + ", ".join(f"{n}={stats[i]}" for i, n in enumerate(STAT_NAMES)),
                  flush=True)


def run_emulator(listen_host, listen_port, target_host, target_port, profile, seed=None,
                 stats=None, ready=None, verbose=True):
    try:
        asyncio.run(serve(listen_host, listen_port, target_host, target_port, profile,
                          seed, stats, ready, verbose))
    except KeyboardInterrupt:
        pass
