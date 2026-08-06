"""`_apply_device_port`: the shared device-port resolver behind `_resolve_setup_config_ports`.

Locks the behaviour the four device blocks (bpod/stage/scale/pulsepal) rely on: on success it
sets `serial_port_{device}` (and mirrors into `patched` only when asked); on failure it changes
nothing (the CLI value survives) and returns the ValueError so the caller warns/cleans up itself.
"""

from __future__ import annotations

from murineshiftwork.cli.evaluate import _apply_device_port


class _FakeSetup:
    """Minimal SetupConfig stand-in: device_port() returns a value or raises the given error."""

    def __init__(self, ports: dict):
        self._ports = ports

    def device_port(self, name: str) -> str:
        value = self._ports[name]
        if isinstance(value, Exception):
            raise value
        return value


def test_success_sets_key_and_patched():
    ad: dict = {}
    patched: dict = {}
    exc = _apply_device_port(
        _FakeSetup({"scale": "/dev/ttyUSB0"}), ad, patched, "scale", set_patched=True
    )
    assert exc is None
    assert ad["serial_port_scale"] == "/dev/ttyUSB0"
    assert patched["serial_port_scale"] == "/dev/ttyUSB0"


def test_success_can_skip_patched():
    # bpod resolves into args_dict only, never patched
    ad: dict = {"serial_port_bpod": "/dev/cli"}
    patched: dict = {}
    exc = _apply_device_port(
        _FakeSetup({"bpod": "/dev/resolved"}), ad, patched, "bpod", set_patched=False
    )
    assert exc is None
    assert ad["serial_port_bpod"] == "/dev/resolved"
    assert "serial_port_bpod" not in patched


def test_failure_returns_exc_and_leaves_cli_value_untouched():
    ad: dict = {"serial_port_bpod": "/dev/cli"}
    patched: dict = {}
    exc = _apply_device_port(
        _FakeSetup({"bpod": ValueError("no match")}),
        ad,
        patched,
        "bpod",
        set_patched=False,
    )
    assert isinstance(exc, ValueError)
    assert ad["serial_port_bpod"] == "/dev/cli"  # CLI value survives
    assert "serial_port_bpod" not in patched
