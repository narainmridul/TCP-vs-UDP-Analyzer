"""Run every scenario for TCP and UDP and collect the metrics.

Each scenario runs two workloads per protocol:
  paced  fixed send rate (default 400 msg/s x 1 KB ~ 3.3 Mbps): delivery, delay,
         jitter and reliability under identical offered load
  bulk   send as fast as the sender can: achievable throughput
"""

import json
import multiprocessing as mp
import os
import time

from . import client, emulator, metrics, netem
from .scenarios import SCENARIOS


def _start(target, *args, **kwargs):
    ready = mp.Event()
    p = mp.Process(target=target, args=args, kwargs={**kwargs, "ready": ready}, daemon=True)
    p.start()
    if not ready.wait(10):
        p.terminate()
        raise RuntimeError(f"{target.__name__} did not start")
    return p


def _stop(p):
    p.terminate()
    p.join(5)


def run(scenario_names, count=800, size=1024, rate=400, bulk_count=1500, bulk=True,
        backend="emulator", host="127.0.0.1", ctrl_port=5000, data_port=5001, emu_port=6001,
        seed=1, log=print):
    from .server import run_server

    if backend == "netem":
        ok, why = netem.available()
        if not ok:
            raise RuntimeError(f"netem backend unavailable: {why}")

    server = _start(run_server, host, data_port, data_port, ctrl_port, verbose=False)
    client.wait_for_server(host, ctrl_port)
    results = []
    try:
        for name in scenario_names:
            title, profile = SCENARIOS[name]
            log(f"\n▶ {name}: {title}  [{profile.describe()}]")
            stats = mp.Array("q", len(emulator.STAT_NAMES), lock=False)
            if backend == "emulator":
                emu = _start(emulator.run_emulator, host, emu_port, host, data_port, profile,
                             seed=seed, stats=stats, verbose=False)
                port = emu_port
            else:
                netem.apply(profile)
                emu, port = None, data_port
            try:
                row = {"scenario": name, "title": title, "profile": profile.to_dict(),
                       "describe": profile.describe(), "paced": {}, "bulk": {}, "link": {}}
                for proto in ("tcp", "udp"):
                    before = list(stats)
                    m = metrics.compute(client.run_test(
                        proto, host, port, host, ctrl_port, count=count, size=size, rate=rate,
                        settle=_settle(profile)))
                    row["link"][proto] = {n: stats[i] - before[i]
                                          for i, n in enumerate(emulator.STAT_NAMES)
                                          if n.startswith(proto)}
                    row["paced"][proto] = m
                    log(f"   {proto.upper()} paced  delivered {m['delivery_pct']:6.2f}%  "
                        f"p50 {_f(m['latency_p50_ms'])} ms  p95 {_f(m['latency_p95_ms'])} ms  "
                        f"jitter {_f(m['jitter_ms'])} ms  in-order {m['reliability_pct']:.1f}%")
                    if bulk:
                        b = metrics.compute(client.run_test(
                            proto, host, port, host, ctrl_port, count=bulk_count, size=size,
                            rate=0, settle=_settle(profile)))
                        row["bulk"][proto] = b
                        log(f"   {proto.upper()} bulk   goodput {b['goodput_mbps']:7.2f} Mbps  "
                            f"delivered {b['delivery_pct']:6.2f}%")
                results.append(row)
            finally:
                if emu is not None:
                    _stop(emu)
                else:
                    netem.clear()
                time.sleep(0.1)
    finally:
        _stop(server)

    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"count": count, "size": size, "rate": rate, "bulk_count": bulk_count,
                   "backend": backend, "seed": seed,
                   "offered_mbps": round(rate * size * 8 / 1e6, 2)},
        "results": results,
    }


def _settle(profile):
    # long enough for the slowest in-flight UDP datagram to arrive
    return max(0.6, 2 * (profile.delay_ms + 4 * profile.jitter_ms) / 1000 + 0.3)


def _f(x):
    return "   -  " if x is None else f"{x:6.2f}"


def save(data, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump(data, f, indent=2)
    cols = ["delivery_pct", "lost", "corrupted", "duplicates", "out_of_order", "latency_mean_ms",
            "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "latency_max_ms", "jitter_ms",
            "goodput_mbps", "reliability_pct", "duration_s"]
    with open(os.path.join(out_dir, "results.csv"), "w") as f:
        f.write("scenario,workload,proto," + ",".join(cols) + "\n")
        for row in data["results"]:
            for wl in ("paced", "bulk"):
                for proto, m in row[wl].items():
                    f.write(",".join([row["scenario"], wl, proto] +
                                     ["" if m[c] is None else str(m[c]) for c in cols]) + "\n")
