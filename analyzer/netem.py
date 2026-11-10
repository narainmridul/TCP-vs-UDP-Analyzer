"""Optional backend: Linux `tc netem` on the loopback interface.

Instead of the userspace emulator, impairments are applied by the kernel, so
TCP's real retransmission and congestion control run. Requires Linux, root
and the sch_netem kernel module. Note that netem on `lo` affects both
directions (data and ACKs), so the RTT is 2 x delay, matching the userspace
model.
"""

import shutil
import subprocess


def tc_args(profile, dev="lo"):
    args = ["tc", "qdisc", "replace", "dev", dev, "root", "netem"]
    if profile.delay_ms or profile.jitter_ms:
        args += ["delay", f"{profile.delay_ms}ms"]
        if profile.jitter_ms:
            args += [f"{profile.jitter_ms}ms", "distribution", "normal"]
    if profile.loss_pct:
        args += ["loss", f"{profile.loss_pct}%"]
    if profile.corrupt_pct:
        args += ["corrupt", f"{profile.corrupt_pct}%"]
    if profile.duplicate_pct:
        args += ["duplicate", f"{profile.duplicate_pct}%"]
    if profile.bandwidth_mbps:
        args += ["rate", f"{profile.bandwidth_mbps}mbit"]
        # netem's limit is in packets; approximate the byte queue with 1500-byte packets
        args += ["limit", str(max(1, int(profile.queue_kb * 1024 / 1500)))]
    else:
        args += ["limit", "100000"]
    return args


def available(dev="lo"):
    if shutil.which("tc") is None:
        return False, "the `tc` command is not installed (apt install iproute2)"
    probe = subprocess.run(["tc", "qdisc", "replace", "dev", dev, "root", "netem", "delay", "0ms"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        return False, probe.stderr.strip() or "tc netem failed (need root and the sch_netem module)"
    clear(dev)
    return True, ""


def apply(profile, dev="lo"):
    subprocess.run(tc_args(profile, dev), check=True, capture_output=True, text=True)


def clear(dev="lo"):
    subprocess.run(["tc", "qdisc", "del", "dev", dev, "root"], capture_output=True, text=True)
