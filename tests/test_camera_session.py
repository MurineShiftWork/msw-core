"""CameraSession over camera backends found by entry point (a fake backend here)."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pytest
import yaml
from msw_plugin_api.camera import (
    UNIT_MISSING,
    UNIT_READY,
    UNIT_RECORDING,
    UNIT_STOPPED,
    CameraConfigError,
    UnitResult,
)
from murineshiftwork.namespace.manifest import (
    append_acquisition_to_session,
    init_acquisition_manifest,
    init_session_manifest,
)
from murineshiftwork.namespace.paths import generate_session_paths

from murineshiftwork.hardware.camera import client as camera_client
from murineshiftwork.hardware.camera import session as camera_session
from murineshiftwork.hardware.camera.records import (
    CamerasRecord,
    peer_cameras,
    read_cameras_record,
)
from murineshiftwork.hardware.camera.session import CameraSession, CameraStartError
from murineshiftwork.logic.config.models import CameraGroup, CameraUnit, SetupConfig


class FakeBackend:
    """A camera backend whose cameras fail on demand.

    ``fail[phase]`` maps a camera name to the error it reports in that phase;
    ``raises`` lists the phases that raise instead of reporting; ``throw`` maps
    ``(acq_type, phase)`` to an exception that one backend raises.
    """

    instances: list[FakeBackend] = []
    fail: dict[str, dict[str, str]] = {}
    raises: set[str] = set()
    throw: dict[tuple[str, str], BaseException] = {}

    def __init__(self, acq_type: str, version: str = "1.2.3") -> None:
        self.acq_type = acq_type
        self.version = version
        self.calls: list[tuple] = []
        self.units: list = []
        self.settings: dict = {}
        FakeBackend.instances.append(self)

    def _results(self, phase: str, ok_state: str) -> list[UnitResult]:
        if phase in self.raises:
            raise RuntimeError(f"{phase} exploded")
        if (self.acq_type, phase) in self.throw:
            raise self.throw[self.acq_type, phase]
        errors = self.fail.get(phase, {})
        return [
            UnitResult(
                name=u.name,
                ok=u.name not in errors,
                state=UNIT_MISSING if u.name in errors else ok_state,
                error=errors.get(u.name, ""),
                files=[f"{u.name}.avi"] if phase == "stop" else [],
                extra={"serial": u.hw_id} if u.hw_id else {},
            )
            for u in self.units
        ]

    def configure(self, settings, units):
        self.calls.append(("configure", dict(settings), [u.name for u in units]))
        if settings.get("bad"):
            raise CameraConfigError("bad settings")
        self.settings, self.units = dict(settings), list(units)

    def preflight(self):
        self.calls.append(("preflight",))
        return self._results("preflight", UNIT_READY)

    def start(self, target):
        self.calls.append(("start", target))
        return self._results("start", UNIT_RECORDING)

    def stop(self, reason):
        self.calls.append(("stop", reason))
        return self._results("stop", UNIT_STOPPED)

    def close(self):
        self.calls.append(("close",))


class _EntryPoint:
    def __init__(self, name, factory):
        self.name, self.value, self._factory = name, f"fake:{name}", factory

    def load(self):
        return self._factory


@pytest.fixture(autouse=True)
def backends(monkeypatch):
    """Register fake ``flir_bonsai`` and ``rce`` backends as ``msw.camera`` plugins."""
    FakeBackend.instances = []
    FakeBackend.fail = {}
    FakeBackend.raises = set()
    FakeBackend.throw = {}
    eps = [
        _EntryPoint("flir_bonsai", lambda: FakeBackend("video_flir")),
        _EntryPoint("rce", lambda: FakeBackend("video_rce", "0.7.0")),
    ]
    monkeypatch.setattr(
        "msw_plugin_api.camera.entry_points",
        lambda group: eps if group == "msw.camera" else [],
    )
    return FakeBackend


def behaviour(**kw) -> CameraGroup:
    return CameraGroup(
        name="behaviour",
        backend="flir_bonsai",
        units=[
            CameraUnit(name="top", hw_id="1801", index=0),
            CameraUnit(name="back", index=1),
        ],
        **kw,
    )


def pupil(**kw) -> CameraGroup:
    return CameraGroup(
        name="pupil",
        backend="rce",
        role="eye",
        record="always",
        units=[CameraUnit(name="eye")],
        settings={"config": "pupil.yaml"},
        **kw,
    )


@pytest.fixture
def session_paths(tmp_path):
    """Behaviour acquisition paths with its manifests, as TaskProcess leaves them."""
    sp = generate_session_paths(
        subject="m1", task="seq", basepath=tmp_path, printout=False
    )
    folder = Path(sp["session_folder"])
    folder.mkdir(parents=True)
    init_session_manifest(folder.parent, sp["host_session_name"])
    append_acquisition_to_session(folder.parent, sp["session_basename"])
    init_acquisition_manifest(folder, sp["session_basename"])
    return sp


def peers(sp) -> dict[str, dict]:
    data = yaml.safe_load(
        (Path(sp["session_folder"]).parent / "session_manifest.yaml").read_text()
    )
    return {a["basename"]: a for a in data["acquisitions"] if "group" in a}


def by_backend(acq_type: str) -> FakeBackend:
    return next(b for b in FakeBackend.instances if b.acq_type == acq_type)


# --- the happy path ---


def test_start_and_stop_one_group(session_paths, caplog):
    caplog.set_level(logging.INFO)
    cams = CameraSession([behaviour(settings={"driver": "spinnaker"})])
    cams.open()
    cams.start(session_paths)
    cams.stop("completed")

    fake = by_backend("video_flir")
    configure, _, start, stop, close = fake.calls
    assert configure == ("configure", {"driver": "spinnaker"}, ["top", "back"])
    assert fake.units[0].settings == {"index": 0} and fake.units[0].hw_id == "1801"
    target = start[1]
    expected = generate_session_paths(
        subject="m1",
        task="seq",
        basepath=session_paths["basepath"],
        acq_type="video_flir",
        linked_to=session_paths["host_session_name"],
        datetime_str=session_paths["datetime"],
        printout=False,
    )
    assert target.acqdir == expected["session_folder"]
    assert target.basename == expected["session_basename"]
    assert target.host_relpath == session_paths["session_folder_relative"]
    assert target.host_name == session_paths["session_basename"]
    assert Path(target.acqdir).is_dir()
    assert stop == ("stop", "completed") and close == ("close",)

    peer = peers(session_paths)[target.basename]
    assert peer["status"] == "complete"
    assert peer["cameras"] == ["top", "back"]
    assert (peer["group"], peer["role"], peer["backend"], peer["backend_version"]) == (
        "behaviour",
        "",
        "flir_bonsai",
        "1.2.3",
    )
    # one line per camera at start and at stop
    lines = [r.getMessage() for r in caplog.records if r.name.endswith("session")]
    assert [m for m in lines if " start: " in m] == [
        "Camera behaviour/top start: recording",
        "Camera behaviour/back start: recording",
    ]
    assert len([m for m in lines if " stop: " in m]) == 2


def test_manifest_records_groups_units_and_clocks(session_paths):
    with CameraSession([behaviour(), pupil()], policy="continue") as cams:
        cams.start(session_paths)

    record = read_cameras_record(session_paths["session_folder"])
    assert record is not None and record.policy == "continue"
    flir, rce = record.groups
    assert (flir.name, flir.backend, flir.backend_version, flir.acq_type) == (
        "behaviour",
        "flir_bonsai",
        "1.2.3",
        "video_flir",
    )
    assert (rce.role, rce.record, rce.backend_version) == ("eye", "always", "0.7.0")
    assert flir.status == "complete" and flir.basename.endswith("video_flir__v1")
    assert flir.start.wall_ns <= flir.stop.wall_ns
    assert flir.start.monotonic_ns <= flir.stop.monotonic_ns
    assert flir.start.wall_iso.startswith("20")
    top = flir.units[0]
    assert (top.name, top.hw_id, top.ok, top.state) == ("top", "1801", True, "stopped")
    assert top.files == ["top.avi"] and top.extra == {"serial": "1801"}
    # the rest of the acquisition manifest is kept
    raw = yaml.safe_load(
        (
            Path(session_paths["session_folder"]) / "acquisition_manifest.yaml"
        ).read_text()
    )
    assert raw["type"] == "acquisition" and "cameras" in raw


def test_no_groups_is_a_no_op(session_paths):
    cams = CameraSession([])
    assert not cams
    cams.open()
    cams.start(session_paths)
    cams.stop()
    assert read_cameras_record(session_paths["session_folder"]) is None
    assert FakeBackend.instances == []


def test_stop_is_idempotent(session_paths):
    cams = CameraSession([behaviour()])
    cams.start(session_paths)  # opens on demand
    cams.stop()
    cams.stop()
    assert [c[0] for c in by_backend("video_flir").calls].count("stop") == 1


# --- policy raise ---


def test_raise_on_preflight_failure_names_the_cameras(session_paths):
    FakeBackend.fail = {"preflight": {"back": "serial 1810 not found"}}
    cams = CameraSession([behaviour(), pupil()])
    with pytest.raises(CameraStartError) as exc:
        cams.open()
    assert "behaviour/back: missing: serial 1810 not found" in str(exc.value)
    assert exc.value.phase == "preflight"
    for fake in FakeBackend.instances:
        assert ("close",) in fake.calls and not any(c[0] == "start" for c in fake.calls)


def test_raise_on_start_failure_stops_what_started(session_paths):
    FakeBackend.fail = {"start": {"eye": "no frames"}}
    cams = CameraSession([behaviour(), pupil()])
    cams.open()
    with pytest.raises(CameraStartError, match="pupil/eye: missing: no frames"):
        cams.start(session_paths)
    for fake in FakeBackend.instances:
        assert [c[0] for c in fake.calls][-2:] == ["stop", "close"]
    statuses = {p["group"]: p["status"] for p in peers(session_paths).values()}
    assert statuses == {"behaviour": "complete", "pupil": "failed"}
    record = read_cameras_record(session_paths["session_folder"])
    assert [g.status for g in record.groups] == ["complete", "failed"]


def test_backend_exception_fails_every_camera_of_the_group(session_paths):
    FakeBackend.raises = {"start"}
    cams = CameraSession([behaviour()])
    cams.open()
    with pytest.raises(CameraStartError) as exc:
        cams.start(session_paths)
    names = [r.name for _, r in exc.value.failures]
    assert names == ["top", "back"]
    assert all(r.error == "RuntimeError: start exploded" for _, r in exc.value.failures)
    [peer] = peers(session_paths).values()
    assert peer["status"] == "failed"


@pytest.mark.parametrize("phase", ["start", "stop"])
def test_backend_error_finalises_the_peer_failed_once(
    session_paths, monkeypatch, phase
):
    """A backend error marks the peer failed; a later stop never turns it complete."""
    FakeBackend.raises = {phase}
    finalised: list[str] = []
    real = camera_session.finalize_acquisition_in_session

    def spy(container, basename, status="complete", **kw):
        finalised.append(status)
        real(container, basename, status=status, **kw)

    monkeypatch.setattr(camera_session, "finalize_acquisition_in_session", spy)
    cams = CameraSession([behaviour()])
    cams.open()
    if phase == "start":
        with pytest.raises(CameraStartError):
            cams.start(session_paths)
    else:
        cams.start(session_paths)
        cams.stop()
    FakeBackend.raises = set()
    cams.stop()
    assert finalised == ["failed"]
    [peer] = peers(session_paths).values()
    assert peer["status"] == "failed"


def test_clean_session_finalises_the_peer_complete_after_stop(session_paths):
    cams = CameraSession([behaviour()])
    cams.start(session_paths)
    [peer] = peers(session_paths).values()
    assert peer["status"] == "running"
    cams.stop()
    [peer] = peers(session_paths).values()
    assert peer["status"] == "complete"


def test_missing_result_counts_as_failed(session_paths, monkeypatch):
    monkeypatch.setattr(FakeBackend, "preflight", lambda self: [])
    with pytest.raises(CameraStartError, match="no preflight result reported"):
        CameraSession([behaviour()]).open()


def test_config_error_raises_whatever_the_policy():
    group = behaviour(settings={"bad": True})
    with pytest.raises(CameraConfigError, match="bad settings"):
        CameraSession([group], policy="continue").open()


def test_missing_backend_names_what_to_install(monkeypatch):
    monkeypatch.setattr("msw_plugin_api.camera.entry_points", lambda group: [])
    with pytest.raises(CameraConfigError) as exc:
        CameraSession([behaviour()]).open()
    msg = str(exc.value)
    assert "'flir_bonsai' is not installed" in msg
    assert "msw-flir-bonsai" in msg and "murineshiftwork[flir]" in msg


# --- policy continue ---


def test_continue_warns_and_runs_on(session_paths, caplog):
    FakeBackend.fail = {"start": {"back": "no frames"}}
    cams = CameraSession([behaviour()], policy="continue")
    cams.open()
    cams.start(session_paths)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Camera behaviour/back start: missing: no frames" in warned
    cams.stop()
    [peer] = peers(session_paths).values()
    assert peer["status"] == "partial"
    [group] = read_cameras_record(session_paths["session_folder"]).groups
    top, back = group.units
    assert (top.ok, back.ok, back.error) == (True, False, "no frames")


def test_continue_stops_a_group_left_without_cameras(session_paths):
    FakeBackend.fail = {"start": {"eye": "no frames"}}
    cams = CameraSession([behaviour(), pupil()], policy="continue")
    cams.open()
    cams.start(session_paths)
    rce = by_backend("video_rce")
    assert [c[0] for c in rce.calls][-1] == "stop"  # stopped at once
    assert [c[0] for c in by_backend("video_flir").calls][-1] == "start"
    cams.stop()
    assert [c[0] for c in rce.calls].count("stop") == 1
    statuses = {p["group"]: p["status"] for p in peers(session_paths).values()}
    assert statuses == {"behaviour": "complete", "pupil": "failed"}


def test_continue_skips_a_group_failing_preflight(session_paths):
    FakeBackend.fail = {"preflight": {"eye": "unreachable"}}
    cams = CameraSession([behaviour(), pupil()], policy="continue")
    cams.open()
    cams.start(session_paths)
    cams.stop()
    assert not any(c[0] == "start" for c in by_backend("video_rce").calls)
    assert [p["group"] for p in peers(session_paths).values()] == ["behaviour"]
    record = read_cameras_record(session_paths["session_folder"])
    assert [(g.name, g.status) for g in record.groups] == [
        ("behaviour", "complete"),
        ("pupil", "failed"),
    ]


def test_context_manager_stops_on_error(session_paths):
    with pytest.raises(KeyError), CameraSession([behaviour()]) as cams:
        cams.start(session_paths)
        raise KeyError("task died")
    assert ("stop", "error: KeyError") in by_backend("video_flir").calls


# --- which groups start ---


def _run(groups, record_video, **extra):
    return {
        "camera_groups": groups,
        "settings.task.patched": {"record_video": record_video},
        **extra,
    }


def test_always_groups_start_without_video():
    groups = [behaviour(), pupil()]
    assert [g.name for g in CameraSession.from_run(_run(groups, False)).groups] == [
        "pupil"
    ]
    assert [g.name for g in CameraSession.from_run(_run(groups, True)).groups] == [
        "behaviour",
        "pupil",
    ]
    session = CameraSession.from_run(_run(groups, False), record_video=True)
    assert len(session.groups) == 2


def test_policy_comes_from_the_run_then_the_setup():
    groups = [behaviour()]
    setup = SetupConfig(name="rig", camera_failure="continue")
    assert CameraSession.from_run(_run(groups, True)).policy == "raise"
    assert (
        CameraSession.from_run(_run(groups, True, setup_config=setup)).policy
        == "continue"
    )
    run = _run(groups, True, setup_config=setup, camera_failure="raise")
    assert CameraSession.from_run(run).policy == "raise"


# --- records ---


def test_records_read_the_legacy_list_of_names():
    record = CamerasRecord.model_validate(["top", "back"])
    assert record.camera_names == ["top", "back"]
    peer = peer_cameras({"basename": "b", "status": "complete", "cameras": ["top"]})
    assert [u.name for u in peer.units] == ["top"] and peer.status == "complete"
    assert peer_cameras({"basename": "b"}).units == []


# --- deprecated client ---


def test_make_camera_client_warns_and_drives_one_group(session_paths):
    with pytest.warns(DeprecationWarning):
        client = camera_client.make_camera_client(behaviour())
    assert client.acq_type == "video_flir"
    client.start()
    client.setup_agents()
    paths = generate_session_paths(
        subject="m1",
        task="seq",
        basepath=session_paths["basepath"],
        acq_type=client.acq_type,
        linked_to=session_paths["host_session_name"],
        datetime_str=session_paths["datetime"],
        printout=False,
    )
    Path(paths["session_folder"]).mkdir(parents=True)
    client.initialize_acquisition(
        acquisition_path=session_paths["session_folder_relative"],
        acquisition_name=session_paths["session_basename"],
        acqdir=paths["session_folder"],
        basename=paths["session_basename"],
    )
    client.start_preview()
    client.start_recording()
    client.stop_acquisition()
    client.stop()
    calls = [c[0] for c in by_backend("video_flir").calls]
    assert calls == ["configure", "preflight", "start", "stop", "close"]
    assert peers(session_paths)[paths["session_basename"]]["status"] == "complete"
    assert read_cameras_record(session_paths["session_folder"]).groups[0].units


def test_make_camera_client_without_cameras(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert camera_client.make_camera_client(None) is None
        cfg = tmp_path / "camera.yaml"
        cfg.touch()
        client = camera_client.make_camera_client(
            None, config_file_camera=str(cfg), output_dir=str(tmp_path)
        )
    assert client.group.backend == "rce"
    # the conductor saves its YAMLs under the output dir, as before camera groups
    assert client.group.settings == {"config": str(cfg), "data_dir": str(tmp_path)}


def test_make_camera_client_refuses_an_rce_group_without_its_config():
    """A configured RCE camera whose ensemble YAML is gone must not run without
    video."""
    missing = CameraGroup(name="rce", backend="rce", settings={"config": "no.yaml"})
    with (
        pytest.warns(DeprecationWarning),
        pytest.raises(CameraConfigError, match="'no.yaml'"),
    ):
        camera_client.make_camera_client(missing)


def test_make_camera_client_honours_the_run_policy(session_paths):
    FakeBackend.fail = {"preflight": {"top": "unplugged"}}
    group = behaviour().with_failure_policy("continue")
    with pytest.warns(DeprecationWarning):
        client = camera_client.make_camera_client(group)
    assert client.session.policy == "continue"
    client.start()  # top is unplugged, back records
    with pytest.warns(DeprecationWarning):
        override = camera_client.make_camera_client(group, camera_failure="raise")
    assert override.session.policy == "raise"


def test_make_camera_client_raises_on_failed_start(session_paths):
    FakeBackend.fail = {"preflight": {"top": "unplugged"}}
    with pytest.warns(DeprecationWarning):
        client = camera_client.make_camera_client([behaviour()])
    with pytest.raises(CameraStartError):
        client.start()
    assert by_backend("video_flir").calls[-1] == ("close",)


# --- interrupts and failing stops ---


def _calls(acq_type: str) -> list[str]:
    return [c[0] for c in by_backend(acq_type).calls]


def test_interrupt_in_preflight_closes_every_backend():
    FakeBackend.throw = {("video_flir", "preflight"): KeyboardInterrupt()}
    cams = CameraSession([behaviour(), pupil()])
    with pytest.raises(KeyboardInterrupt):
        cams.open()
    for fake in FakeBackend.instances:
        assert fake.calls[-1] == ("close",)


def test_interrupt_in_start_stops_what_started(session_paths):
    FakeBackend.throw = {("video_rce", "start"): KeyboardInterrupt()}
    cams = CameraSession([behaviour(), pupil()])
    cams.open()
    with pytest.raises(KeyboardInterrupt):
        cams.start(session_paths)
    assert _calls("video_flir")[-2:] == ["stop", "close"]
    assert _calls("video_rce")[-1] == "close"
    statuses = {p["group"]: p["status"] for p in peers(session_paths).values()}
    assert statuses["behaviour"] == "complete"


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(2)])
def test_a_group_raising_in_stop_does_not_leave_the_others_running(session_paths, exc):
    FakeBackend.throw = {("video_flir", "stop"): exc}
    cams = CameraSession([behaviour(), pupil()])
    cams.start(session_paths)
    with pytest.raises(type(exc)):
        cams.stop("completed")
    # the other group is stopped, every backend closed, the record written
    assert _calls("video_rce")[-2:] == ["stop", "close"]
    assert _calls("video_flir")[-1] == "close"
    statuses = {p["group"]: p["status"] for p in peers(session_paths).values()}
    assert statuses == {"behaviour": "failed", "pupil": "complete"}
    record = read_cameras_record(session_paths["session_folder"])
    assert [g.status for g in record.groups] == ["failed", "complete"]
    # a second stop tries the interrupted group again, and only that one
    FakeBackend.throw = {}
    cams.stop("completed")
    assert _calls("video_flir").count("stop") == 2
    assert _calls("video_rce").count("stop") == 1
    cams.stop("completed")
    assert _calls("video_flir").count("stop") == 2


def test_stop_survives_a_backend_reporting_no_file_list(session_paths, monkeypatch):
    def stop(self, reason):
        self.calls.append(("stop", reason))
        return [
            UnitResult(name=u.name, ok=True, state=UNIT_STOPPED, files=None, extra=None)
            for u in self.units
        ]

    cams = CameraSession([behaviour(), pupil()])
    cams.start(session_paths)
    monkeypatch.setattr(FakeBackend, "stop", stop)
    cams.stop()
    for fake in FakeBackend.instances:
        assert fake.calls[-1] == ("close",)
    statuses = {p["group"]: p["status"] for p in peers(session_paths).values()}
    assert statuses == {"behaviour": "complete", "pupil": "complete"}


def test_start_twice_starts_each_group_once(session_paths):
    cams = CameraSession([behaviour(), pupil()])
    cams.start(session_paths)
    cams.start(session_paths)
    assert _calls("video_flir").count("start") == 1
    assert _calls("video_rce").count("start") == 1
    assert len(peers(session_paths)) == 2
    cams.stop()


def test_raise_starts_no_group_after_a_failed_one(session_paths):
    FakeBackend.fail = {"start": {"top": "no frames"}}
    cams = CameraSession([behaviour(), pupil()])
    cams.open()
    with pytest.raises(CameraStartError, match="behaviour/top"):
        cams.start(session_paths)
    assert "start" not in _calls("video_rce")
    assert _calls("video_rce")[-1] == "close"
    assert [p["group"] for p in peers(session_paths).values()] == ["behaviour"]


def test_no_start_after_stop(session_paths):
    cams = CameraSession([behaviour()])
    cams.open()
    cams.stop()
    cams.start(session_paths)
    assert "start" not in _calls("video_flir")


def test_unit_files_are_those_of_the_latest_phase_reporting_any(session_paths):
    reported = {"start": ["top.avi.part"], "stop": ["top.avi", "top.csv"]}

    def results(self, phase, ok_state):
        return [
            UnitResult(
                name=u.name,
                ok=True,
                state=ok_state,
                files=list(reported.get(phase, [])) if u.name == "top" else [],
            )
            for u in self.units
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(FakeBackend, "_results", results)
        cams = CameraSession([behaviour()])
        cams.start(session_paths)
        record = cams.record()
        assert record.groups[0].units[0].files == ["top.avi.part"]
        cams.stop()
    [group] = read_cameras_record(session_paths["session_folder"]).groups
    top, back = group.units
    assert top.files == ["top.avi", "top.csv"] and back.files == []


def test_remote_host_fills_an_empty_hw_id(session_paths, monkeypatch):
    real = FakeBackend._results

    def results(self, phase, ok_state):
        out = real(self, phase, ok_state)
        for r in out:
            r.extra = {**r.extra, "host": "pi-eye.example"}
        return out

    monkeypatch.setattr(FakeBackend, "_results", results)
    with CameraSession([pupil(), behaviour()]) as cams:
        cams.start(session_paths)
    rce, flir = read_cameras_record(session_paths["session_folder"]).groups
    assert rce.units[0].hw_id == "pi-eye.example"
    assert flir.units[0].hw_id == "1801"  # a configured hw_id is kept


def test_started_anchor_after_the_backend_returned(session_paths):
    with CameraSession([behaviour()]) as cams:
        cams.start(session_paths)
    [group] = read_cameras_record(session_paths["session_folder"]).groups
    assert group.start.monotonic_ns <= group.started.monotonic_ns
    assert group.started.monotonic_ns <= group.stop.monotonic_ns


def test_backend_that_cannot_be_imported_names_what_to_install(monkeypatch):
    class _Broken:
        name, value = "rce", "rpi_camera_ensemble.integrations.msw:RceBackend"

        def load(self):
            raise ModuleNotFoundError("No module named 'aiohttp'")

    monkeypatch.setattr("msw_plugin_api.camera.entry_points", lambda group: [_Broken()])
    with pytest.raises(CameraConfigError) as exc:
        CameraSession([pupil()]).open()
    msg = str(exc.value)
    assert "cannot be imported (No module named 'aiohttp')" in msg
    assert "rpi-camera-ensemble[msw]>=0.9.0" in msg
