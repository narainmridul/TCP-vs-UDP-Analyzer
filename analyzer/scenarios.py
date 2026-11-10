"""Named network conditions used by the benchmark."""

from .emulator import LinkProfile

SCENARIOS = {
    "baseline": ("Ideal LAN", LinkProfile()),
    "high-latency": ("Long-haul path (e.g. Mumbai to Frankfurt)", LinkProfile(delay_ms=80)),
    "jitter": ("Variable delay (congested Wi-Fi)", LinkProfile(delay_ms=30, jitter_ms=15)),
    "loss-1": ("Light loss, 1%", LinkProfile(delay_ms=20, loss_pct=1)),
    "loss-5": ("Moderate loss, 5%", LinkProfile(delay_ms=20, loss_pct=5)),
    "loss-15": ("Heavy loss, 15%", LinkProfile(delay_ms=20, loss_pct=15)),
    "bottleneck": ("2 Mbps bottleneck, small router buffer",
                   LinkProfile(delay_ms=20, bandwidth_mbps=2, queue_kb=32)),
    "mobile": ("Bad mobile link (all of the above, mildly)",
               LinkProfile(delay_ms=40, jitter_ms=20, loss_pct=3, corrupt_pct=0.5,
                           duplicate_pct=1, bandwidth_mbps=10, queue_kb=64)),
}

QUICK = ["baseline", "high-latency", "loss-5", "bottleneck"]
