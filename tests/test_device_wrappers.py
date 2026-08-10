"""DeviceProtocol wrappers for stage and scale (boundary rework, Step 1).

Wrappers add the name/preflight/connect/disconnect/handle surface over the existing drivers
(one_axis_stage.StageController, hardware.scale.make_scale). Tested with the drivers mocked, so no
hardware is needed; verifies each wrapper satisfies DeviceProtocol and drives the real construction
path (StageController.from_config with the port injected; make_scale + .start()/.stop()).
"""

from __future__ import annotations

import sys
import types

import pytest

from murineshiftwork.hardware.manager import DeviceProtocol, ManagedDevice
from murineshiftwork.hardware.scale import ScaleDevice
from murineshiftwork.hardware.stage import StageDevice

# --------------------------------------------------------------------------- #
# all wrappers satisfy the structural DeviceProtocol (via the ManagedDevice base)


def test_wrappers_satisfy_device_protocol():
    from murineshiftwork.hardware.bpod.device import BpodDevice
    from murineshiftwork.hardware.pulsepal.device import PulsePalDevice

    for cls, key in (
        (StageDevice, "stage"),
        (ScaleDevice, "scale"),
        (BpodDevice, "bpod"),
        (PulsePalDevice, "pulsepal"),
    ):
        dev = cls("/dev/ttyX")
        assert isinstance(dev, DeviceProtocol)
        assert isinstance(dev, ManagedDevice)
        assert dev.name == key


# --------------------------------------------------------------------------- #
# ManagedDevice base lifecycle


def test_managed_device_base_lifecycle(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: True
    )
    events: list[str] = []

    class _Dev(ManagedDevice):
        name = "toy"

        def _open(self):
            events.append("open")
            return "HANDLE"

        def _close(self, handle):
            events.append(f"close:{handle}")

    dev = _Dev("/dev/ttyX")
    dev.preflight()
    with pytest.raises(RuntimeError, match="not connected"):
        _ = dev.handle  # guarded before connect
    dev.connect()
    assert dev.handle == "HANDLE"
    dev.disconnect()
    assert events == ["open", "close:HANDLE"]
    with pytest.raises(RuntimeError):
        _ = dev.handle  # nulled after disconnect


def test_managed_device_disconnect_is_best_effort():
    class _Dev(ManagedDevice):
        name = "toy"

        def _open(self):
            return object()

        def _close(self, handle):
            raise OSError("teardown blew up")

    dev = _Dev("/x")
    dev.connect()
    dev.disconnect()  # must not raise despite _close raising
    with pytest.raises(RuntimeError):
        _ = dev.handle


# --------------------------------------------------------------------------- #
# StageDevice


def _install_fake_stage(monkeypatch):
    """Install a fake one_axis_stage.controller module; return the recorder."""
    calls: dict = {}

    class _FakeStage:
        def __init__(self):
            self.api = types.SimpleNamespace(
                connection=types.SimpleNamespace(
                    disconnect=lambda: calls.__setitem__("disconnected", True)
                )
            )

        @classmethod
        def from_config(cls, cfg):
            calls["cfg"] = cfg
            return cls()

    mod = types.ModuleType("one_axis_stage.controller")
    mod.StageController = _FakeStage
    pkg = types.ModuleType("one_axis_stage")
    monkeypatch.setitem(sys.modules, "one_axis_stage", pkg)
    monkeypatch.setitem(sys.modules, "one_axis_stage.controller", mod)
    return calls


def test_stage_connect_uses_from_config_with_injected_port(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: True
    )
    calls = _install_fake_stage(monkeypatch)
    dev = StageDevice(
        "/dev/ttyUSB2",
        controller_config={"connection": {"baudrate": 115200}, "axes": {"y": {}}},
    )
    dev.preflight()
    dev.connect()
    # from_config got the config with the resolved serial port injected into connection
    assert calls["cfg"]["connection"]["serial_port"] == "/dev/ttyUSB2"
    assert calls["cfg"]["connection"]["baudrate"] == 115200  # existing keys preserved
    assert calls["cfg"]["axes"] == {"y": {}}
    assert dev.handle is not None
    dev.disconnect()
    assert calls["disconnected"] is True


def test_stage_preflight_raises_on_missing_port(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: False
    )
    with pytest.raises(ValueError, match="serial port not accessible"):
        StageDevice("/nope").preflight()


def test_stage_handle_before_connect_raises():
    with pytest.raises(RuntimeError, match="not connected"):
        _ = StageDevice("/x").handle


# --------------------------------------------------------------------------- #
# ScaleDevice


class _FakeScale:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_scale_connect_uses_make_scale_and_starts(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: True
    )
    seen: dict = {}
    fake = _FakeScale()

    def _fake_make_scale(serial_port, scale_type, baudrate, protocol):
        seen.update(
            serial_port=serial_port,
            scale_type=scale_type,
            baudrate=baudrate,
            protocol=protocol,
        )
        return fake

    monkeypatch.setattr("murineshiftwork.hardware.scale.make_scale", _fake_make_scale)
    dev = ScaleDevice("/dev/ttyUSB1", scale_type="bench", baudrate=9600, protocol=2)
    dev.preflight()
    dev.connect()
    assert seen == {
        "serial_port": "/dev/ttyUSB1",
        "scale_type": "bench",
        "baudrate": 9600,
        "protocol": 2,
    }
    assert fake.started is True
    assert dev.handle is fake
    dev.disconnect()
    assert fake.stopped is True


def test_scale_sim_type_skips_port_preflight(monkeypatch):
    # a sim scale needs no serial port -> preflight must not raise even with no port present
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: False
    )
    ScaleDevice("", scale_type="sim").preflight()  # no raise


def test_scale_preflight_raises_on_missing_port(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.hardware.manager.serial_port_present", lambda p: False
    )
    with pytest.raises(ValueError, match="serial port not accessible"):
        ScaleDevice("/nope", scale_type="hx711").preflight()


# --------------------------------------------------------------------------- #
# BpodDevice forwards the pybpod workspace (set after construction, once the session folder exists)


def test_bpod_device_forwards_workspace_to_factory(monkeypatch):
    from murineshiftwork.hardware.bpod.device import BpodDevice

    captured: dict = {}

    class _FakeFactory:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def open(self):
            pass

    monkeypatch.setattr(
        "murineshiftwork.hardware.bpod.device.BpodFactory", _FakeFactory
    )
    dev = BpodDevice("/dev/ttyACM0")
    dev.set_workspace("/sessions/s1", "s1.msw")
    dev.connect()

    assert captured["serial_port"] == "/dev/ttyACM0"
    assert captured["workspace_path"] == "/sessions/s1"
    assert captured["session_name"] == "s1.msw"


def test_bpod_device_defaults_workspace_to_none_when_unset(monkeypatch):
    from murineshiftwork.hardware.bpod.device import BpodDevice

    captured: dict = {}

    class _FakeFactory:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def open(self):
            pass

    monkeypatch.setattr(
        "murineshiftwork.hardware.bpod.device.BpodFactory", _FakeFactory
    )
    BpodDevice("/dev/ttyACM0").connect()  # no set_workspace call
    assert captured["workspace_path"] is None
    assert captured["session_name"] is None
