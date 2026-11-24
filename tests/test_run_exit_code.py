"""`msw run` exits 3 ("completed degraded") when video was on, the camera failure
policy let the run go on, and no camera recorded."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from msw_plugin_api.camera import UNIT_MISSING, UNIT_READY, UNIT_RECORDING, UnitResult

from murineshiftwork import cli
from murineshiftwork.cli import execute
from murineshiftwork.hardware.camera.session import CameraSession
from murineshiftwork.logic.config.models import CameraGroup
from murineshiftwork.logic.run_context import RunContext


class _Backend:
    acq_type = "video_fake"

    def __init__(self, ok: bool):
        self.ok = ok
        self.units: list = []

    def configure(self, settings, units):
        self.units = list(units)

    def _results(self, state):
        return [
            UnitResult(
                name=u.name,
                ok=self.ok,
                state=state if self.ok else UNIT_MISSING,
                error="" if self.ok else "unreachable",
            )
            for u in self.units
        ] or [UnitResult(name="cam", ok=self.ok, state=state)]

    def preflight(self):
        return self._results(UNIT_READY)

    def start(self, target):
        return self._results(UNIT_RECORDING)

    def stop(self, reason):
        return self._results(UNIT_READY)

    def close(self):
        pass


GROUP = CameraGroup(name="top", backend="fake")


def _task(cameras_ok: bool | None):
    """A task that drives one camera group through CameraSession (None: no camera)."""

    def run_task(**kw):
        if cameras_ok is None:
            return
        session = CameraSession(
            [GROUP], policy="continue", backend_loader=lambda _: _Backend(cameras_ok)
        )
        session.open()
        session.start_targets(
            {"top": SimpleNamespace(acqdir=kw["out_path"] + "/top", basename="top")}
        )
        session.stop()

    return run_task


def _run(monkeypatch, tmp_path, *, cameras_ok, record_video=True, policy="continue"):
    fake_mod = SimpleNamespace(run_task=_task(cameras_ok))
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_mod)
    ctx = RunContext(
        command="run",
        subject="m1",
        task_name="demo",
        out_path=str(tmp_path),
        camera_failure=policy,
        task_settings={"record_video": record_video},
    )
    return execute.run_task(run_context=ctx, camera_groups=[GROUP])


def test_no_camera_recorded_is_degraded(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=False) == execute.EXIT_DEGRADED


def test_camera_never_started_is_degraded(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=None) == execute.EXIT_DEGRADED


def test_a_recorded_camera_exits_0(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=True) == 0


@pytest.mark.parametrize(
    "kw", [{"record_video": False}, {"policy": "raise"}], ids=["no-video", "raise"]
)
def test_only_video_runs_under_continue_are_degraded(monkeypatch, tmp_path, kw):
    assert _run(monkeypatch, tmp_path, cameras_ok=False, **kw) == 0


def test_cli_exits_with_the_run_code(monkeypatch):
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "run"})
    monkeypatch.setattr(cli, "_dispatch", lambda args_dict: execute.EXIT_DEGRADED)
    with pytest.raises(SystemExit) as exc:
        cli.run_cli(["run", "-t", "x"])
    assert exc.value.code == 3
