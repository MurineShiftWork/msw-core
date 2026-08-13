"""Measure the added trigger latency of the isolated sound server.

Reports the client-put -> server-recv hop (the latency the subprocess isolation adds on top of the
in-process StereoSound path). Run the SAME command on Linux and Windows to compare parity:

    uv run python scripts/bench_sound_server.py            # pure IPC (no sound card needed)
    uv run python scripts/bench_sound_server.py --real     # server also opens PortAudio

Budget: p99 <= 5 ms, Unix/Windows parity.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys

from murineshiftwork.logic.sound_server import SoundServerClient


def _pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-n", type=int, default=5000, help="measured triggers (after warmup)"
    )
    ap.add_argument("--warmup", type=int, default=500)
    ap.add_argument(
        "--real",
        action="store_true",
        help="server opens PortAudio (measures under the real audio thread load)",
    )
    args = ap.parse_args()

    # measure=True drives the latency reply path. --real additionally opens PortAudio so the
    # server's get-loop competes with the live audio callback thread (and actually plays each cue).
    client = SoundServerClient(measure=True, open_audio=args.real)
    try:
        # register one cue so the server registry is warm (parity with a real session)
        client.register_new_sound(frequency=5000, duration=0.1, amplitude=0.2)
        lats_ms = [
            x * 1000.0
            for x in client.measure_trigger_latency(n=args.n, warmup=args.warmup)
        ]
    finally:
        client.close()

    lats_ms.sort()
    print(
        f"platform : {platform.system()} {platform.release()} / py{sys.version.split()[0]}"
    )
    print(f"samples  : {len(lats_ms)} (warmup {args.warmup} discarded)")
    print(f"min      : {lats_ms[0]:.3f} ms")
    print(f"p50      : {_pct(lats_ms, 0.50):.3f} ms")
    print(f"mean     : {statistics.fmean(lats_ms):.3f} ms")
    print(f"p95      : {_pct(lats_ms, 0.95):.3f} ms")
    print(f"p99      : {_pct(lats_ms, 0.99):.3f} ms")
    print(f"p99.9    : {_pct(lats_ms, 0.999):.3f} ms")
    print(f"max      : {lats_ms[-1]:.3f} ms")

    p99 = _pct(lats_ms, 0.99)
    verdict = "PASS" if p99 <= 5.0 else "FAIL"
    print(f"budget   : p99 <= 5 ms -> {verdict} (p99={p99:.3f} ms)")
    return 0 if p99 <= 5.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
