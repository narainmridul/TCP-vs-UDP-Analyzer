import multiprocessing as mp
import random
import socket
import unittest

from analyzer import client, emulator, metrics, netem, protocol
from analyzer.emulator import LinkProfile
from analyzer.server import run_server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProtocolTest(unittest.TestCase):
    def test_roundtrip_and_corruption(self):
        payload = protocol.make_payload(100)
        msg = protocol.encode(7, 42, 1.5, payload)
        sid, seq, ts, length, crc = protocol.decode_header(msg)
        self.assertEqual((sid, seq, ts, length), (7, 42, 1.5, 100))
        body = msg[protocol.HEADER_SIZE:]
        self.assertTrue(protocol.payload_ok(body, length, crc))
        bad = bytearray(body); bad[5] ^= 1
        self.assertFalse(protocol.payload_ok(bytes(bad), length, crc))
        self.assertIsNone(protocol.decode_header(b"XX" + msg[2:]))


class MetricsTest(unittest.TestCase):
    def test_loss_dup_reorder_corrupt(self):
        run = {"proto": "udp", "count": 5, "size": 1000, "rate": 100,
               "send_ts": [0.0, 0.01, 0.02, 0.03, 0.04],
               # seq 1 lost, 3 arrives before 2, 0 duplicated, 4 corrupted
               "records": [[0, 0.010, True], [3, 0.045, True], [2, 0.050, True],
                           [0, 0.060, True], [4, 0.070, False]]}
        m = metrics.compute(run)
        self.assertEqual(m["delivered"], 3)
        self.assertEqual(m["lost"], 2)
        self.assertEqual(m["duplicates"], 1)
        self.assertEqual(m["corrupted"], 1)
        self.assertEqual(m["out_of_order"], 1)
        self.assertEqual(m["reliability_pct"], 40.0)
        self.assertAlmostEqual(m["latency_min_ms"], 10.0)
        self.assertAlmostEqual(m["latency_max_ms"], 30.0)


class ProfileTest(unittest.TestCase):
    def test_parse(self):
        p = LinkProfile.parse("delay=50,jitter=5,loss=2,bw=10,dup=1")
        self.assertEqual((p.delay_ms, p.jitter_ms, p.loss_pct, p.bandwidth_mbps, p.duplicate_pct),
                         (50, 5, 2, 10, 1))
        with self.assertRaises(ValueError):
            LinkProfile.parse("speed=3")

    def test_netem_args(self):
        a = netem.tc_args(LinkProfile(delay_ms=20, jitter_ms=5, loss_pct=1, bandwidth_mbps=2))
        self.assertEqual(a[:7], ["tc", "qdisc", "replace", "dev", "lo", "root", "netem"])
        for tok in ("20ms", "5ms", "1%", "2mbit"):
            self.assertIn(tok, a)

    def test_queue_tail_drop(self):
        link = emulator.Link(LinkProfile(bandwidth_mbps=1, queue_kb=4), random.Random(0))
        accepted = sum(link.enqueue(0.0, 1024, may_drop=True) is not None for _ in range(10))
        self.assertEqual(accepted, 4)


class EndToEndTest(unittest.TestCase):
    """Real sockets: server + emulator processes, both protocols."""

    def run_through(self, profile, proto, count=200):
        ctrl, data, emu = free_port(), free_port(), free_port()
        procs = []
        for target, args, kw in ((run_server, ("127.0.0.1", data, data, ctrl), {"verbose": False}),
                                 (emulator.run_emulator, ("127.0.0.1", emu, "127.0.0.1", data, profile),
                                  {"seed": 3, "verbose": False})):
            ev = mp.Event()
            p = mp.Process(target=target, args=args, kwargs={**kw, "ready": ev}, daemon=True)
            p.start(); ev.wait(10); procs.append(p)
        try:
            client.wait_for_server("127.0.0.1", ctrl)
            return metrics.compute(client.run_test(proto, "127.0.0.1", emu, "127.0.0.1", ctrl,
                                                   count=count, size=512, rate=1000, settle=0.4))
        finally:
            for p in procs:
                p.terminate(); p.join(5)

    def test_tcp_is_reliable_under_loss(self):
        m = self.run_through(LinkProfile(delay_ms=5, loss_pct=10), "tcp")
        self.assertEqual(m["delivery_pct"], 100.0)
        self.assertEqual(m["out_of_order"], 0)
        self.assertGreater(m["latency_p95_ms"], 10)  # retransmissions cost time

    def test_udp_loses_under_loss(self):
        m = self.run_through(LinkProfile(delay_ms=5, loss_pct=10), "udp")
        self.assertLess(m["delivery_pct"], 97)
        self.assertGreater(m["delivery_pct"], 80)

    def test_clean_link(self):
        for proto in ("tcp", "udp"):
            m = self.run_through(LinkProfile(delay_ms=10), proto)
            self.assertEqual(m["delivery_pct"], 100.0)
            self.assertGreaterEqual(m["latency_min_ms"], 9.5)


if __name__ == "__main__":
    unittest.main()
