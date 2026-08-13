"""Crash-isolated sound output: PortAudio runs in a subprocess so a device segfault or hang
cannot take the session down with it.

A task uses :class:`SoundServerClient` exactly like :class:`~murineshiftwork.logic.sounds.StereoSound`
(``register_new_sound`` / ``execute_sound_handler`` / ``close`` / ``sounds``). The client owns a
spawned subprocess that holds the real ``StereoSound`` (and therefore PortAudio); commands cross a
``multiprocessing.Queue``. A native crash in PortAudio kills only the subprocess - the client detects
that (``proc.is_alive()``) and degrades to no-sound rather than propagating the fault. A native
segfault cannot be caught by ``try/except`` in-process, so subprocess isolation is the only real
defense.

Latency design (target: <=5 ms added trigger latency, p99, on Unix and Windows):

* ``spawn`` start method - required on Windows, and keeps the child free of inherited PortAudio/Qt
  state on Unix. One-time startup cost only.
* the server blocks on ``Queue.get()`` - event-driven wakeup, so it does not ride the ~15.6 ms
  Windows default timer granularity that a polling loop would.
* ``timeBeginPeriod(1)`` via ctypes + an elevated process priority on Windows; best-effort ``nice``
  on Unix - tighten scheduler wakeup so the play trigger is delivered promptly.
* cues are pre-registered and the waveform is built inside the server (the client sends only the
  small scalar spec), so ``execute_sound_handler`` is a fire-and-forget integer ``put``.

The added latency measured here is the client-``put`` -> server-``get`` hop. ``time.perf_counter`` is
backed by a system-wide monotonic clock on both Linux (``CLOCK_MONOTONIC``) and Windows
(``QueryPerformanceCounter``), so a timestamp taken in the client and one taken in the server are
directly comparable and their difference is a valid cross-process latency.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import os
import sys
import time

log = logging.getLogger(__name__)

# Command tags on the client -> server queue.
_PLAY = "play"
_REGISTER = "register"
_SHUTDOWN = None  # sentinel

# Reply tags on the server -> client queue.
_CODE = "code"
_LATENCY = "lat"

_REGISTER_TIMEOUT_S = (
    15.0  # generous: covers spawn + PortAudio warm-up on first register
)


def _tighten_scheduler() -> None:
    """Best-effort: raise process priority / tighten the timer so a play trigger isn't delayed.

    Every step is guarded - none is required for correctness, only for jitter. On Unix ``nice(-n)``
    needs privilege and will simply fail (leaving default priority) for an unprivileged user.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes

            # 1 ms timer/scheduler quantum (vs the ~15.6 ms default) for the lifetime of the process.
            ctypes.windll.winmm.timeBeginPeriod(1)
            high_priority_class = 0x00000080
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), high_priority_class)
        except Exception:
            log.debug(
                "sound server: could not raise Windows priority/timer", exc_info=True
            )
    else:
        with contextlib.suppress(Exception):
            os.nice(-10)


def _server_main(
    cmd_q, result_q, sound_device, measure, open_audio, channel_mode
) -> None:
    """Subprocess entry point: own the real ``StereoSound``, block on the command queue, play on trigger.

    ``measure`` makes the server reply to every play trigger with the client-put -> server-recv
    latency (for the benchmark). ``open_audio`` controls whether PortAudio is opened: normal sessions
    pass ``open_audio=True``; the pure-IPC benchmark passes ``False`` so it needs no sound card, while
    ``--real`` passes both True to measure the get-loop with the live PortAudio callback thread active.
    """
    _tighten_scheduler()

    sound = None
    if open_audio:
        try:
            from murineshiftwork.logic.sounds import StereoSound

            sound = StereoSound(sound_device=sound_device, channel_mode=channel_mode)
            sound.setup_sound_device()
        except Exception:
            log.exception(
                "sound server: failed to open sound device; running WITHOUT sound"
            )
            sound = None

    while True:
        cmd = cmd_q.get()  # BLOCKING - event-driven wakeup, no polling
        if cmd is _SHUTDOWN:
            break

        tag = cmd[0]
        if tag == _PLAY:
            _, sound_code, put_ts = cmd
            if measure:
                result_q.put((_LATENCY, time.perf_counter() - put_ts))
            if sound is not None:
                try:
                    sound.execute_sound_handler(sound_code=sound_code)
                except Exception:
                    log.warning(
                        "sound server: play failed for code %r",
                        sound_code,
                        exc_info=True,
                    )
        elif tag == _REGISTER:
            spec = cmd[1]
            if sound is not None:
                try:
                    code = sound.register_new_sound(**spec)
                except Exception:
                    log.exception("sound server: register_new_sound failed")
                    code = None
            else:
                # measure/degraded mode: hand back a deterministic 1-indexed code so the client's
                # sound registry stays consistent with what a real StereoSound would have returned.
                code = spec.get("_index", 1)
            result_q.put((_CODE, code))

    if sound is not None:
        with contextlib.suppress(Exception):
            sound.close()


class SoundServerClient:
    """Drop-in replacement for :class:`StereoSound` that runs PortAudio in an isolated subprocess.

    Mirrors the surface the tasks use: ``register_new_sound`` (blocking request/reply so the caller
    gets the same 1-indexed code back), ``execute_sound_handler`` (fire-and-forget trigger),
    ``close``, and the ``sounds`` property. If the subprocess dies, every subsequent call degrades to
    a no-op (logged once) instead of raising - a dead sound device must not stop a session.
    """

    default_sound_device = "XONAR SOUND CARD"  # mirror StereoSound.default_sound_device

    def __init__(
        self,
        sound_device: str | None = None,
        measure: bool = False,
        open_audio: bool | None = None,
        channel_mode: str | None = None,
    ) -> None:
        self._measure = measure
        # normal use opens PortAudio; the pure-IPC benchmark (measure=True) skips it unless asked
        if open_audio is None:
            open_audio = not measure
        self._sounds: dict = {}  # local mirror of registered codes (parity with StereoSound.sounds)
        self._alive = True
        self._warned_dead = False

        ctx = mp.get_context("spawn")
        self._cmd_q = ctx.Queue()
        self._result_q = ctx.Queue()
        self._proc = ctx.Process(
            target=_server_main,
            args=(
                self._cmd_q,
                self._result_q,
                sound_device,
                measure,
                open_audio,
                channel_mode,
            ),
            name="msw-sound-server",
            daemon=True,
        )
        self._proc.start()

    # --- health ---------------------------------------------------------------------------------
    def _check_alive(self) -> bool:
        if self._alive and not self._proc.is_alive():
            self._alive = False
        if not self._alive and not self._warned_dead:
            log.error(
                "sound server subprocess is not running (exit=%s); continuing WITHOUT sound",
                self._proc.exitcode,
            )
            self._warned_dead = True
        return self._alive

    @property
    def is_alive(self) -> bool:
        return self._check_alive()

    @property
    def sounds(self) -> dict:
        return self._sounds

    # --- StereoSound-compatible surface ---------------------------------------------------------
    def register_new_sound(self, **spec):
        """Register a cue in the server and return its 1-indexed code (mirrors StereoSound)."""
        index = len(self._sounds) + 1
        if not self._check_alive():
            # keep the local index advancing so codes stay stable even while degraded
            self._sounds[index] = spec
            return index
        spec.setdefault("_index", index)
        self._cmd_q.put((_REGISTER, spec))
        try:
            tag, code = self._result_q.get(timeout=_REGISTER_TIMEOUT_S)
        except Exception:
            log.error("sound server: register timed out; cue %d will be silent", index)
            code = index
        self._sounds[code] = spec
        return code

    def execute_sound_handler(
        self, sound_code=None, raise_errors: bool = False
    ) -> None:
        """Fire-and-forget play trigger (mirrors StereoSound.execute_sound_handler)."""
        if not self._check_alive():
            return
        # timestamp at the client so the server can report the added trigger latency in measure mode
        self._cmd_q.put((_PLAY, sound_code, time.perf_counter()))

    def setup_sound_device(self) -> None:
        """No-op on the client: the server sets up PortAudio at startup. Kept for interface parity."""

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._cmd_q.put(_SHUTDOWN)
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)

    # --- benchmarking ---------------------------------------------------------------------------
    def measure_trigger_latency(self, n: int = 2000, warmup: int = 200):
        """Fire ``n`` play triggers and return the list of client-put -> server-recv latencies (s).

        Ping-pong: each trigger is timestamped at ``put``, the server timestamps at ``get`` and
        replies with the delta; the client waits for the reply before the next trigger. This measures
        the one-way added latency per trigger (the >=5 ms concern), not throughput. Requires a client
        constructed with ``measure=True``.
        """
        if not self._measure:
            raise RuntimeError(
                "measure_trigger_latency requires SoundServerClient(measure=True)"
            )
        lats: list[float] = []
        for i in range(n + warmup):
            self._cmd_q.put((_PLAY, 1, time.perf_counter()))
            _tag, lat = self._result_q.get(timeout=5)
            if (
                i >= warmup
            ):  # discard warmup (first triggers pay import/JIT/cache-cold costs)
                lats.append(lat)
        return lats
