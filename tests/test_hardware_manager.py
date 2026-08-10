"""HardwareManager orchestration: open order, reverse-order close, and partial-open teardown."""

from __future__ import annotations

import pytest

from murineshiftwork.hardware.manager import DeviceCollection, HardwareManager


class _FakeDevice:
    """Records lifecycle calls; can be told to fail at preflight or connect."""

    def __init__(self, name, *, fail_preflight=False, fail_connect=False):
        self.name = name
        self._fail_preflight = fail_preflight
        self._fail_connect = fail_connect
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
