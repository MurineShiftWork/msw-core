"""Serial-safe live-command injection into a running trial (control-plane Phase 0).

A running trial owns the Bpod serial connection on the task thread. The ONLY serial-safe way to
issue a live command mid-trial (an extra reward, a stage move) is from inside pybpodapi's
``run_state_machine`` poll loop - i.e. a :meth:`BpodFactory.add_loop_handler` callback, which runs
on that same thread. Injecting from any other thread races the state machine's own serial I/O and
corrupts the stream.

:class:`LiveInjector` implements that safely:
  - ``submit(cmd)`` is called by the control plane on ANY thread - it validates the command against a
    rig-local :class:`CommandPolicy` and enqueues it. It never touches hardware.
  - ``drain()`` is registered as a loop handler and runs on the task thread each iteration - the only
    place hardware is touched. Actions are NON-BLOCKING (open now, close on a later iteration) so the
    poll loop never stalls for a pulse.
  - applied commands are emitted for the audit log: overrides do NOT appear in the trial data, so
    recording them is a data-completeness requirement (reward accounting), not just security.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from murineshiftwork.hardware.bpod.override import BpodOverrideAPI
from murineshiftwork.logic.config.models import ActionRequest

log = logging.getLogger(__name__)


class PolicyError(ValueError):
    """A live command was rejected by the safety envelope."""


@dataclass(frozen=True)
class CommandPolicy:
    """Safety envelope for live hardware injection, ENFORCED LOCALLY by the rig.

    Loaded from the setup config so the rig never trusts a network command beyond these limits - a
    compromised or buggy control plane cannot deliver an unbounded reward or drive an unlisted valve.
    """

    allowed_actions: frozenset[str] = frozenset({"reward"})
    allowed_valves: frozenset[int] = frozenset()
    max_reward_ms: float = 500.0
    min_interval_s: float = 0.25  # rate limit between accepted commands

    def validate(self, cmd: ActionRequest) -> None:
        if cmd.action not in self.allowed_actions:
            raise PolicyError(
                f"action {cmd.action!r} not allowed (allowed: {sorted(self.allowed_actions)})"
            )
        if cmd.action == "reward":
            valve = int(cmd.params.get("valve", 0))
            ms = float(cmd.params.get("ms", 0))
            if valve not in self.allowed_valves:
                raise PolicyError(
                    f"valve {valve} not allowed (allowed: {sorted(self.allowed_valves)})"
                )
            if not 0 < ms <= self.max_reward_ms:
                raise PolicyError(f"reward {ms} ms outside (0, {self.max_reward_ms}]")


class LiveInjector:
    """Thread-safe live-command channel into a running trial; applied single-threaded via ``drain``."""

    def __init__(
        self,
        override: BpodOverrideAPI,
        policy: CommandPolicy,
        emit: Callable[[dict], None] | None = None,
    ) -> None:
        self._ov = override
        self._policy = policy
        self._emit = emit
        self._q: queue.Queue[ActionRequest] = queue.Queue()
        self._pending: list[tuple[float, int]] = []  # (close_perf_time, valve)
        self._last_accept = 0.0
        self._accept_lock = threading.Lock()  # guards the rate-limit check-and-set

    # --- external interface (ANY thread) --------------------------------------------------------
    def submit(self, cmd: ActionRequest) -> None:
        """Validate + enqueue a command. Raises :class:`PolicyError` if the envelope rejects it.

        Never touches hardware - the running trial's ``drain`` applies it. Safe from any thread.
        """
        self._policy.validate(cmd)
        # The check-and-set must be atomic: submit() may race across threads.
        with self._accept_lock:
            now = time.monotonic()
            if now - self._last_accept < self._policy.min_interval_s:
                raise PolicyError("rate limit: command too soon after the previous one")
            self._last_accept = now
        self._q.put(cmd)

    # --- task thread (registered via BpodFactory.add_loop_handler) ------------------------------
    def drain(self) -> None:
        """One run-loop iteration: close any elapsed timed action, then apply one queued command."""
        now = time.perf_counter()
        self._close_elapsed(now)
        try:
            cmd = self._q.get_nowait()
        except queue.Empty:
            return
        self._apply(cmd, now)

    def flush_pending(self) -> None:
        """Close any still-open timed valves - call between trials / on teardown."""
        self._close_elapsed(float("inf"))

    # --- internals ------------------------------------------------------------------------------
    def _close_elapsed(self, now: float) -> None:
        if not self._pending:
            return
        keep: list[tuple[float, int]] = []
        for close_t, valve in self._pending:
            if now >= close_t:
                self._ov.close_valve(valve)
                self._audit("reward_close", valve=valve)
            else:
                keep.append((close_t, valve))
        self._pending[:] = keep

    def _apply(self, cmd: ActionRequest, now: float) -> None:
        if cmd.action == "reward":
            valve = int(cmd.params["valve"])
            ms = float(cmd.params["ms"])
            self._ov.open_valve(valve)
            self._pending.append((now + ms / 1000.0, valve))
            self._audit("reward_open", valve=valve, ms=ms)

    def _audit(self, event: str, **fields: Any) -> None:
        if self._emit is None:
            return
        try:
            self._emit({"event": event, "t": time.time(), **fields})
        except Exception:
            log.warning("injector emit failed", exc_info=True)
