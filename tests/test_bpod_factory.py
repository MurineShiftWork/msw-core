"""BpodFactory device-detection: behaviour-port count drives 4- vs 8-port config.

The board model is read from the hardware descriptor the device reports during
handshake (count of 'P' channels), not assumed from the settings module - so an
8-port board that opens cleanly under 4-port settings is still detected as 8.
"""

from types import SimpleNamespace

from murineshiftwork.hardware.bpod.factory import BpodFactory


def _bpod_with_inputs(inputs):
    return SimpleNamespace(_hardware=SimpleNamespace(inputs=inputs))


def test_counts_four_behaviour_ports():
    bpod = _bpod_with_inputs(["X", "P", "P", "P", "P", "B", "B", "W", "W"])
    assert BpodFactory._behavior_port_count(bpod) == 4


def test_counts_eight_behaviour_ports():
    bpod = _bpod_with_inputs(["X", *(["P"] * 8), "B", "B"])
    assert BpodFactory._behavior_port_count(bpod) == 8


def test_zero_when_hardware_absent():
    assert BpodFactory._behavior_port_count(SimpleNamespace(_hardware=None)) == 0


def test_zero_when_inputs_none():
    bpod = SimpleNamespace(_hardware=SimpleNamespace(inputs=None))
    assert BpodFactory._behavior_port_count(bpod) == 0
