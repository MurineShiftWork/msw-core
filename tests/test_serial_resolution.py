"""Cross-platform serial port resolution and presence checks.

Covers the `port` (direct, e.g. COM3 / /dev/ttyACM0) vs `port_by_path` (Linux
by-path) split, bare-tty normalisation, the exactly-one validation, and the
platform-agnostic serial_port_present() preflight helper.
"""

import pytest
from pydantic import ValidationError

from murineshiftwork.logic.config.models import BpodDevice, SerialDevice
from murineshiftwork.logic.misc import serial_port_present

# --------------------------------------------------------------------------- #
# resolve_port: direct `port`


def test_direct_com_port_returned_verbatim():
    d = SerialDevice(type="bpod", port="COM3")
    assert d.resolve_port() == "COM3"


def test_direct_dev_path_returned_verbatim():
    d = SerialDevice(type="bpod", port="/dev/ttyACM0")
    assert d.resolve_port() == "/dev/ttyACM0"


@pytest.mark.parametrize(
    ("bare", "expected"),
    [("ttyACM0", "/dev/ttyACM0"), ("ttyUSB1", "/dev/ttyUSB1"), ("ttyS2", "/dev/ttyS2")],
)
def test_bare_linux_tty_gets_dev_prefix(bare, expected):
    assert SerialDevice(type="bpod", port=bare).resolve_port() == expected


def test_com_port_not_treated_as_bare_tty():
    # COM3 must never be rewritten with a /dev/ prefix.
    assert SerialDevice(type="scale", port="COM3").resolve_port() == "COM3"


# --------------------------------------------------------------------------- #
# resolve_port: port_by_path (Linux)


def test_port_by_path_missing_raises_with_windows_hint():
    d = SerialDevice(
        type="bpod", port_by_path="pci-0000:00:14.0-usb-0:1:1.0-nonexistent"
    )
    with pytest.raises(ValueError, match="set 'port'"):
        d.resolve_port()


# --------------------------------------------------------------------------- #
# Exactly-one validation


def test_neither_port_source_rejected():
    with pytest.raises(ValidationError, match="exactly one"):
        SerialDevice(type="bpod")


def test_both_port_sources_rejected():
    with pytest.raises(ValidationError, match="exactly one"):
        SerialDevice(type="bpod", port="COM3", port_by_path="something")


def test_typed_subclass_accepts_direct_port():
    d = BpodDevice(type="bpod", port="COM5")
    assert d.resolve_port() == "COM5"


# --------------------------------------------------------------------------- #
# serial_port_present()


def test_present_true_for_existing_dev_node():
    assert serial_port_present("/dev/null") is True


def test_present_false_for_missing_dev_node():
    assert serial_port_present("/dev/msw_nonexistent_port_xyz") is False


def test_present_true_for_com_port_deferred():
    # Cannot filesystem-check a COM port; defer to the real open at connect time.
    assert serial_port_present("COM3") is True


def test_present_false_for_empty():
    assert serial_port_present("") is False
