"""Turn raw send/receive records into comparable metrics."""

import math
import statistics


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def compute(run):
    """Metrics for one run (output of client.run_test).

    delivery_pct     unique, intact messages received / messages sent
    loss_pct         100 - delivery_pct
    corrupted        messages that arrived with a bad CRC (counted as not delivered)
    duplicates       extra copies of a message that was already received
    out_of_order     arrivals whose seq is lower than one already seen
    latency_*_ms     one-way delay, send -> receive (same clock on one host)
    jitter_ms        mean |latency(i) - latency(i-1)| over consecutive arrivals
    goodput_mbps     unique intact payload bits / (last arrival - first send)
    reliability_pct  messages delivered exactly once, intact AND in order / sent
    """
    count, size = run["count"], run["size"]
    send_ts = run["send_ts"]
    seen = set()
    latencies = []      # first copy of each intact message, in arrival order
    corrupted = duplicates = out_of_order = 0
    ooo_seqs = set()
    max_seq = -1
    last_recv = None

    for seq, ts, ok in run["records"]:
        if not ok:
            corrupted += 1
            continue
        if seq >= count:
            continue
        if seq in seen:
            duplicates += 1
            continue
        seen.add(seq)
        if seq < max_seq:
            out_of_order += 1
            ooo_seqs.add(seq)
        else:
            max_seq = seq
        latencies.append((ts - send_ts[seq]) * 1000.0)
        last_recv = ts if last_recv is None else max(last_recv, ts)

    delivered = len(seen)
    lat_sorted = sorted(latencies)
    jitter = (statistics.fmean(abs(b - a) for a, b in zip(latencies, latencies[1:]))
              if len(latencies) > 1 else 0.0)
    first_send = send_ts[0] if count else 0.0
    duration = (last_recv - first_send) if last_recv else 0.0
    goodput = (delivered * size * 8 / duration / 1e6) if duration > 0 else 0.0
    reliable = delivered - len(ooo_seqs)

    def r(x, n=3):
        return None if x is None else round(x, n)

    return {
        "proto": run["proto"],
        "sent": count,
        "delivered": delivered,
        "lost": count - delivered,
        "delivery_pct": r(100.0 * delivered / count if count else 0.0, 2),
        "loss_pct": r(100.0 * (count - delivered) / count if count else 0.0, 2),
        "corrupted": corrupted,
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "send_errors": run.get("send_errors", 0),
        "latency_mean_ms": r(statistics.fmean(latencies) if latencies else None),
        "latency_min_ms": r(lat_sorted[0] if lat_sorted else None),
        "latency_p50_ms": r(_pct(lat_sorted, 50)),
        "latency_p95_ms": r(_pct(lat_sorted, 95)),
        "latency_p99_ms": r(_pct(lat_sorted, 99)),
        "latency_max_ms": r(lat_sorted[-1] if lat_sorted else None),
        "jitter_ms": r(jitter),
        "duration_s": r(duration),
        "goodput_mbps": r(goodput),
        "offered_mbps": r(run["rate"] * size * 8 / 1e6 if run["rate"] > 0 else None),
        "reliability_pct": r(100.0 * reliable / count if count else 0.0, 2),
    }
