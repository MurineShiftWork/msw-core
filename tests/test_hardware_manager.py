"""HardwareManager orchestration: open order, reverse-order close, and partial-open teardown."""

from __future__ import annotations

import pytest

from murineshiftwork.hardware.manager import (
    DeviceCollection,
    DeviceProtocol,
    HardwareManager,
)


class _FakeDevice:
    """Records lifecycle calls; can be told to fail at preflight, connect or disconnect."""

    def __init__(
        self, name, *, fail_preflight=False, fail_connect=False, fail_disconnect=False
    ):
        self.name = name
        self._fail_preflight = fail_preflight
        self._fail_connect = fail_connect
        self._fail_disconnect = fail_disconnect
        self.calls: list[str] = []

    def preflight(self):
        self.calls.append("preflight")
        if self._fail_preflight:
            raise ValueError(f"{self.name} preflight failed")

    def connect(self):
        self.calls.append("connect")
        if self._fail_connect:
            raise RuntimeError(f"{self.name} connect failed")

    def disconnect(self):
        self.calls.append("disconnect")
        if self._fail_disconnect:
            raise RuntimeError(f"{self.name} disconnect failed")

    @property
    def handle(self):
        return f"{self.name}_handle"


def test_open_preflights_then_connects_each_and_returns_collection():
    a, b = _FakeDevice("bpod"), _FakeDevice("scale")
    with HardwareManager([a, b]) as devices:
        assert isinstance(devices, DeviceCollection)
        assert devices["bpod"] == "bpod_handle"
        assert devices["scale"] == "scale_handle"
        assert a.calls == ["preflight", "connect"]
        assert b.calls == ["preflight", "connect"]


def test_close_disconnects_in_reverse_order():
    order: list[str] = []
    a, b = _FakeDevice("bpod"), _FakeDevice("scale")
    a.disconnect = lambda: order.append("bpod")  # type: ignore[method-assign]
    b.disconnect = lambda: order.append("scale")  # type: ignore[method-assign]
    with HardwareManager([a, b]):
        pass
    assert order == ["scale", "bpod"]  # reverse of open order


def test_partial_open_tears_down_already_opened_on_connect_failure():
    a = _FakeDevice("bpod")
    b = _FakeDevice("scale", fail_connect=True)
    with pytest.raises(RuntimeError, match="scale connect failed"):
        HardwareManager([a, b]).open()
    # the device opened before the failure must have been disconnected, not leaked
    assert a.calls == ["preflight", "connect", "disconnect"]


def test_preflight_failure_aborts_before_connecting_anything():
    a = _FakeDevice("bpod", fail_preflight=True)
    b = _FakeDevice("scale")
    with pytest.raises(ValueError, match="bpod preflight failed"):
        HardwareManager([a, b]).open()
    assert a.calls == ["preflight"]  # never connected
    assert b.calls == []  # never reached


def test_context_manager_closes_on_exit():
    dev = _FakeDevice("bpod")
    with HardwareManager([dev]) as devices:
        assert "bpod" in devices
        assert dev.calls == ["preflight", "connect"]
    assert dev.calls == ["preflight", "connect", "disconnect"]


def test_context_manager_closes_when_body_raises():
    dev = _FakeDevice("bpod")
    with pytest.raises(KeyError), HardwareManager([dev]):
        raise KeyError("task blew up")
    assert dev.calls[-1] == "disconnect"


def test_disconnect_failure_is_swallowed_and_others_still_close():
    # reverse-order close: "scale" disconnects first and fails, "bpod" must still close
    a = _FakeDevice("bpod")
    b = _FakeDevice("scale", fail_disconnect=True)
    mgr = HardwareManager([a, b])
    mgr.open()
    mgr.close()  # must not raise
    assert b.calls[-1] == "disconnect"
    assert a.calls[-1] == "disconnect"


def test_close_forgets_opened_devices():
    dev = _FakeDevice("bpod")
    mgr = HardwareManager([dev])
    mgr.open()
    mgr.close()
    mgr.close()  # a second close does not disconnect again
    assert dev.calls.count("disconnect") == 1


def test_empty_device_list_opens_to_empty_collection():
    with HardwareManager([]) as devices:
        assert isinstance(devices, DeviceCollection)
        assert devices == {}


# --------------------------------------------------------------------------- #
# DeviceProtocol is structural: all four members are required


def test_fake_device_satisfies_protocol():
    assert isinstance(_FakeDevice("x"), DeviceProtocol)


def test_object_without_members_fails_protocol():
    assert not isinstance(object(), DeviceProtocol)


def test_missing_handle_fails_protocol():
    class _NoHandle:
        name = "x"

        def preflight(self): ...
        def connect(self): ...
        def disconnect(self): ...

    assert not isinstance(_NoHandle(), DeviceProtocol)
