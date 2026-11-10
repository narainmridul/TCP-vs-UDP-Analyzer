"""Web dashboard: python -m analyzer web  ->  http://localhost:8000

Runs real tests (server + emulator processes, real sockets) from the browser.
Standard library only.
"""

import json
import multiprocessing as mp
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import benchmark, client, emulator, metrics, report
from .emulator import LinkProfile
from .scenarios import QUICK, SCENARIOS

STATIC = os.path.join(os.path.dirname(__file__), "static")
HOST = "127.0.0.1"


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def series(raw):
    """Per-message view for the timeline: latency (ms) of first intact copy, or None if lost."""
    lat = [None] * raw["count"]
    ooo, max_seq = [], -1
    for seq, ts, ok in raw["records"]:
        if not ok or seq >= raw["count"] or lat[seq] is not None:
            continue
        lat[seq] = round((ts - raw["send_ts"][seq]) * 1000, 3)
        if seq < max_seq:
            ooo.append(seq)
        max_seq = max(max_seq, seq)
    return {"lat": lat, "ooo": ooo}


class App:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.ctrl, self.data = free_port(), free_port()
        self.server = benchmark._start(_run_server, HOST, self.data, self.data, self.ctrl)
        client.wait_for_server(HOST, self.ctrl)
        self.job = {"state": "idle", "log": [], "result": None, "kind": None}
        self.lock = threading.Lock()

    def start(self, kind, fn):
        with self.lock:
            if self.job["state"] == "running":
                return False
            self.job = {"state": "running", "log": [], "result": None, "kind": kind, "t0": time.time()}

        def work():
            try:
                self.job["result"] = fn(self.job["log"].append)
                self.job["state"] = "done"
            except Exception as e:  # surface to the UI
                self.job["log"].append(f"error: {e}")
                self.job["state"] = "error"
        threading.Thread(target=work, daemon=True).start()
        return True

    def compare(self, req, log):
        prof = LinkProfile(**{k: float(v) for k, v in req["profile"].items()
                              if k in LinkProfile.__dataclass_fields__})
        count, size, rate = int(req["count"]), int(req["size"]), float(req["rate"])
        stats = mp.Array("q", len(emulator.STAT_NAMES), lock=False)
        port = free_port()
        emu = benchmark._start(emulator.run_emulator, HOST, port, HOST, self.data, prof,
                               stats=stats, verbose=False)
        out = {"profile": prof.to_dict(), "describe": prof.describe(), "protos": {}}
        try:
            for proto in ("tcp", "udp"):
                log(f"{proto} · sending {count} messages")
                before = list(stats)
                raw = client.run_test(proto, HOST, port, HOST, self.ctrl, count=count, size=size,
                                      rate=rate, settle=benchmark._settle(prof))
                m = metrics.compute(raw)
                m["series"] = series(raw)
                m["link"] = {n: stats[i] - before[i] for i, n in enumerate(emulator.STAT_NAMES)
                             if n.startswith(proto)}
                if req.get("bulk"):
                    log(f"{proto} · bulk throughput")
                    b = metrics.compute(client.run_test(proto, HOST, port, HOST, self.ctrl,
                                                        count=int(req.get("bulk_count", 1500)),
                                                        size=size, rate=0,
                                                        settle=benchmark._settle(prof)))
                    m["bulk"] = {k: b[k] for k in ("goodput_mbps", "delivery_pct")}
                out["protos"][proto] = m
        finally:
            benchmark._stop(emu)
        return out

    def bench(self, req, log):
        names = QUICK if req.get("quick") else list(SCENARIOS)
        data = benchmark.run(names, log=lambda s: s.strip() and log(s.strip()),
                             ctrl_port=free_port(), data_port=free_port(), emu_port=free_port())
        benchmark.save(data, self.out_dir)
        report.write_html(data, os.path.join(self.out_dir, "report.html"))
        data["findings"] = report.findings(data)
        return data


def _run_server(*a, ready=None):
    from .server import run_server
    run_server(*a, verbose=False, ready=ready)


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype="application/json", code=200):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def file(self, path, ctype):
            if not os.path.exists(path):
                return self.send({"error": "not found"}, code=404)
            with open(path, "rb") as f:
                self.send(f.read(), ctype)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self.file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
            if p == "/api/scenarios":
                return self.send([{"name": n, "title": t, "profile": pr.to_dict(), "describe": pr.describe()}
                                  for n, (t, pr) in SCENARIOS.items()])
            if p == "/api/job":
                j = dict(app.job)
                j.pop("t0", None)
                return self.send(j)
            if p == "/api/last":
                path = os.path.join(app.out_dir, "results.json")
                if not os.path.exists(path):
                    return self.send(None)
                with open(path) as f:
                    data = json.load(f)
                data["findings"] = report.findings(data)
                return self.send(data)
            if p == "/report":
                return self.file(os.path.join(app.out_dir, "report.html"), "text/html; charset=utf-8")
            self.send({"error": "not found"}, code=404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            fn = {"/api/run": app.compare, "/api/benchmark": app.bench}.get(self.path)
            if fn is None:
                return self.send({"error": "not found"}, code=404)
            ok = app.start(self.path.rsplit("/", 1)[1], lambda log: fn(req, log))
            self.send({"started": ok}, code=200 if ok else 409)
    return H


def run_web(port=8000, out_dir="results", open_browser=True):
    app = App(out_dir)
    httpd = ThreadingHTTPServer((HOST, port), make_handler(app))
    url = f"http://{HOST}:{port}"
    print(f"TCP vs UDP Analyzer dashboard at {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        benchmark._stop(app.server)
