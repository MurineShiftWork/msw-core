"""LiveInjector + CommandPolicy + BpodFactory loop-handler registry (SimBpod, no hardware).

Covers the leaf of the control-plane live-injection design: policy enforcement, thread-safe
submit()/task-thread drain() split, non-blocking timed valve action, audit emit, and that
BpodFactory dispatches registered per-iteration loop handlers.
"""

from __future__ import annotations

import time
import types

import pytest
from pybpodapi.bpod.hardware.channels import ChannelName, ChannelType

from murineshiftwork.hardware.bpod.factory import BpodFactory
from murineshiftwork.hardware.bpod.injection import (
    CommandPolicy,
    LiveInjector,
    PolicyError,
)
from murineshiftwork.hardware.bpod.override import BpodOverrideAPI
from murineshiftwork.hardware.bpod.sim import SimBpod
from murineshiftwork.logic.config.models import ActionRequest


def _reward(valve=2, ms=20.0):
    return ActionRequest(
        setup="s", device="bpod", action="reward", params={"valve": valve, "ms": ms}
    )


def _injector(policy=None, emit=None):
    bpod = SimBpod()
    pol = policy or CommandPolicy(allowed_valves=frozenset({2}), min_interval_s=0.0)
    return bpod, LiveInjector(BpodOverrideAPI(bpod), pol, emit=emit)


# --- CommandPolicy --------------------------------------------------------------------------------
def test_policy_accepts_allowed_reward():
    CommandPolicy(allowed_valves=frozenset({2})).validate(_reward(2, 20))  # no raise


def test_policy_rejects_unlisted_action():
    with pytest.raises(PolicyError, match="not allowed"):
        CommandPolicy().validate(
            ActionRequest(setup="s", device="bpod", action="launch_missile")
        )


def test_policy_rejects_unlisted_valve_and_overlong_reward():
    pol = CommandPolicy(allowed_valves=frozenset({2}), max_reward_ms=100)
    with pytest.raises(PolicyError, match="valve 7 not allowed"):
        pol.validate(_reward(7, 20))
    with pytest.raises(PolicyError, match="outside"):
        pol.validate(_reward(2, 500))


# --- submit (external thread): validate + rate-limit, no hardware ---------------------------------
def test_submit_validates_and_enqueues_without_touching_hardware():
    bpod, inj = _injector()
    inj.submit(_reward(2, 20))
    assert (
        bpod.override_calls() == []
    )  # nothing applied until drain() runs on the task thread


def test_submit_rejects_out_of_policy():
    _bpod, inj = _injector(policy=CommandPolicy(allowed_valves=frozenset({2})))
    with pytest.raises(PolicyError):
        inj.submit(_reward(9, 20))


def test_submit_rate_limits():
    _bpod, inj = _injector(
        policy=CommandPolicy(allowed_valves=frozenset({2}), min_interval_s=10.0)
    )
    inj.submit(_reward(2, 20))
    with pytest.raises(PolicyError, match="rate limit"):
        inj.submit(_reward(2, 20))


def test_submit_rate_limit_holds_under_concurrent_threads():
    # The rate-limit check-and-set must be atomic: many threads submitting at once must not all
    # slip through the window (the envelope is a safety limit, not a best-effort hint).
    import threading

    _bpod, inj = _injector(
        policy=CommandPolicy(allowed_valves=frozenset({2}), min_interval_s=10.0)
    )
    accepted = []
    start = threading.Barrier(8)

    def _hammer():
        start.wait()
        try:
            inj.submit(_reward(2, 20))
            accepted.append(1)
        except PolicyError:
            pass

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (
        len(accepted) == 1
    )  # exactly one wins the interval; the rest are rate-limited


# --- drain (task thread): non-blocking open now, close on a later iteration -----------------------
def test_drain_opens_then_closes_on_a_later_iteration():
    bpod, inj = _injector()
    inj.submit(_reward(valve=2, ms=1))  # 1 ms pulse
    inj.drain()  # opens valve 2, schedules the close
    opens = [c[1:] for c in bpod.override_calls()]
    assert opens == [
        (ChannelType.OUTPUT, ChannelName.VALVE, 2, 1)
    ]  # opened, not yet closed
    time.sleep(0.003)  # let the 1 ms pulse elapse
    inj.drain()  # closes valve 2 (no new command queued)
    calls = [c[1:] for c in bpod.override_calls()]
    assert calls[-1] == (ChannelType.OUTPUT, ChannelName.VALVE, 2, 0)


def test_drain_noop_when_queue_empty():
    bpod, inj = _injector()
    inj.drain()
    assert bpod.override_calls() == []


def test_flush_pending_closes_open_valve():
    bpod, inj = _injector()
    inj.submit(_reward(valve=2, ms=200))  # won't elapse during the microsecond test
    inj.drain()  # open
    inj.flush_pending()  # force-close (teardown / between trials)
    assert [c[1:] for c in bpod.override_calls()][-1] == (
        ChannelType.OUTPUT,
        ChannelName.VALVE,
        2,
        0,
    )


def test_audit_emit_records_open_and_close():
    events: list[dict] = []
    bpod, inj = _injector(emit=events.append)
    inj.submit(_reward(valve=2, ms=1))
    inj.drain()
    time.sleep(0.003)
    inj.drain()
    kinds = [e["event"] for e in events]
    assert kinds == ["reward_open", "reward_close"]
    assert events[0]["valve"] == 2 and events[0]["ms"] == 1


# --- BpodFactory loop-handler registry ------------------------------------------------------------
def test_add_loop_handler_dispatches_all_and_suppresses_errors():
    f = BpodFactory(serial_port="")
    f._bpod = (
        types.SimpleNamespace()
    )  # stand in for an opened pybpod Bpod (settable loop_handler)
    calls: list[str] = []
    f.add_loop_handler(lambda: calls.append("a"))

    def _boom():
        raise RuntimeError("handler blew up")

    f.add_loop_handler(_boom)  # must not break the loop
    f.add_loop_handler(lambda: calls.append("b"))

    assert (
        f._bpod.loop_handler == f._dispatch_loop_handlers
    )  # routed through the registry
    f._bpod.loop_handler()  # one simulated run-loop iteration (as pybpodapi would call it)
    assert calls == ["a", "b"]  # both ran; the raiser was suppressed
