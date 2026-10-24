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
    # preflight reads the scalar identity + ports off the RunContext (as real flow does), so build a
    # ctx carrying them; setup_config stays a plain dict key (it's not a RunContext-owned scalar).
    ctx = RunContext(
        ports=ports,
        out_path=str(tmp_path),
        subject=extra.get("subject", "s"),
        simulate=bool(extra.get("simulate", False)),
        debug=bool(extra.get("debug", False)),
    )
    d = {
        "setup_config": setup,
        "run_context": ctx,
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


@pytest.mark.parametrize(
    "extra", [{"debug": True}, {"subject": "_test_subject"}], ids=["debug", "test"]
)
def test_skipped_for_debug_and_test_subject(tmp_path, monkeypatch, extra):
    seen: list = []
    _accessible(monkeypatch, lambda port=None, **k: seen.append(port) or False)
    setup = _Setup({"bpod": _Cfg("bpod")})
    preflight_hardware_check(_args(tmp_path, setup, DevicePorts(bpod="/b"), **extra))
    assert seen == []  # bypassed before any port is probed


def test_reports_missing_camera_config_when_recording_video(tmp_path):
    d = _args(tmp_path, None, DevicePorts(), config_file_camera="missing_cam.yaml")
    d["settings.task.patched"] = {"record_video": True}
    with pytest.raises(RuntimeError, match="Camera config not found"):
        preflight_hardware_check(d)


def test_camera_config_present_passes(tmp_path):
    cam = tmp_path / "camera.yaml"
    cam.touch()
    d = _args(tmp_path, None, DevicePorts(), config_file_camera=str(cam))
    d["settings.task.patched"] = {"record_video": True}
    preflight_hardware_check(d)  # no raise


def test_reports_output_dir_that_cannot_be_created(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.touch()  # a regular file cannot hold a child directory on any OS
    with pytest.raises(RuntimeError, match="Cannot create output dir"):
        preflight_hardware_check(_args(blocker / "out", None, DevicePorts()))


def test_reports_unwritable_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.cli.preflight.test_path_is_writable", lambda p: False
    )
    with pytest.raises(RuntimeError, match="Output directory not writable"):
        preflight_hardware_check(_args(tmp_path, None, DevicePorts()))


def test_collects_all_failures_into_one_error(tmp_path, monkeypatch):
    _accessible(monkeypatch, lambda port=None, **k: False)
    monkeypatch.setattr(
        "murineshiftwork.cli.preflight.test_path_is_writable", lambda p: False
    )
    d = _args(
        tmp_path, None, DevicePorts(bpod="/b"), config_file_camera="missing_cam.yaml"
    )
    d["settings.task.patched"] = {"record_video": True}
    with pytest.raises(RuntimeError) as exc:
        preflight_hardware_check(d)
    msg = str(exc.value)
    assert "bpod serial port not accessible" in msg
    assert "Camera config not found" in msg
    assert "Output directory not writable" in msg
