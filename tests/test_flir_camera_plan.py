"""FlirBonsaiClient matches cameras by serial through msw_flir_bonsai.probe.

msw-flir-bonsai is optional, so a fake package stands in for it: probe,
resolve and runner are recorded, no Bonsai or camera is touched.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from murineshiftwork.hardware.camera.client import FlirBonsaiClient
from murineshiftwork.logic.config.models import CameraConfig, CameraUnit


class _Runner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __iter__(self):
        return iter([])


class _Multi:
    def __init__(self, runners):
        self.runners = runners
        self.started = False

    def start(self):
        self.started = True


def _install_fake_flir(monkeypatch, resolve=None):
    """Register a fake msw_flir_bonsai; ``resolve=None`` omits the probe module."""
    pkg = ModuleType("msw_flir_bonsai")
    runner = ModuleType("msw_flir_bonsai.runner")
    runner.BonsaiCameraRunner = _Runner
    runner.MultiCameraRunner = _Multi
    monkeypatch.setitem(sys.modules, "msw_flir_bonsai", pkg)
    monkeypatch.setitem(sys.modules, "msw_flir_bonsai.runner", runner)
    calls = {}
    if resolve is None:
        monkeypatch.setitem(sys.modules, "msw_flir_bonsai.probe", None)
        return calls
    probe = ModuleType("msw_flir_bonsai.probe")

    def probe_cameras(driver):
        calls["probe"] = driver
        return SimpleNamespace(source="sdk", count=2, cameras=[])

    def resolve_camera_units(units, driver, result):
        calls["units"] = [(u.name, u.index, u.serial) for u in units]
        return resolve(units)

    probe.probe_cameras = probe_cameras
    probe.resolve_camera_units = resolve_camera_units
    monkeypatch.setitem(sys.modules, "msw_flir_bonsai.probe", probe)
    return calls


def _client(driver="flycap"):
    cfg = CameraConfig(
        backend="flir_bonsai",
        driver=driver,
        bonsai_exe="Bonsai.exe",
        cameras=[
            CameraUnit(index=0, name="top", serial="12345678"),
            CameraUnit(index=1, name="side", serial="87654321"),
        ],
    )
    client = FlirBonsaiClient(cfg, output_dir="")
    client.initialize_acquisition(acqdir="", basename="s1__video_flir")
    return client


def test_serial_resolves_index(monkeypatch):
    calls = _install_fake_flir(
        monkeypatch,
        resolve=lambda units: [
            SimpleNamespace(index=1, serial=""),
            SimpleNamespace(index=0, serial=""),
        ],
    )
    client = _client()
    client.start_recording()
    assert calls["probe"] == "flycap"
    assert calls["units"] == [("top", 0, "12345678"), ("side", 1, "87654321")]
    kwargs = [r.kwargs for r in client._runner.runners]
    assert [k["cam_index"] for k in kwargs] == [1, 0]
    assert all("cam_serial" not in k for k in kwargs)
    # artifact names keep the configured camera, not the resolved index
    assert kwargs[0]["cam_basename"].endswith("top.0")
    assert client._runner.started


def test_spinnaker_serial_passed_to_runner(monkeypatch):
    _install_fake_flir(
        monkeypatch,
        resolve=lambda units: [
            SimpleNamespace(index=u.index, serial=u.serial) for u in units
        ],
    )
    client = _client(driver="spinnaker")
    client.start_recording()
    serials = [r.kwargs["cam_serial"] for r in client._runner.runners]
    assert serials == ["12345678", "87654321"]


def test_mismatch_refuses_to_start(monkeypatch):
    def missing(units):
        raise RuntimeError("camera serial(s) ['87654321'] not connected")

    _install_fake_flir(monkeypatch, resolve=missing)
    client = _client()
    with pytest.raises(RuntimeError, match="87654321"):
        client.start_recording()
    assert client._runner is None


def test_without_probe_launches_by_index(monkeypatch):
    _install_fake_flir(monkeypatch, resolve=None)
    client = _client()
    client.start_recording()
    assert [r.kwargs["cam_index"] for r in client._runner.runners] == [0, 1]


def test_serial_accepts_unquoted_yaml_int():
    assert CameraUnit(index=0, serial=12345678).serial == "12345678"
    assert CameraUnit(index=0).serial == ""
