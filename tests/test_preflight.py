"""preflight_hardware_check: data-driven device accessibility + output/camera checks."""

from __future__ import annotations

import pytest

from murineshiftwork.cli.preflight import preflight_hardware_check
from murineshiftwork.logic.run_context import DevicePorts, RunContext


class _Cfg:
    def __init__(self, type_):
        self.type = type_


class _Setup:
    def __init__(self, devices):
        self.devices = devices

    def device_port(self, name):
        return ""  # no config-port fallback in these tests


def _accessible(monkeypatch, ok):
    monkeypatch.setattr(
        "murineshiftwork.cli.preflight.test_serial_port_is_accessible", ok
    )


def _args(tmp_path, setup, ports, **extra):
    d = {
        "setup_config": setup,
        "run_context": RunContext(ports=ports),
        "settings.task.patched": {},
        "out_path": str(tmp_path),
    }
    d.update(extra)
    return d


def test_reports_inaccessible_declared_device(tmp_path, monkeypatch):
    _accessible(monkeypatch, lambda port=None, **k: port != "/bad")
    setup = _Setup({"bpod": _Cfg("bpod"), "stage": _Cfg("stage_tower")})
    # stage resolves by NAME (type 'stage_tower' has no ports field) and is inaccessible
    ports = DevicePorts(bpod="/good", stage="/bad")
    with pytest.raises(RuntimeError, match="stage serial port not accessible"):
        preflight_hardware_check(_args(tmp_path, setup, ports))


def test_passes_when_all_declared_accessible(tmp_path, monkeypatch):
    _accessible(monkeypatch, lambda port=None, **k: True)
    setup = _Setup({"bpod": _Cfg("bpod"), "pulsepal": _Cfg("pulsepal")})
    preflight_hardware_check(
        _args(tmp_path, setup, DevicePorts(bpod="/b", pulsepal="/p"))
    )  # no raise


def test_skipped_in_simulate(tmp_path, monkeypatch):
    seen: list = []
    _accessible(monkeypatch, lambda port=None, **k: seen.append(port) or True)
    setup = _Setup({"bpod": _Cfg("bpod")})
    preflight_hardware_check(
        _args(tmp_path, setup, DevicePorts(bpod="/b"), simulate=True)
    )
    assert seen == []  # skipped entirely, no port probed


def test_no_setup_still_checks_cli_bpod(tmp_path, monkeypatch):
    _accessible(monkeypatch, lambda port=None, **k: False)
    d = {
        "setup_config": None,
        "settings.task.patched": {},
        "out_path": str(tmp_path),
        "serial_port_bpod": "/b",
    }
    with pytest.raises(RuntimeError, match="bpod serial port not accessible"):
        preflight_hardware_check(d)
