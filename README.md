# TCP vs UDP Performance Analyzer

A client-server application that sends identical message streams over **TCP** and **UDP** through an
emulated network link, and compares them on **packet delivery, delay, jitter, throughput and reliability**
across eight network conditions.

Pure Python 3.9+ standard library: nothing to install.

```
 client ──data──► emulator :6001 ──data──► server :5001 (tcp + udp)
   │            delay · jitter · loss ·         │
   │            corruption · duplication ·      │
   │            bandwidth cap · router queue    │
   └──────────── control :5000 (JSON, unimpaired) ◄┘   "what did you receive?"
```

## Quick start

```bash
python3 -m analyzer benchmark            # all 8 scenarios, ~75 s
python3 -m analyzer benchmark --quick    # 4 scenarios, ~35 s
```

Prints a summary table and plain-language findings, then writes `results/report.html` (charts, open in any
browser), `results/results.json` and `results/results.csv`. A sample run is committed in `results/`.

## Live demo (three terminals, or three machines)

```bash
python3 -m analyzer server
python3 -m analyzer emulator --scenario loss-5          # or --link "delay=50,jitter=10,loss=2,bw=5"
python3 -m analyzer client --count 500 --rate 250        # runs TCP then UDP, prints side by side
```

Across machines: run the emulator with `--target <server-ip>` and the client with
`--host <emulator-ip> --server <server-ip>`. One-way delay needs synced clocks (NTP); delivery, jitter and
throughput don't.

Suggested 5-minute script for the presentation:
1. `scenarios`: show the conditions.
2. Client on `baseline`: both deliver 100% with sub-millisecond delay.
3. Restart emulator on `loss-5`: UDP drops ~5% at a flat 21 ms; TCP delivers 100% but p99 jumps to ~240 ms.
   That's the trade-off in one screen.
4. `bottleneck`: UDP floods the 2 Mbps link and loses ~37%; TCP backs off and loses nothing.
5. Open `results/report.html` for the full picture.

## Scenarios

| name | condition | profile |
| --- | --- | --- |
| baseline | Ideal LAN | none |
| high-latency | Long-haul path | 80 ms delay |
| jitter | Congested Wi-Fi | 30 ms ± 15 ms |
| loss-1 / loss-5 / loss-15 | Lossy links | 20 ms, 1% / 5% / 15% loss |
| bottleneck | Slow link, small buffer | 20 ms, 2 Mbps, 32 KB queue |
| mobile | Bad mobile link | 40 ms ± 20, 3% loss, 0.5% corrupt, 1% dup, 10 Mbps |

## What gets measured

Every message carries `seq`, a send timestamp and a CRC-32 (see `analyzer/protocol.py`). The server logs each
arrival; the client fetches the log over the control channel.

| metric | definition |
| --- | --- |
| Delivery % | unique, intact messages received ÷ sent |
| Delay p50/p95/p99 | one-way, send → receive |
| Jitter | mean \|delay(i) − delay(i−1)\| over consecutive arrivals |
| Throughput (goodput) | unique intact payload bits ÷ (last arrival − first send), bulk send |
| Reliability | delivered exactly once, intact **and** in order ÷ sent. What an app gets without its own repair logic |
| Corrupted / duplicates / reordered | raw counts |

Each scenario runs two workloads per protocol. **Paced** (400 msg/s × 1 KB ≈ 3.3 Mbps) gives the same
offered load for a fair delay/loss comparison. **Bulk** (as fast as possible) measures achievable throughput.

## Results (sample run)

| scenario | delivery TCP / UDP | p95 delay ms TCP / UDP | UDP reordered | bulk Mbps TCP / UDP |
| --- | --- | --- | --- | --- |
| baseline | 100 / 100 | 0.5 / 0.4 | 0 | 1262 / 300 |
| high-latency | 100 / 100 | 144 / 82 | 0 | 3.2 / 134 |
| jitter | 100 / 100 | 61 / 54 | 582 of 800 | 6.3 / 128 |
| loss-5 | 100 / 94.1 | 256 / 22 | 0 | 5.3 / 341 |
| loss-15 | 100 / 83.8 | 1227 / 22 | 0 | 1.6 / 300 |
| bottleneck | 100 / 63.4 | 1314 / 152 | 0 | 1.95 / 1.7 (2.2% delivered) |

The pattern: **TCP trades time for completeness, UDP trades completeness for time.** Loss never reaches a TCP
application, but each lost segment stalls everything behind it, so tail latency and throughput degrade
sharply. UDP's latency stays at the propagation delay whatever the loss rate, but the application sees
every drop, reorder and duplicate. With high delay, TCP throughput is capped at window ÷ RTT
(64 KB ÷ 160 ms ≈ 3.3 Mbps, measured 3.2). UDP has no such limit, and also no congestion control, which is
why it destroys itself on the bottleneck link. Baseline bulk UDP is slower than TCP because Python pays a
system call per 1 KB datagram, while TCP moves large chunks.

## How the emulator works (and its limits)

`analyzer/emulator.py` is an impairment proxy applying one link profile to both protocols:

- **UDP**: per datagram: random loss, payload bit-flip (caught by the CRC), duplication, independent
  normal-distributed delay (so datagrams reorder), and serialization at the bandwidth cap with tail drop when
  the router queue is full.
- **TCP**: real kernel TCP runs on both hops, but a userspace proxy can't drop bytes from a stream. So the
  stream is cut into MSS segments, and each "lost" or corrupted segment is delayed by TCP's recovery
  cost: **+1 RTT** (fast retransmit), then **RTO from 200 ms with exponential backoff** if the
  retransmission is lost too. Delivery is strictly in order (head-of-line blocking). A 64 KB window, freed
  only when the ACK would return, bounds throughput to window ÷ RTT.
- **Not modelled**: TCP congestion-window cuts after loss (real TCP would lose even more throughput under
  loss), and the ACK path is not lossy.

For the kernel's real TCP on Linux, run as root with `sch_netem` available:

```bash
sudo python3 -m analyzer benchmark --backend netem
```

This applies `tc qdisc ... netem` to `lo` per scenario instead of using the proxy. It was not exercised in
the development environment, whose kernel lacks netem.

## Project layout

```
analyzer/
  protocol.py   wire format: 24-byte header (magic, session, seq, timestamp, length, CRC-32)
  server.py     asyncio TCP + UDP receiver, JSON control channel
  client.py     paced sender, result collection
  emulator.py   network link emulator (the impairment proxy)
  netem.py      optional Linux tc-netem backend
  metrics.py    delivery, delay percentiles, jitter, goodput, reliability
  scenarios.py  the 8 named network conditions
  benchmark.py  runs everything, saves JSON/CSV
  report.py     terminal tables, findings, HTML report with SVG charts
tests/          unit tests + end-to-end tests over real sockets
```

```bash
python3 -m unittest discover -s tests
```
