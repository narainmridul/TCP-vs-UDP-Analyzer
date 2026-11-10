"""Text tables, auto-generated findings and a self-contained HTML report."""

import html
import math

TCP_COLOR, UDP_COLOR = "var(--tcp)", "var(--udp)"


# ------------------------------------------------------------------ text

def _v(x, fmt="{:.2f}"):
    return "-" if x is None else fmt.format(x)


def text_table(rows):
    """Side-by-side metrics for one client run (tcp and/or udp)."""
    fields = [
        ("Messages sent", "sent", "{:d}"), ("Delivered", "delivered", "{:d}"),
        ("Delivery %", "delivery_pct", "{:.2f}"), ("Corrupted", "corrupted", "{:d}"),
        ("Duplicates", "duplicates", "{:d}"), ("Out of order", "out_of_order", "{:d}"),
        ("Latency mean (ms)", "latency_mean_ms", "{:.2f}"), ("Latency p50 (ms)", "latency_p50_ms", "{:.2f}"),
        ("Latency p95 (ms)", "latency_p95_ms", "{:.2f}"), ("Latency p99 (ms)", "latency_p99_ms", "{:.2f}"),
        ("Latency max (ms)", "latency_max_ms", "{:.2f}"), ("Jitter (ms)", "jitter_ms", "{:.2f}"),
        ("Goodput (Mbps)", "goodput_mbps", "{:.2f}"), ("Exactly-once in order %", "reliability_pct", "{:.2f}"),
    ]
    head = f"{'Metric':26}" + "".join(f"{r['proto'].upper():>12}" for r in rows)
    lines = [head, "-" * len(head)]
    for label, key, fmt in fields:
        lines.append(f"{label:26}" + "".join(f"{_v(r[key], fmt):>12}" for r in rows))
    return "\n".join(lines)


def summary_table(data):
    hdr = (f"{'scenario':13}{'deliv TCP':>10}{'UDP':>8}{'p95 ms TCP':>12}{'UDP':>9}"
           f"{'jitter TCP':>11}{'UDP':>7}{'UDP reord':>10}{'Mbps TCP':>10}{'UDP*':>8}")
    out = [hdr, "-" * len(hdr)]
    for r in data["results"]:
        t, u = r["paced"]["tcp"], r["paced"]["udp"]
        bt, bu = r["bulk"].get("tcp", {}), r["bulk"].get("udp", {})
        out.append(f"{r['scenario']:13}{t['delivery_pct']:>9.1f}%{u['delivery_pct']:>7.1f}%"
                   f"{_v(t['latency_p95_ms'], '{:.1f}'):>12}{_v(u['latency_p95_ms'], '{:.1f}'):>9}"
                   f"{_v(t['jitter_ms'], '{:.1f}'):>11}{_v(u['jitter_ms'], '{:.1f}'):>7}"
                   f"{u['out_of_order']:>10d}"
                   f"{_v(bt.get('goodput_mbps'), '{:.1f}'):>10}{_v(bu.get('goodput_mbps'), '{:.1f}'):>8}")
    out.append("* bulk UDP goodput counts only what arrived; see delivery % in the report")
    return "\n".join(out)


# ------------------------------------------------------------------ findings

def findings(data):
    """Plain-language observations derived from the measured numbers."""
    res = {r["scenario"]: r for r in data["results"]}
    out = []

    def P(r, proto):
        return r["paced"][proto]

    def B(r, proto):
        return r["bulk"].get(proto)

    base = res.get("baseline")
    if base:
        t, u = P(base, "tcp"), P(base, "udp")
        if t["latency_p50_ms"] and u["latency_p50_ms"]:
            out.append(f"On a clean link both deliver 100%; UDP's median one-way delay is "
                       f"{u['latency_p50_ms']:.2f} ms vs {t['latency_p50_ms']:.2f} ms for TCP. "
                       f"With no impairment the gap is pure processing overhead (stream framing, ACKs, the relay), not the network.")

    for r in data["results"]:
        p = r["profile"]
        t, u = P(r, "tcp"), P(r, "udp")
        bt, bu = B(r, "tcp"), B(r, "udp")
        name = r["scenario"]
        if p["loss_pct"] and not p["bandwidth_mbps"] and not p["jitter_ms"]:
            out.append(f"{name}: at {p['loss_pct']:g}% loss UDP lost {u['loss_pct']:.1f}% of messages "
                       f"and kept p95 at {u['latency_p95_ms']:.0f} ms. TCP delivered "
                       f"{t['delivery_pct']:.0f}%, but retransmissions and head-of-line blocking "
                       f"pushed its p95 to {t['latency_p95_ms']:.0f} ms"
                       + (f" and cut bulk throughput to {bt['goodput_mbps']:.1f} Mbps." if bt else "."))
        elif p["jitter_ms"] and not p["loss_pct"]:
            out.append(f"{name}: with ±{p['jitter_ms']:g} ms jitter, {u['out_of_order']} of "
                       f"{u['sent']} UDP datagrams arrived out of order. TCP had none, but waiting "
                       f"for late segments lifted its median delay to {t['latency_p50_ms']:.0f} ms "
                       f"vs {u['latency_p50_ms']:.0f} ms for UDP.")
        elif p["bandwidth_mbps"] and not p["loss_pct"]:
            cfg = data["config"]
            out.append(f"{name}: offered {cfg['offered_mbps']:g} Mbps into a {p['bandwidth_mbps']:g} Mbps "
                       f"link. UDP has no congestion control, so the router dropped "
                       f"{u['loss_pct']:.0f}% of it. TCP lost nothing but slowed the sender: "
                       f"its median delay grew to {t['latency_p50_ms']:.0f} ms as data queued.")
            if bt and bu:
                out.append(f"{name} (bulk): TCP filled the bottleneck at {bt['goodput_mbps']:.2f} Mbps "
                           f"with 100% delivery; blasting UDP got {bu['goodput_mbps']:.2f} Mbps through "
                           f"but only {bu['delivery_pct']:.1f}% of what it sent arrived.")
        elif p["delay_ms"] and not any(p[k] for k in ("loss_pct", "jitter_ms", "bandwidth_mbps",
                                                         "corrupt_pct", "duplicate_pct")):
            if bt and bu and base and B(base, "tcp"):
                rtt = 2 * p["delay_ms"]
                bound = p["window_kb"] * 1024 * 8 / (rtt / 1000) / 1e6
                out.append(f"{name}: both see ~{p['delay_ms']:g} ms delay, but TCP bulk throughput "
                           f"collapsed from {B(base, 'tcp')['goodput_mbps']:.0f} to "
                           f"{bt['goodput_mbps']:.1f} Mbps: a {p['window_kb']:g} KB window over a "
                           f"{rtt:g} ms RTT allows at most {bound:.1f} Mbps. UDP, which waits for "
                           f"no ACKs, still pushed {bu['goodput_mbps']:.0f} Mbps.")
        elif sum(bool(p[k]) for k in ("loss_pct", "jitter_ms", "bandwidth_mbps", "corrupt_pct")) >= 2:
            out.append(f"{name}: under combined impairments UDP delivered {u['delivery_pct']:.1f}% "
                       f"({u['corrupted']} corrupted, {u['duplicates']} duplicated, "
                       f"{u['out_of_order']} reordered) with p95 {u['latency_p95_ms']:.0f} ms; TCP "
                       f"delivered {t['delivery_pct']:.0f}%, intact and in order, with p95 "
                       f"{t['latency_p95_ms']:.0f} ms.")

    out.append("Takeaway: choose TCP when every byte must arrive intact and in order (files, web, "
               "databases). Choose UDP when late data is useless (voice, video, games, DNS) and the "
               "application can tolerate or repair loss itself.")
    return out


# ------------------------------------------------------------------ charts

def _fmt(x):
    if x is None:
        return "–"
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 10:
        return f"{x:.1f}"
    return f"{x:.2f}"


def bar_chart(title, subtitle, rows, unit, log=False, max_value=None):
    """Horizontal grouped bars. rows = [(label, tcp_value, udp_value)]."""
    label_w, chart_w, value_w = 118, 380, 64
    bar_h, gap, group_gap, top = 13, 3, 14, 8
    width = label_w + chart_w + value_w
    group_h = 2 * bar_h + gap
    height = top + len(rows) * (group_h + group_gap) + 22

    vals = [v for _, a, b in rows for v in (a, b) if v is not None]
    hi = max_value or (max(vals) if vals else 1) or 1
    if log:
        positives = [v for v in vals if v > 0]
        lo = 10 ** math.floor(math.log10(min(positives))) if positives else 0.1
        hi = 10 ** math.ceil(math.log10(hi))
        span = math.log10(hi) - math.log10(lo)

        def scale(v):
            return 0 if not v or v <= 0 else max(1.5, chart_w * (math.log10(max(v, lo)) - math.log10(lo)) / span)
        ticks = [10 ** e for e in range(int(math.log10(lo)), int(math.log10(hi)) + 1)]
    else:
        raw = hi / 4
        mag = 10 ** math.floor(math.log10(raw))
        step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
        hi = step * math.ceil(hi / step - 1e-9)

        def scale(v):
            return 0 if not v else chart_w * v / hi
        ticks = [step * i for i in range(int(round(hi / step)) + 1)]

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">']
    axis_y = height - 18
    for tv in ticks:
        x = label_w + scale(tv) if (tv > 0 or not log) else label_w
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{axis_y}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{axis_y + 13}" text-anchor="middle">{_fmt(tv)}</text>')
    y = top
    for label, a, b in rows:
        parts.append(f'<text class="lbl" x="{label_w - 8}" y="{y + group_h / 2 + 4:.1f}" '
                     f'text-anchor="end">{html.escape(label)}</text>')
        for v, color, cls in ((a, TCP_COLOR, "tcp"), (b, UDP_COLOR, "udp")):
            w = scale(v)
            parts.append(f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="2" '
                         f'fill="{color}"><title>{cls.upper()}: {_fmt(v)} {unit}</title></rect>')
            parts.append(f'<text class="val" x="{label_w + w + 5:.1f}" y="{y + bar_h - 2.5}">{_fmt(v)}</text>')
            y += bar_h + gap
        y += group_gap - gap
    parts.append("</svg>")
    return (f'<figure class="chart"><figcaption><strong>{html.escape(title)}</strong>'
            f'<span>{html.escape(subtitle)}</span></figcaption>{"".join(parts)}</figure>')


# ------------------------------------------------------------------ html

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--grid:#ecebe6;
--tcp:#2563c9;--udp:#d9692b;--good:#1f7a4d;--bad:#b3261e;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;
--muted:#9c9b94;--line:#34332f;--grid:#2a2a27;--tcp:#6c9cf0;--udp:#f08a4f;--good:#5cc28d;--bad:#f2766b;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#9c9b94;--line:#34332f;
--grid:#2a2a27;--tcp:#6c9cf0;--udp:#f08a4f;--good:#5cc28d;--bad:#f2766b;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:19px;margin:40px 0 12px}
.sub{color:var(--muted);margin:0 0 24px}
.legend{display:flex;gap:18px;font-size:13px;color:var(--muted);margin:0 0 8px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card b{display:block;font-size:13px;color:var(--muted);font-weight:600;margin-bottom:4px}
.card p{margin:0;font-size:14px}
ul.find{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 16px 16px 36px;margin:0}
ul.find li{margin:6px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,500px),1fr));gap:14px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 14px 6px;margin:0}
.chart figcaption{display:flex;flex-direction:column;margin-bottom:8px}
.chart figcaption span{color:var(--muted);font-size:13px}
.chart svg{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .tick{fill:var(--muted);font-size:10px}
.chart .lbl{fill:var(--ink);font-size:11.5px}
.chart .val{fill:var(--muted);font-size:10.5px}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
thead th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}
tr.tcp td:nth-child(2){color:var(--tcp);font-weight:600}tr.udp td:nth-child(2){color:var(--udp);font-weight:600}
td.bad{color:var(--bad)}td.ok{color:var(--good)}
.method{color:var(--muted);font-size:14px}.method code{font-size:13px}
footer{color:var(--muted);font-size:12px;margin-top:40px}
"""


def write_html(data, path):
    res = data["results"]
    cfg = data["config"]

    def series(workload, key):
        return [(r["scenario"], r[workload].get("tcp", {}).get(key), r[workload].get("udp", {}).get(key))
                for r in res]

    charts = [
        bar_chart("Packet delivery", "Paced load. % of messages that arrived intact (higher is better)",
                  series("paced", "delivery_pct"), "%", max_value=100),
        bar_chart("Exactly-once, in-order delivery", "Paced load. What an application gets without "
                  "doing its own reordering (higher is better)",
                  series("paced", "reliability_pct"), "%", max_value=100),
        bar_chart("Median one-way delay", "Paced load, ms, log scale (lower is better)",
                  series("paced", "latency_p50_ms"), "ms", log=True),
        bar_chart("Tail delay (p95)", "Paced load, ms, log scale (lower is better)",
                  series("paced", "latency_p95_ms"), "ms", log=True),
        bar_chart("Jitter", "Paced load. Mean change in delay between consecutive arrivals, ms",
                  series("paced", "jitter_ms"), "ms"),
    ]
    if any(r["bulk"] for r in res):
        charts.append(bar_chart("Throughput (goodput)", "Bulk send, Mbps of intact data delivered, "
                                "log scale (higher is better)", series("bulk", "goodput_mbps"), "Mbps", log=True))
        charts.append(bar_chart("Delivery under bulk send", "Bulk send. % of messages that arrived. "
                                "UDP has no congestion control",
                                series("bulk", "delivery_pct"), "%", max_value=100))

    cards = "".join(f'<div class="card"><b>{html.escape(r["scenario"])} · {html.escape(r["title"])}</b>'
                    f'<p>{html.escape(r["describe"])}</p></div>' for r in res)
    finds = "".join(f"<li>{html.escape(f)}</li>" for f in findings(data))

    cols = [("Delivery %", "delivery_pct"), ("Lost", "lost"), ("Corrupt", "corrupted"),
            ("Dup", "duplicates"), ("Reordered", "out_of_order"), ("p50 ms", "latency_p50_ms"),
            ("p95 ms", "latency_p95_ms"), ("p99 ms", "latency_p99_ms"), ("Max ms", "latency_max_ms"),
            ("Jitter ms", "jitter_ms"), ("In-order %", "reliability_pct")]
    rows = []
    for r in res:
        for proto in ("tcp", "udp"):
            m = r["paced"][proto]
            b = r["bulk"].get(proto, {})
            cells = []
            for _, k in cols:
                v = m[k]
                cls = ""
                if k == "delivery_pct":
                    cls = "ok" if v == 100 else "bad"
                cells.append(f'<td class="{cls}">{_fmt(v) if isinstance(v, float) else v}</td>')
            cells.append(f"<td>{_fmt(b.get('goodput_mbps'))}</td><td>{_fmt(b.get('delivery_pct'))}</td>")
            rows.append(f'<tr class="{proto}"><td>{html.escape(r["scenario"])}</td><td>{proto.upper()}</td>'
                        + "".join(cells) + "</tr>")
    table = ("<table><thead><tr><th>Scenario</th><th>Proto</th>"
             + "".join(f"<th>{c}</th>" for c, _ in cols)
             + "<th>Bulk Mbps</th><th>Bulk deliv %</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    backend = ("userspace link emulator (TCP loss modelled as retransmission delay)"
               if cfg["backend"] == "emulator" else "Linux tc netem on loopback (kernel TCP)")
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCP vs UDP Report</title><style>{CSS}</style></head><body><main>
<h1>TCP vs UDP Performance Analyzer</h1>
<p class="sub">{len(res)} network scenarios · paced load {cfg['count']} × {cfg['size']} B at {cfg['rate']:g} msg/s
({cfg['offered_mbps']:g} Mbps) · bulk {cfg['bulk_count']} messages · backend: {backend} · {html.escape(data['generated'])}</p>

<h2>Key findings</h2><ul class="find">{finds}</ul>

<h2>Network conditions tested</h2><div class="cards">{cards}</div>

<h2>Results</h2>
<div class="legend"><span><i style="background:var(--tcp)"></i>TCP</span><span><i style="background:var(--udp)"></i>UDP</span></div>
<div class="grid2">{''.join(charts)}</div>

<h2>All measurements</h2><div class="tablewrap">{table}</div>

<h2>Method</h2><div class="method">
<p>The client sends sequence-numbered, CRC-protected, timestamped messages to the server through a link
emulator. The server records every arrival and the client fetches those records over a separate control
channel that bypasses the emulator. Delay is one-way (sender and receiver share a clock), jitter is the mean
change in delay between consecutive arrivals, goodput counts only unique, intact payload bytes, and
"in-order" counts messages delivered exactly once, intact and not overtaken.</p>
<p>The emulator applies the same profile to both protocols. UDP datagrams are dropped, corrupted, duplicated,
delayed independently (so they reorder) and tail-dropped when the router queue is full. A userspace proxy
cannot drop bytes from a TCP stream, so each lost or corrupted TCP segment is instead delayed by what TCP's
recovery costs: +1 RTT for fast retransmit, then exponential RTO backoff from 200 ms. Segments are released in
order (head-of-line blocking), and a 64 KB window freed by returning ACKs limits throughput to window/RTT.
Congestion-window reduction is not modelled. Run <code>--backend netem</code> as root on Linux to measure
the kernel's real TCP instead.</p></div>
<footer>Generated by the TCP vs UDP Performance Analyzer · raw data in results.json / results.csv</footer>
</main></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
