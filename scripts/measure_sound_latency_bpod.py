"""Measure end-to-end sound trigger latency on real Bpod hardware via a BNC loopback.

Wiring: sound-card analog output -> Bpod BNC In 1. With --channel-mode both (default) the tone is on
both channels, so either L/R conductor works; with --channel-mode ttl the full-scale sync marker is
on ttl_channel (default right), which trips the BNC at the true buffer onset.

Each trial the 'arm' state fires a SoftCode on entry; the softcode handler plays the cue; Bpod waits
for the rising edge on BNC1 (event BNC1High). Latency = t(BNC1High) - t(arm entry), both taken on
Bpod's own hardware clock, so the host clock never enters the measurement. This is the full path the
animal experiences: Bpod -> USB softcode -> host handler -> (IPC ->) PortAudio -> DAC -> analog -> BNC.

Backends (this is the A/B that answers "is the isolated server any worse?"):
  --backend server      SoundServerClient  (isolated subprocess)   [default]
  --backend inprocess   StereoSound         (today's in-process model, baseline)

Run:
  uv run python scripts/measure_sound_latency_bpod.py --port /dev/ttyACM2 --backend server -n 200
  uv run python scripts/measure_sound_latency_bpod.py --port /dev/ttyACM2 --backend inprocess -n 200
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time


def _pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _build_sound(backend, sound_device, channel_mode):
    if backend == "server":
        from murineshiftwork.logic.sound_server import SoundServerClient

        # opens PortAudio in the subprocess
        return SoundServerClient(sound_device=sound_device, channel_mode=channel_mode)
    from murineshiftwork.logic.sounds import StereoSound

    snd = StereoSound(sound_device=sound_device, channel_mode=channel_mode)
    snd.setup_sound_device()
    return snd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/ttyACM2", help="Bpod serial port")
    ap.add_argument("--backend", choices=["server", "inprocess"], default="server")
    ap.add_argument("-n", type=int, default=200, help="trials to measure")
    ap.add_argument("--freq", type=float, default=2000.0, help="cue frequency (Hz)")
    ap.add_argument("--duration", type=float, default=0.05, help="cue duration (s)")
    ap.add_argument(
        "--amplitude", type=float, default=1.0, help="cue amplitude (full scale = 1.0)"
    )
    ap.add_argument(
        "--timeout", type=float, default=0.5, help="per-trial wait for BNC1High (s)"
    )
    ap.add_argument("--iti", type=float, default=0.15, help="inter-trial gap (s)")
    ap.add_argument(
        "--sound-device",
        default=None,
        help="device name substring (None = default out)",
    )
    ap.add_argument("--channel-mode", choices=["both", "ttl"], default="both")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    from pybpodapi.state_machine import StateMachine

    from murineshiftwork.hardware.bpod.factory import BpodFactory

    sound = _build_sound(args.backend, args.sound_device, args.channel_mode)
    cue = sound.register_new_sound(
        frequency=args.freq, duration=args.duration, amplitude=args.amplitude
    )
    print(
        f"backend={args.backend} channel_mode={args.channel_mode} "
        f"cue_code={cue} freq={args.freq}Hz amp={args.amplitude}"
    )

    bpod = BpodFactory(
        serial_port=args.port, workspace_path=None
    )  # workspace None: GC-segfault-safe
    bpod.open()
    bpod.softcode_handler_function = lambda code: sound.execute_sound_handler(
        sound_code=code
    )

    lats_ms: list[float] = []
    misses = 0
    try:
        for i in range(args.n):
            sma = StateMachine(bpod)
            sma.add_state(
                state_name="arm",
                state_timer=args.timeout,
                state_change_conditions={"BNC1High": "exit", "Tup": "exit"},
                output_actions=[("SoftCode", cue)],
            )
            bpod.send_state_machine(sma)
            bpod.run_state_machine(sma)

            trial = bpod.session.current_trial.export()
            arm_enter = trial["States timestamps"]["arm"][0][0]
            highs = trial["Events timestamps"].get("BNC1High")
            if highs:
                lats_ms.append((highs[0] - arm_enter) * 1000.0)
            else:
                misses += 1
            time.sleep(args.iti)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{args.n} measured, {misses} misses", flush=True)
    finally:
        with __import__("contextlib").suppress(Exception):
            sound.close()
        with __import__("contextlib").suppress(Exception):
            bpod.close_safely()

    print(f"\nbackend  : {args.backend}")
    print(f"trials   : {args.n}  hits={len(lats_ms)}  misses={misses}")
    if not lats_ms:
        print(
            "NO BNC1High detected on any trial - the analog tone never crossed the BNC threshold."
        )
        print("Try: raise system volume / --amplitude, or lower the tone --freq.")
        return 2
    lats_ms.sort()
    print(f"min      : {lats_ms[0]:.3f} ms")
    print(f"p50      : {_pct(lats_ms, 0.50):.3f} ms")
    print(f"mean     : {statistics.fmean(lats_ms):.3f} ms")
    print(f"p95      : {_pct(lats_ms, 0.95):.3f} ms")
    print(f"p99      : {_pct(lats_ms, 0.99):.3f} ms")
    print(f"max      : {lats_ms[-1]:.3f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
