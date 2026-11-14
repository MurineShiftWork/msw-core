"""``msw cameras check`` over fake camera backends found by entry point."""

from __future__ import annotations

import json

import pytest
import yaml
from msw_plugin_api.camera import (
    UNIT_MISSING,
    UNIT_READY,
    CameraConfigError,
    UnitResult,
)

from murineshiftwork.cli import tasks as cli_tasks
from murineshiftwork.cli.cameras import check_cameras
from murineshiftwork.cli.parser import parse_args


class FakeBackend:
    """Reports every camera ready unless named in ``missing``."""

    instances: list[FakeBackend] = []
    missing: set[str] = set()
    interrupt = False

    def __init__(self, acq_type: str) -> None:
        self.acq_type = acq_type
        self.version = "1.2.3"
        self.units: list = []
        self.calls: list[str] = []
        FakeBackend.instances.append(self)

    def configure(self, settings, units):
        self.calls.append("configure")
        if settings.get("bad"):
            raise CameraConfigError("bad settings")
        self.units = list(units)

    def preflight(self):
        self.calls.append("preflight")
        if self.interrupt:
            raise KeyboardInterrupt
        return [
            UnitResult(
                name=u.name,
                ok=u.name not in self.missing,
                state=UNIT_MISSING if u.name in self.missing else UNIT_READY,
                error="not found" if u.name in self.missing else "",
                error_code="not_found" if u.name in self.missing else "",
            )
            for u in self.units
        ]

    def start(self, target):
        self.calls.append("start")
        return []

    def stop(self, reason):
        self.calls.append("stop")
        return []

    def close(self):
        self.calls.append("close")


class _EntryPoint:
    def __init__(self, name, factory):
        self.name, self.value, self._factory = name, f"fake:{name}", factory

    def load(self):
        return self._factory


@pytest.fixture(autouse=True)
def backends(monkeypatch):
    FakeBackend.instances = []
    FakeBackend.missing = set()
    FakeBackend.interrupt = False
    eps = [
        _EntryPoint("flir_bonsai", lambda: FakeBackend("video_flir")),
        _EntryPoint("rce", lambda: FakeBackend("video_rce")),
    ]
    monkeypatch.setattr(
        "msw_plugin_api.camera.entry_points",
        lambda group: eps if group == "msw.camera" else [],
    )
    monkeypatch.delenv("BONSAI_EXE", raising=False)
    return FakeBackend


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A config dir with setup ``rig``: a video group and an always-on group."""
    (tmp_path / "setups").mkdir()
    (tmp_path / "ensemble.yaml").write_text("agents: []\n")
    monkeypatch.setenv("MSW_DATA_DIR", str(tmp_path / "data"))
    write_setup(
        tmp_path,
        [
            {
                "name": "behaviour",
                "backend": "flir_bonsai",
                "units": [{"name": "top", "hw_id": "12345678"}, {"name": "side"}],
            },
            {
                "name": "pupil",
                "backend": "rce",
                "record": "always",
                "units": [{"name": "eye"}],
                "settings": {"config": "ensemble.yaml"},
            },
        ],
    )
    return tmp_path


def write_setup(config_dir, cameras) -> None:
    (config_dir / "setups" / "rig.yaml").write_text(
        yaml.safe_dump({"name": "rig", "cameras": cameras})
    )


def task_yaml(tmp_path, monkeypatch, settings: dict) -> None:
    path = tmp_path / "bundled" / "task.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(settings))
    monkeypatch.setattr(cli_tasks, "find_task_by_name", lambda task_name: task_name)
    monkeypatch.setattr(cli_tasks, "_task_yaml_path", lambda name: path)


def closed_all():
    return FakeBackend.instances and all(
        b.calls[-1] == "close" and "start" not in b.calls for b in FakeBackend.instances
    )


def test_all_ready_exits_0(config_dir, capsys):
    assert check_cameras("rig", config_dir=str(config_dir)) == 0
    out = capsys.readouterr().out
    assert "behaviour" in out and "12345678" in out and "1.2.3" in out
    assert "3/3 camera(s) ready in 2 group(s)" in out
    assert closed_all()


def test_one_missing_exits_1(config_dir, capsys):
    FakeBackend.missing = {"side"}
    assert check_cameras("rig", config_dir=str(config_dir)) == 1
    out = capsys.readouterr().out
    line = next(
        ln for ln in out.splitlines() if ln.startswith("behaviour") and "side" in ln
    )
    assert "missing" in line and "not_found" in line and "not found" in line
    assert "2/3 camera(s) ready" in out
    assert closed_all()


def test_policy_is_not_applied(config_dir, capsys):
    """A failing camera under camera_failure: raise is reported, not raised."""
    FakeBackend.missing = {"top", "eye"}
    assert check_cameras("rig", config_dir=str(config_dir), as_json=True) == 1
    data = json.loads(capsys.readouterr().out)
    states = {u["name"]: u["state"] for g in data["groups"] for u in g["units"]}
    assert states == {"top": "missing", "side": "ready", "eye": "missing"}


@pytest.mark.parametrize(
    "cameras, message",
    [
        ([{"name": "x", "backend": "nope", "units": [{"name": "a"}]}], "not installed"),
        (
            [{"name": "x", "backend": "rce", "settings": {"config": "gone.yaml"}}],
            "Camera config not found",
        ),
        (
            [
                {
                    "name": "x",
                    "backend": "flir_bonsai",
                    "settings": {"bonsai_exe": "C:/no/Bonsai.exe"},
                }
            ],
            "Bonsai executable not found",
        ),
        (
            [{"name": "x", "backend": "flir_bonsai", "settings": {"bad": True}}],
            "bad settings",
        ),
    ],
)
def test_config_error_exits_2(config_dir, capsys, cameras, message):
    write_setup(config_dir, cameras)
    assert check_cameras("rig", config_dir=str(config_dir)) == 2
    assert message in capsys.readouterr().err
    assert all(b.calls[-1] == "close" for b in FakeBackend.instances)


def test_unknown_setup_exits_2(config_dir, capsys):
    assert check_cameras("other", config_dir=str(config_dir)) == 2
    assert "no setup config" in capsys.readouterr().err


def test_json_shape(config_dir, capsys):
    FakeBackend.missing = {"eye"}
    assert check_cameras("rig", config_dir=str(config_dir), as_json=True) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["setup"] == "rig" and data["task"] is None and data["ok"] is False
    assert [g["name"] for g in data["groups"]] == ["behaviour", "pupil"]
    group = data["groups"][1]
    assert group["backend"] == "rce" and group["backend_version"] == "1.2.3"
    assert group["units"] == [
        {
            "name": "eye",
            "hw_id": "",
            "ok": False,
            "state": "missing",
            "error_code": "not_found",
            "error": "not found",
            "extra": {},
        }
    ]


def test_json_config_error(config_dir, capsys):
    write_setup(config_dir, [{"name": "x", "backend": "nope"}])
    assert check_cameras("rig", config_dir=str(config_dir), as_json=True) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["groups"] == []
    assert "not installed" in data["error"]


def test_backends_closed_after_an_interrupt(config_dir):
    FakeBackend.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        check_cameras("rig", config_dir=str(config_dir))
    assert closed_all()


@pytest.mark.parametrize(
    "settings, groups",
    [
        ({"record_video": False}, ["pupil"]),
        ({"record_video": True}, ["behaviour", "pupil"]),
        ({"record_video": True, "record_cameras": ["top"]}, ["behaviour", "pupil"]),
    ],
)
def test_task_decides_the_groups(
    config_dir, tmp_path, monkeypatch, capsys, settings, groups
):
    task_yaml(tmp_path, monkeypatch, settings)
    code = check_cameras("rig", task="t", config_dir=str(config_dir), as_json=True)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task"] == "t"
    assert [g["name"] for g in data["groups"]] == groups
    if "record_cameras" in settings:
        assert [u["name"] for u in data["groups"][0]["units"]] == ["top"]


def test_task_with_unknown_record_camera_exits_2(
    config_dir, tmp_path, monkeypatch, capsys
):
    task_yaml(tmp_path, monkeypatch, {"record_video": True, "record_cameras": ["x"]})
    assert check_cameras("rig", task="t", config_dir=str(config_dir)) == 2
    assert "unknown camera" in capsys.readouterr().err


def test_parser_registers_cameras_check():
    args = parse_args(["cameras", "check", "rig", "--task", "seq", "--json"])
    assert (args["setup"], args["task"], args["as_json"]) == ("rig", "seq", True)
    assert args["func"].__name__ == "run_cameras_check"
