"""`msw run` exits 3 ("completed degraded") when the task asked for video (the
``record_video`` setting, or a camera session with a ``with_video`` group), the
camera failure policy let the run go on, and no camera recorded; it exits 4 when
some camera recorded and another requested one did not."""

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

    def __init__(self, ok: bool, bad: tuple[str, ...] = (), bad_phase: str = ""):
        self.ok = ok
        self.bad = bad  # cameras that fail in bad_phase ("" : every phase)
        self.bad_phase = bad_phase
        self.units: list = []

    def configure(self, settings, units):
        self.units = list(units)

    def _results(self, state, phase):
        def ok(name):
            lost = name in self.bad and self.bad_phase in ("", phase)
            return self.ok and not lost

        return [
            UnitResult(
                name=u.name,
                ok=ok(u.name),
                state=state if ok(u.name) else UNIT_MISSING,
                error="" if ok(u.name) else "unreachable",
            )
            for u in self.units
        ] or [UnitResult(name="cam", ok=self.ok, state=state)]

    def preflight(self):
        return self._results(UNIT_READY, "preflight")

    def start(self, target):
        return self._results(UNIT_RECORDING, "start")

    def stop(self, reason):
        return self._results(UNIT_READY, "stop")

    def close(self):
        pass


GROUP = CameraGroup(name="top", backend="fake")
ALWAYS = CameraGroup(name="top", backend="fake", record="always")


TWO = CameraGroup(
    name="top",
    backend="fake",
    units=[{"name": "left", "hw_id": "a"}, {"name": "right", "hw_id": "b"}],
)


def _task(cameras_ok: bool | None, group: CameraGroup = GROUP, bad=(), bad_phase=""):
    """A task that drives one camera group through CameraSession (None: no camera)."""

    def run_task(**kw):
        if cameras_ok is None:
            return
        session = CameraSession(
            [group],
            policy="continue",
            backend_loader=lambda _: _Backend(cameras_ok, bad, bad_phase),
        )
        session.open()
        session.start_targets(
            {"top": SimpleNamespace(acqdir=kw["out_path"] + "/top", basename="top")}
        )
        session.stop()

    return run_task


def _run(
    monkeypatch,
    tmp_path,
    *,
    cameras_ok,
    record_video=True,
    policy="continue",
    group=GROUP,
    bad=(),
    bad_phase="",
    simulate=False,
):
    fake_mod = SimpleNamespace(run_task=_task(cameras_ok, group, bad, bad_phase))
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_mod)
    ctx = RunContext(
        command="run",
        subject="m1",
        task_name="demo",
        out_path=str(tmp_path),
        camera_failure=policy,
        simulate=simulate,
        task_settings={} if record_video is None else {"record_video": record_video},
    )
    return execute.run_task(run_context=ctx, camera_groups=[group])


def test_no_camera_recorded_is_degraded(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=False) == execute.EXIT_DEGRADED


def test_camera_never_started_is_degraded(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=None) == execute.EXIT_DEGRADED


def test_a_recorded_camera_exits_0(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=True) == 0


def test_a_task_asking_for_video_without_the_setting_is_degraded(monkeypatch, tmp_path):
    # a task that records video by nature has no record_video setting
    code = _run(monkeypatch, tmp_path, cameras_ok=False, record_video=None)
    assert code == execute.EXIT_DEGRADED


@pytest.mark.parametrize(
    "kw",
    [
        {"record_video": False, "group": ALWAYS},
        {"record_video": None, "group": ALWAYS},
        {"policy": "raise"},
    ],
    ids=["no-video", "no-setting", "raise"],
)
def test_only_video_runs_under_continue_are_degraded(monkeypatch, tmp_path, kw):
    assert _run(monkeypatch, tmp_path, cameras_ok=False, **kw) == 0


@pytest.mark.parametrize("phase", ["preflight", "start", "stop"])
def test_some_cameras_lost_is_partial(monkeypatch, tmp_path, phase):
    # the camera lost in any phase (e.g. 0 frames at stop) is named in the warning
    warnings: list[str] = []
    monkeypatch.setattr(
        execute.logging, "warning", lambda msg, *a: warnings.append(msg % a)
    )
    code = _run(
        monkeypatch,
        tmp_path,
        cameras_ok=True,
        group=TWO,
        bad=("right",),
        bad_phase=phase,
    )
    assert code == execute.EXIT_PARTIAL_CAMERAS == 4
    text = " ".join(warnings)
    assert "top/right" in text
    assert "top/left" not in text


def test_all_cameras_recorded_exits_0(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, cameras_ok=True, group=TWO) == 0


def test_all_cameras_lost_is_degraded_not_partial(monkeypatch, tmp_path):
    code = _run(
        monkeypatch, tmp_path, cameras_ok=True, group=TWO, bad=("left", "right")
    )
    assert code == execute.EXIT_DEGRADED


@pytest.mark.parametrize(
    "kw",
    [{"simulate": True}, {"record_video": False, "group": ALWAYS}],
    ids=["simulate", "no-video"],
)
def test_partial_loss_exits_0_in_simulate_and_without_video(monkeypatch, tmp_path, kw):
    kw = {"group": TWO, **kw} if "group" not in kw else kw
    assert _run(monkeypatch, tmp_path, cameras_ok=True, bad=("right",), **kw) == 0


def test_cli_exits_with_the_run_code(monkeypatch):
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "run"})
    monkeypatch.setattr(cli, "_dispatch", lambda args_dict: execute.EXIT_DEGRADED)
    with pytest.raises(SystemExit) as exc:
        cli.run_cli(["run", "-t", "x"])
    assert exc.value.code == 3
