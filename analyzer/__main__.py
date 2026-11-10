"""Command line: python -m analyzer <server|emulator|client|benchmark|scenarios>"""

import argparse
import os
import sys

from . import benchmark, client, emulator, metrics, report
from .scenarios import QUICK, SCENARIOS


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m analyzer",
                                 description="TCP vs UDP Performance Analyzer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("server", help="run the measurement server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=5001, help="TCP and UDP data port")
    s.add_argument("--ctrl-port", type=int, default=5000)

    e = sub.add_parser("emulator", help="run the network emulator between client and server")
    e.add_argument("--listen", default="0.0.0.0")
    e.add_argument("--port", type=int, default=6001, help="port clients connect to (tcp+udp)")
    e.add_argument("--target", default="127.0.0.1", help="server host")
    e.add_argument("--target-port", type=int, default=5001)
    g = e.add_mutually_exclusive_group()
    g.add_argument("--scenario", choices=SCENARIOS)
    g.add_argument("--link", help="e.g. 'delay=50,jitter=10,loss=2,bw=10,queue=64'")
    e.add_argument("--seed", type=int)

    c = sub.add_parser("client", help="run one test against a server (directly or via emulator)")
    c.add_argument("--proto", choices=["tcp", "udp", "both"], default="both")
    c.add_argument("--host", default="127.0.0.1", help="where to send data (server or emulator)")
    c.add_argument("--port", type=int, default=6001)
    c.add_argument("--server", default=None, help="server host for the control channel (default: --host)")
    c.add_argument("--ctrl-port", type=int, default=5000)
    c.add_argument("--count", type=int, default=1000)
    c.add_argument("--size", type=int, default=1024, help="payload bytes per message")
    c.add_argument("--rate", type=float, default=400, help="messages/s, 0 = as fast as possible")

    b = sub.add_parser("benchmark", help="run all scenarios locally and write a report")
    b.add_argument("--quick", action="store_true", help=f"only {', '.join(QUICK)}")
    b.add_argument("--scenarios", help="comma-separated subset")
    b.add_argument("--count", type=int, default=800)
    b.add_argument("--size", type=int, default=1024)
    b.add_argument("--rate", type=float, default=400)
    b.add_argument("--bulk-count", type=int, default=1500)
    b.add_argument("--no-bulk", action="store_true")
    b.add_argument("--backend", choices=["emulator", "netem"], default="emulator")
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--out", default="results")

    sub.add_parser("scenarios", help="list the built-in network scenarios")

    w = sub.add_parser("web", help="open the interactive dashboard in a browser")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--out", default="results")
    w.add_argument("--no-browser", action="store_true")

    rp = sub.add_parser("report", help="rebuild report.html from a results.json")
    rp.add_argument("results_json")

    a = ap.parse_args(argv)

    if a.cmd == "server":
        from .server import run_server
        run_server(a.host, a.port, a.port, a.ctrl_port)

    elif a.cmd == "emulator":
        prof = SCENARIOS[a.scenario][1] if a.scenario else emulator.LinkProfile.parse(a.link)
        emulator.run_emulator(a.listen, a.port, a.target, a.target_port, prof, seed=a.seed)

    elif a.cmd == "client":
        ctrl_host = a.server or a.host
        client.wait_for_server(ctrl_host, a.ctrl_port)
        protos = ["tcp", "udp"] if a.proto == "both" else [a.proto]
        rows = [metrics.compute(client.run_test(p, a.host, a.port, ctrl_host, a.ctrl_port,
                                                a.count, a.size, a.rate)) for p in protos]
        print(report.text_table(rows))

    elif a.cmd == "benchmark":
        names = (a.scenarios.split(",") if a.scenarios else QUICK if a.quick else list(SCENARIOS))
        bad = [n for n in names if n not in SCENARIOS]
        if bad:
            ap.error(f"unknown scenario(s): {', '.join(bad)}")
        data = benchmark.run(names, a.count, a.size, a.rate, a.bulk_count, not a.no_bulk,
                             a.backend, seed=a.seed)
        benchmark.save(data, a.out)
        path = report.write_html(data, os.path.join(a.out, "report.html"))
        print("\n" + report.summary_table(data))
        print("\nKey findings:")
        for line in report.findings(data):
            print(f"  • {line}")
        print(f"\nWrote {a.out}/results.json, {a.out}/results.csv and {path}")

    elif a.cmd == "web":
        from .web import run_web
        run_web(a.port, a.out, not a.no_browser)

    elif a.cmd == "scenarios":
        for name, (title, prof) in SCENARIOS.items():
            print(f"{name:13} {title:45} {prof.describe()}")

    elif a.cmd == "report":
        import json
        with open(a.results_json) as f:
            data = json.load(f)
        print(report.write_html(data, os.path.join(os.path.dirname(a.results_json) or ".", "report.html")))


if __name__ == "__main__":
    sys.exit(main())
