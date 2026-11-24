"""BpodFactory device-detection: behaviour-port count drives 4- vs 8-port config.

The board model is read from the hardware descriptor the device reports during
handshake (count of 'P' channels), not assumed from the settings module - so an
8-port board that opens cleanly under 4-port settings is still detected as 8.
"""

import logging
from types import SimpleNamespace

import pytest

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


# --- connect retries: the first handshake after the DTR reset reads boot bytes ---


def _failing_factory(monkeypatch, errors):
    factory = BpodFactory(
        serial_port="COM9", connect_retries=len(errors), retry_delay_s=0
    )
    it = iter(errors)

    def _create():
        raise next(it)

    monkeypatch.setattr(factory, "_create_bpod_object", _create)
    return factory


def _levels(caplog, text):
    return [r.levelno for r in caplog.records if text in r.getMessage()]


def _decode_error():
    return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def test_first_handshake_garbage_is_debug_later_attempts_warn(monkeypatch, caplog):
    factory = _failing_factory(monkeypatch, [_decode_error(), _decode_error()])
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        factory.open()
    assert _levels(caplog, "failed: UnicodeDecodeError") == [
        logging.DEBUG,
        logging.WARNING,
    ]
    assert _levels(caplog, "connect attempt 2/2 on") == [logging.INFO]


def test_other_first_attempt_errors_still_warn(monkeypatch, caplog):
    factory = _failing_factory(monkeypatch, [OSError("port busy")])
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        factory.open()
    assert _levels(caplog, "failed: OSError") == [logging.WARNING]
