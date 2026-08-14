"""BpodOverrideAPI + BpodActionDriver: firmware ``manual_override`` emission (SimBpod, no hardware).

These exercise the live hardware-control layer the planned ControllerSession / control plane uses for
extra rewards, stage/valve moves, etc. - untested before. SimBpod records every ``manual_override``
call (``override_calls()``), so we assert the API emits the right
``(ChannelType, ChannelName, channel, value)`` firmware writes. The *hardware* behaviour (a valve open
DURING a running trial's state machine) is a manual smoke: ``scripts/hw_smoke_bpod_override.py`` -
verified 2026-08-14 that a mid-trial override fires the valve without disturbing the trial, and that the
override does NOT appear in the trial's Events/States (so the control plane must log overrides itself).
"""

from __future__ import annotations

import pytest
from pybpodapi.bpod.hardware.channels import ChannelName, ChannelType

from murineshiftwork.hardware.bpod.actions import BpodActionDriver
from murineshiftwork.hardware.bpod.override import BpodOverrideAPI
from murineshiftwork.hardware.bpod.sim import SimBpod
from murineshiftwork.logic.config.models import ActionRequest


def _api():
    bpod = SimBpod()
    return bpod, BpodOverrideAPI(bpod)


def _overrides(bpod):
    return [c[1:] for c in bpod.override_calls()]  # (type, name, channel, value)


def test_open_and_close_valve_emit_output_overrides():
    bpod, ov = _api()
    ov.open_valve(2)
    ov.close_valve(2)
    assert _overrides(bpod) == [
        (ChannelType.OUTPUT, ChannelName.VALVE, 2, 1),
        (ChannelType.OUTPUT, ChannelName.VALVE, 2, 0),
    ]


def test_reward_pulses_open_then_close():
    bpod, ov = _api()
    ov.reward(port=1, duration_ms=1, blocking=True)
    assert _overrides(bpod) == [
        (ChannelType.OUTPUT, ChannelName.VALVE, 1, 1),
        (ChannelType.OUTPUT, ChannelName.VALVE, 1, 0),
    ]


def test_close_all_valves_closes_every_port():
    bpod, ov = _api()
    ov.close_all_valves(n_ports=4)
    assert _overrides(bpod) == [
        (ChannelType.OUTPUT, ChannelName.VALVE, p, 0) for p in range(1, 5)
    ]


def test_bnc_and_port_light():
    bpod, ov = _api()
    ov.set_bnc(1, 1)
    ov.set_port_light(3, 128)
    calls = _overrides(bpod)
    assert (ChannelType.OUTPUT, ChannelName.BNC, 1, 1) in calls
    assert (ChannelType.OUTPUT, ChannelName.PWM, 3, 128) in calls


def test_override_acquires_write_lock_when_present():
    """Overrides serialise against other serial writes via bpod._write_lock (ControllerSession)."""
    bpod = SimBpod()
    order: list[str] = []

    class _RecordingLock:
        def __enter__(self):
            order.append("acquire")
            return self

        def __exit__(self, *exc):
            order.append("release")

    bpod._write_lock = _RecordingLock()
    BpodOverrideAPI(bpod).open_valve(1)
    assert order == ["acquire", "release"]


def test_override_works_without_write_lock():
    bpod = SimBpod()  # SimBpod has no _write_lock; the API must degrade gracefully
    assert getattr(bpod, "_write_lock", None) is None
    BpodOverrideAPI(bpod).set_bnc(2, 1)
    assert _overrides(bpod) == [(ChannelType.OUTPUT, ChannelName.BNC, 2, 1)]


def test_action_driver_valve_pulse_emits_n_pulses():
    bpod = SimBpod()
    BpodActionDriver(bpod).dispatch(
        ActionRequest(
            setup="setup-test",
            device="bpod",
            action="valve_pulse",
            params={
                "valve_id": 2,
                "duration_s": 0.001,
                "n_pulses": 3,
                "inter_pulse_s": 0.0,
            },
        )
    )
    calls = _overrides(bpod)
    assert len([c for c in calls if c[3] == 1]) == 3  # one open per pulse
    assert all(c[:3] == (ChannelType.OUTPUT, ChannelName.VALVE, 2) for c in calls)
    assert calls[-1][3] == 0  # ends with the valve closed (defensive final close)


def test_action_driver_rejects_unknown_action():
    drv = BpodActionDriver(SimBpod())
    with pytest.raises(ValueError, match="Unknown action"):
        drv.dispatch(ActionRequest(setup="s", device="bpod", action="nope", params={}))
