"""preflight_hardware_check: data-driven device accessibility + output/camera checks."""

from __future__ import annotations

import pytest
from msw_plugin_api.camera import CameraConfigError

from murineshiftwork.cli.preflight import preflight_hardware_check
from murineshiftwork.logic.config.models import CameraGroup
from murineshiftwork.logic.run_context import DevicePorts, RunContext


class _Cfg:
    def __init__(self, type_):
        self.type = type_


class _Setup:
    def __init__(self, devices, cameras=None):
        self.devices = devices
        self.cameras = cameras

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


class _Backend:
    """A camera backend that refuses ``{"bad": True}`` settings."""

    acq_type, version = "video_x", "1"
    configured: list = []

    def configure(self, settings, units):
        _Backend.configured.append((dict(settings), [u.name for u in units]))
        if settings.get("bad"):
            raise CameraConfigError("bad settings")

    def preflight(self):
        raise AssertionError("pre-flight must not touch a camera")

    start = stop = preflight

    def close(self):
        pass


class _EP:
    def __init__(self, name, broken=False):
        self.name, self.value, self.broken = name, f"fake:{name}", broken

    def load(self):
        if self.broken:
            raise ModuleNotFoundError("No module named 'aiohttp'")
        return _Backend


@pytest.fixture
def installed(monkeypatch):
    """Pretend only the ``flir_bonsai`` camera backend is installed."""
    _Backend.configured = []
    eps = [_EP("flir_bonsai")]
    monkeypatch.setattr("msw_plugin_api.camera.entry_points", lambda group: eps)
    monkeypatch.delenv("BONSAI_EXE", raising=False)
    return eps


def _groups(*groups):
    return [CameraGroup.model_validate(g) for g in groups]


def _video(tmp_path, *groups, **extra):
    d = _args(tmp_path, None, DevicePorts(), **extra)
    d["camera_groups"] = _groups(*groups)
    d["settings.task.patched"] = {"record_video": True}
    return d


def test_reports_missing_camera_config_when_recording_video(tmp_path, installed):
    d = _args(tmp_path, None, DevicePorts(), config_file_camera="missing_cam.yaml")
    d["settings.task.patched"] = {"record_video": True}
    with pytest.raises(RuntimeError, match="Camera config not found"):
        preflight_hardware_check(d)


def test_video_without_video_cameras_is_reported(tmp_path, installed):
    d = _args(tmp_path, None, DevicePorts())
    d["settings.task.patched"] = {"record_video": True}
    with pytest.raises(RuntimeError, match="no video cameras"):
        preflight_hardware_check(d)


def test_camera_config_present_passes(tmp_path, installed):
    installed.append(_EP("rce"))
    cam = tmp_path / "camera.yaml"
    cam.touch()
    d = _video(
        tmp_path, {"name": "r", "backend": "rce", "settings": {"config": str(cam)}}
    )
    preflight_hardware_check(d)  # no raise
    assert _Backend.configured == [({"config": str(cam)}, [])]


def test_missing_rce_config_refuses_the_run(tmp_path, installed):
    installed.append(_EP("rce"))
    missing = str(tmp_path / "gone.yaml")
    d = _video(
        tmp_path, {"name": "r", "backend": "rce", "settings": {"config": missing}}
    )
    with pytest.raises(RuntimeError, match="'r': Camera config not found"):
        preflight_hardware_check(d)


def test_flir_bonsai_setup_needs_no_rce_camera_config(tmp_path, installed):
    d = _video(
        tmp_path,
        {"name": "b", "backend": "flir_bonsai"},
        config_file_camera="camera.rcc.config",
    )
    preflight_hardware_check(d)  # no raise


def test_flir_bonsai_reports_missing_bonsai_exe(tmp_path, installed):
    exe = str(tmp_path / "Bonsai.exe")
    d = _video(
        tmp_path,
        {"name": "b", "backend": "flir_bonsai", "settings": {"bonsai_exe": exe}},
    )
    with pytest.raises(RuntimeError, match="Bonsai executable not found"):
        preflight_hardware_check(d)


def test_flir_bonsai_reports_missing_bonsai_exe_env(tmp_path, installed, monkeypatch):
    monkeypatch.setenv("BONSAI_EXE", str(tmp_path / "Bonsai.exe"))
    d = _video(tmp_path, {"name": "b", "backend": "flir_bonsai"})
    with pytest.raises(RuntimeError, match=r"Bonsai executable not found \(BONSAI_EXE"):
        preflight_hardware_check(d)


def test_backend_refusing_its_settings_is_reported(tmp_path, installed):
    d = _video(
        tmp_path,
        {"name": "b", "backend": "flir_bonsai", "settings": {"bad": True}},
    )
    with pytest.raises(RuntimeError, match="camera group 'b': bad settings"):
        preflight_hardware_check(d)


def test_backend_that_cannot_be_imported_is_reported(tmp_path, installed):
    installed[:] = [_EP("flir_bonsai", broken=True)]
    d = _video(tmp_path, {"name": "b", "backend": "flir_bonsai"})
    with pytest.raises(RuntimeError) as exc:
        preflight_hardware_check(d)
    msg = str(exc.value)
    assert "cannot be imported (No module named 'aiohttp')" in msg
    assert "msw-flir-bonsai>=0.8.0" in msg


def test_installed_backend_passes(tmp_path, installed):
    d = _video(tmp_path, {"name": "b", "backend": "flir_bonsai"})
    preflight_hardware_check(d)  # no raise


def test_missing_backend_names_what_to_install(tmp_path, installed):
    cam = tmp_path / "camera.yaml"
    cam.touch()
    d = _args(tmp_path, None, DevicePorts())
    d["camera_groups"] = _groups(
        {"name": "b", "backend": "flir_bonsai"},
        {
            "name": "pupil",
            "backend": "rce",
            "record": "always",
            "settings": {"config": str(cam)},
        },
    )
    with pytest.raises(RuntimeError) as exc:
        preflight_hardware_check(d)  # the always group starts without video
    msg = str(exc.value)
    assert "camera group 'pupil': camera backend 'rce' is not installed" in msg
    assert "rpi-camera-ensemble[msw]>=0.9.0" in msg and "murineshiftwork[rce]" in msg


def test_video_backend_is_not_checked_without_video(tmp_path, installed):
    d = _args(tmp_path, None, DevicePorts())
    d["camera_groups"] = _groups({"name": "r", "backend": "rce"})
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
