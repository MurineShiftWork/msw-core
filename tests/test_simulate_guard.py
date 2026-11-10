"""A --simulate run must never open real hardware."""

from __future__ import annotations

import pytest

from murineshiftwork.hardware.bpod.factory import BpodFactory
from murineshiftwork.hardware.pulsepal.device import PulsePalDevice
from murineshiftwork.hardware.simulate_guard import (
    SimulatedHardwareError,
    is_simulated,
    mark_simulated,
)
from murineshiftwork.hardware.stage import StageDevice


def test_mark_and_clear():
    assert not is_simulated()
    mark_simulated(True)
    assert is_simulated()
    mark_simulated(False)
    assert not is_simulated()


def test_real_bpod_refused_when_simulated():
    mark_simulated(True)
    with pytest.raises(SimulatedHardwareError, match="Bpod on COM9"):
        BpodFactory(serial_port="COM9").open()


def test_real_pulsepal_refused_when_simulated():
    mark_simulated(True)
    with pytest.raises(SimulatedHardwareError, match="PulsePal"):
        PulsePalDevice("COM9")._open()


def test_sim_pulsepal_allowed_when_simulated():
    mark_simulated(True)
    assert PulsePalDevice("", simulate=True)._open() is not None


def test_real_stage_refused_when_simulated():
    pytest.importorskip("one_axis_stage.controller")
    mark_simulated(True)
    with pytest.raises(SimulatedHardwareError, match="stage"):
        StageDevice("COM9", {})._open()
