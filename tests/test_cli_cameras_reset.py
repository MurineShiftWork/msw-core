"""``msw cameras reset`` over fake camera backends found by entry point."""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import msw_plugin_api.camera as camera_api
import pytest
import yaml
from msw_plugin_api.camera import (
    UNIT_FAILED,
    UNIT_STOPPED,
    CameraConfigError,
    UnitResult,
)

from murineshiftwork.cli.cameras_reset import reset_cameras
from murineshiftwork.cli.parser import parse_args


@runtime_checkable
class _Resettable(Protocol):
    """Stand-in for ``ResettableCameraBackend`` while the installed API lacks it."""

    def reset(self) -> list[UnitResult]: ...


class PlainBackend:
    """A backend without ``reset``."""

    instances: list = []

    def __init__(self, acq_type: str = "video_flir") -> None:
        self.acq_type, self.version = acq_type, "1.0"
        self.units: list = []
        self.calls: list[str] = []
        type(self).instances.append(self)

    def configure(self, settings, units):
        self.calls.append("configure")
        if settings.get("bad"):
            raise CameraConfigError("bad settings")
        self.units = list(units)

    def preflight(self):
        return []

    def start(self, target):
        return []

    def stop(self, reason):
        return []

    def close(self):
        self.calls.append("close")


class ResetBackend(PlainBackend):
    fail: set[str] = set()
    explode = False

    def reset(self):
        self.calls.append("reset")
        if self.explode:
            raise OSError("pi unreachable")
        return [
            UnitResult(
                name=u.name,
                ok=u.name not in self.fail,
                state=UNIT_FAILED if u.name in self.fail else UNIT_STOPPED,
                error="still recording" if u.name in self.fail else "",
            )
            for u in self.units
        ]


class _EntryPoint:
    def __init__(self, name, factory):
        self.name, self.value, self._factory = name, f"fake:{name}", factory

    def load(self):
        return self._factory


@pytest.fixture(autouse=True)
def backends(monkeypatch):
    PlainBackend.instances = []
    ResetBackend.fail = set()
    ResetBackend.explode = False
    eps = [
        _EntryPoint("flir_bonsai", lambda: PlainBackend("video_flir")),
        _EntryPoint("rce", lambda: ResetBackend("video_rce")),
    ]
    monkeypatch.setattr(
        "msw_plugin_api.camera.entry_points",
        lambda group: eps if group == "msw.camera" else [],
    )
    if not hasattr(camera_api, "ResettableCameraBackend"):
        monkeypatch.setattr(
            camera_api, "ResettableCameraBackend", _Resettable, raising=False
        )
    monkeypatch.delenv("BONSAI_EXE", raising=False)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    (tmp_path / "setups").mkdir()
    (tmp_path / "ensemble.yaml").write_text("agents: []\n")
    monkeypatch.setenv("MSW_DATA_DIR", str(tmp_path / "data"))
    write_setup(
        tmp_path,
        [
            {"name": "behaviour", "backend": "flir_bonsai", "units": [{"name": "top"}]},
            {
                "name": "pupil",
                "backend": "rce",
                "units": [{"name": "eye"}, {"name": "body"}],
                "settings": {"config": "ensemble.yaml"},
            },
        ],
    )
    return tmp_path


def write_setup(config_dir, cameras) -> None:
    (config_dir / "setups" / "rig.yaml").write_text(
        yaml.safe_dump({"name": "rig", "cameras": cameras})
    )


def all_closed():
    return all(b.calls[-1] == "close" for b in PlainBackend.instances)


def test_reset_ok(config_dir, capsys):
    assert reset_cameras("rig", config_dir=str(config_dir)) == 0
    out = capsys.readouterr().out
    assert "no reset" in out and "stopped" in out
    assert "2/2 camera(s) reset; no reset: behaviour" in out
    rce = next(b for b in PlainBackend.instances if isinstance(b, ResetBackend))
    assert rce.calls == ["configure", "reset", "close"]
    assert all_closed()


def test_failed_unit_exits_1(config_dir, capsys):
    ResetBackend.fail = {"body"}
    assert reset_cameras("rig", config_dir=str(config_dir), as_json=True) == 1
    data = json.loads(capsys.readouterr().out)
    pupil = next(g for g in data["groups"] if g["name"] == "pupil")
    assert pupil["result"] == "failed"
    assert {u["name"]: u["ok"] for u in pupil["units"]} == {"eye": True, "body": False}
    assert next(g for g in data["groups"] if g["name"] == "behaviour")["result"] == (
        "no reset"
    )


def test_backend_raising_exits_1(config_dir, capsys):
    ResetBackend.explode = True
    assert reset_cameras("rig", config_dir=str(config_dir)) == 1
    assert "OSError: pi unreachable" in capsys.readouterr().out
    assert all_closed()


def test_one_group_config_error_still_resets_the_others(config_dir, capsys):
    write_setup(
        config_dir,
        [
            {"name": "behaviour", "backend": "flir_bonsai", "settings": {"bad": True}},
            {
                "name": "pupil",
                "backend": "rce",
                "units": [{"name": "eye"}],
                "settings": {"config": "ensemble.yaml"},
            },
        ],
    )
    assert reset_cameras("rig", config_dir=str(config_dir)) == 2
    out = capsys.readouterr().out
    assert "config error" in out and "bad settings" in out
    assert "1/1 camera(s) reset" in out
    assert all_closed()


def test_unknown_setup_exits_2(config_dir, capsys):
    assert reset_cameras("other", config_dir=str(config_dir)) == 2
    assert "no setup config" in capsys.readouterr().err


def test_old_plugin_api_exits_2(config_dir, capsys, monkeypatch):
    monkeypatch.delattr(camera_api, "ResettableCameraBackend", raising=False)
    assert reset_cameras("rig", config_dir=str(config_dir)) == 2
    assert "msw-plugin-api" in capsys.readouterr().err
    assert PlainBackend.instances == []


def test_parser():
    args = parse_args(["cameras", "reset", "--setup", "rig", "--json"])
    assert args["setup"] == "rig" and args["as_json"]
    with pytest.raises(SystemExit):
        parse_args(["cameras", "reset"])


def test_group_reports_cover_every_outcome(monkeypatch):
    from murineshiftwork.hardware.camera import reset as camera_reset
    from murineshiftwork.logic.config.models import CameraGroup

    class Raising(ResetBackend):
        explode = True

    def load(name):
        if name == "missing":
            raise CameraConfigError("not installed")
        return {"plain": PlainBackend, "reset": ResetBackend, "raise": Raising}[name]()

    groups = [
        CameraGroup(name=n, backend=n, units=[{"name": f"{n}0"}])
        for n in ("plain", "reset", "raise", "missing")
    ]
    reports = camera_reset.reset_camera_group_reports(groups, backend_loader=load)
    assert [(r.name, r.status) for r in reports] == [
        ("plain", "no_reset"),
        ("reset", "reset"),
        ("raise", "error"),
        ("missing", "config_error"),
    ]
    assert reports[1].units[0].state == UNIT_STOPPED
    assert reports[2].error == "OSError: pi unreachable"
    assert all(b.calls[-1] == "close" for b in PlainBackend.instances)
    # the session lock's view keeps only the reset groups
    assert list(camera_reset.reset_camera_groups(groups, backend_loader=load)) == [
        "reset"
    ]

    monkeypatch.delattr(camera_api, "ResettableCameraBackend", raising=False)
    reports = camera_reset.reset_camera_group_reports(groups, backend_loader=load)
    assert {r.status for r in reports} == {"skipped"}
    assert camera_reset.reset_camera_groups(groups, backend_loader=load) == {}
