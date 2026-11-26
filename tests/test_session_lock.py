"""Per-setup session lock: refuse a live owner, recover from a dead one."""

from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import socket
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, runtime_checkable

import pytest
import yaml
from msw_plugin_api.camera import UNIT_STOPPED, UnitResult
from murineshiftwork.namespace.manifest import (
    append_acquisition_to_session,
    finalize_acquisition_in_session,
    init_session_manifest,
)

from murineshiftwork.cli import execute
from murineshiftwork.hardware.camera import reset as camera_reset
from murineshiftwork.logic import session_lock
from murineshiftwork.logic.config.models import CameraGroup
from murineshiftwork.logic.run_context import RunContext
from murineshiftwork.logic.session_lock import (
    SessionLockError,
    SetupLock,
    interrupt_running_acquisitions,
    lock_path,
    note_session_container,
    pid_alive,
    read_lock,
)


def _write_lock(setup="rig", **fields):
    data = {
        "pid": 999999,
        "host": socket.gethostname(),
        "started_at": "2026-11-24T10:00:00+00:00",
        "setup": setup,
        "session_container": "",
        **fields,
    }
    path = lock_path(setup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(0)


def test_lock_is_written_and_released():
    with SetupLock("rig") as lock:
        data = read_lock(lock.path)
        assert data["pid"] == os.getpid()
        assert data["setup"] == "rig"
        assert data["host"] == socket.gethostname()
        note_session_container("/data/m1/m1__20261124_100000")
        assert read_lock(lock.path)["session_container"].endswith("100000")
    assert not lock.path.exists()


def test_lock_released_on_exception():
    with pytest.raises(RuntimeError), SetupLock("rig") as lock:
        raise RuntimeError("task failed")
    assert not lock.path.exists()


def test_live_owner_refuses():
    path = _write_lock(pid=os.getppid())
    with pytest.raises(SessionLockError, match="running session"):
        SetupLock("rig").acquire()
    assert read_lock(path)["pid"] == os.getppid()  # untouched


def test_owner_on_another_host_refuses():
    _write_lock(host="other-host")
    with pytest.raises(SessionLockError, match="remove"):
        SetupLock("rig").acquire()


def _session_container(tmp_path):
    container = tmp_path / "m1" / "m1__20261124_100000"
    container.mkdir(parents=True)
    init_session_manifest(container, container.name)
    for name in ("behaviour", "video", "done"):
        append_acquisition_to_session(container, name)
    finalize_acquisition_in_session(container, "done", status="complete")
    return container


def _statuses(container):
    data = yaml.safe_load((container / "session_manifest.yaml").read_text())
    return {a["basename"]: a["status"] for a in data["acquisitions"]}


def test_interrupt_running_acquisitions(tmp_path):
    container = _session_container(tmp_path)
    assert interrupt_running_acquisitions(container) == ["behaviour", "video"]
    assert _statuses(container) == {
        "behaviour": "interrupted",
        "video": "interrupted",
        "done": "complete",
    }
    assert interrupt_running_acquisitions(tmp_path / "missing") == []


def test_dead_owner_warns_recovers_and_takes_the_lock(tmp_path, caplog):
    _write_lock(session_container=str(tmp_path / "old"))
    seen = []
    with caplog.at_level(logging.WARNING), SetupLock("rig", on_stale=seen.append):
        assert read_lock(lock_path("rig"))["pid"] == os.getpid()
    assert seen[0]["session_container"] == str(tmp_path / "old")
    assert "did not end cleanly" in caplog.text


def test_unreadable_lock_is_stale():
    lock_path("rig").parent.mkdir(parents=True, exist_ok=True)
    lock_path("rig").write_text(": not yaml : [")
    seen = []
    with SetupLock("rig", on_stale=seen.append):
        pass
    assert seen == [{}]


def test_failing_recovery_does_not_block_the_run():
    _write_lock()

    def _boom(old):
        raise RuntimeError("agent unreachable")

    with SetupLock("rig", on_stale=_boom) as lock:
        assert lock.path.exists()


# --- camera reset ---


@runtime_checkable
class _Resettable(Protocol):
    def reset(self) -> list[UnitResult]: ...


class _Backend:
    acq_type = "video_fake"

    def __init__(self):
        self.calls: list[str] = []

    def configure(self, settings, units):
        self.calls.append("configure")

    def close(self):
        self.calls.append("close")


class _ResettableBackend(_Backend):
    def reset(self):
        self.calls.append("reset")
        return [UnitResult(name="top", ok=True, state=UNIT_STOPPED)]


def test_reset_only_resettable_backends(monkeypatch):
    monkeypatch.setattr(camera_reset, "_resettable_protocol", lambda: _Resettable)
    backends = {"rce": _ResettableBackend(), "flir": _Backend()}
    groups = [
        CameraGroup(name="a", backend="rce"),
        CameraGroup(name="b", backend="flir"),
    ]
    results = camera_reset.reset_camera_groups(groups, backend_loader=backends.get)
    assert list(results) == ["a"]
    assert results["a"][0].state == UNIT_STOPPED
    assert backends["rce"].calls == ["configure", "reset", "close"]
    assert backends["flir"].calls == ["configure", "close"]


def test_reset_skipped_without_the_protocol(monkeypatch, caplog):
    monkeypatch.setattr(camera_reset, "_resettable_protocol", lambda: None)
    with caplog.at_level(logging.INFO):
        out = camera_reset.reset_camera_groups([CameraGroup(name="a", backend="rce")])
    assert out == {}
    assert "no ResettableCameraBackend" in caplog.text


# --- run_task ---


def _run(monkeypatch, tmp_path, *, simulate=False, task=None):
    fake_mod = SimpleNamespace(run_task=task or (lambda **kw: None))
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_mod)
    ctx = RunContext(
        command="run",
        subject="m1",
        task_name="demo",
        setup_name="rig",
        out_path=str(tmp_path),
        simulate=simulate,
    )
    groups = [CameraGroup(name="top", backend="rce")]
    return execute.run_task(
        run_context=ctx, setup_camera_groups=groups, camera_groups=groups
    )


def test_run_task_holds_the_lock_while_the_task_runs(monkeypatch, tmp_path):
    held = []

    def _task(**kw):
        held.append(read_lock(lock_path("rig")))
        assert "setup_camera_groups" not in kw

    _run(monkeypatch, tmp_path, task=_task)
    assert held[0]["pid"] == os.getpid()
    assert not lock_path("rig").exists()


def test_run_task_refuses_a_setup_in_use(monkeypatch, tmp_path):
    _write_lock(pid=os.getppid())
    with pytest.raises(SessionLockError):
        _run(monkeypatch, tmp_path)


def test_simulated_run_refuses_a_setup_in_use(monkeypatch, tmp_path):
    _write_lock(pid=os.getppid())
    with pytest.raises(SessionLockError):
        _run(monkeypatch, tmp_path, simulate=True)


def test_simulated_run_holds_the_lock(monkeypatch, tmp_path):
    held = []
    _run(
        monkeypatch,
        tmp_path,
        simulate=True,
        task=lambda **kw: held.append(read_lock(lock_path("rig"))),
    )
    assert held[0]["pid"] == os.getpid()
    assert not lock_path("rig").exists()


def test_simulated_run_leaves_a_dead_session_lock(monkeypatch, tmp_path):
    container = _session_container(tmp_path)
    path = _write_lock(session_container=str(container))
    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: False)
    loaded = []
    monkeypatch.setattr(camera_reset, "load_backend", loaded.append)
    _run(monkeypatch, tmp_path, simulate=True)
    assert loaded == []
    assert read_lock(path)["pid"] == 999999
    assert _statuses(container)["behaviour"] == "running"


def test_leave_stale_warns_and_takes_no_lock(caplog):
    path = _write_lock()
    with caplog.at_level(logging.WARNING), SetupLock("rig", leave_stale=True):
        assert read_lock(path)["pid"] == 999999
    assert path.exists()
    assert "left for the next run" in caplog.text


# --- clear_stale_lock ---


def test_clear_stale_lock_removes_a_dead_owner(monkeypatch, tmp_path):
    container = _session_container(tmp_path)
    path = _write_lock(session_container=str(container))
    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: False)
    assert session_lock.clear_stale_lock("rig")["pid"] == 999999
    assert not path.exists()
    assert _statuses(container)["behaviour"] == "interrupted"


@pytest.mark.parametrize(
    "fields", [{"pid": os.getppid()}, {"host": "elsewhere"}], ids=["live", "foreign"]
)
def test_clear_stale_lock_keeps_a_live_or_foreign_owner(fields):
    path = _write_lock(**fields)
    assert session_lock.clear_stale_lock("rig") is None
    assert path.exists()


def test_clear_stale_lock_without_a_lock():
    assert session_lock.clear_stale_lock("rig") is None


def test_run_task_recovers_a_dead_session(monkeypatch, tmp_path):
    container = _session_container(tmp_path)
    _write_lock(session_container=str(container))
    monkeypatch.setattr(session_lock, "pid_alive", lambda pid: False)
    monkeypatch.setattr(camera_reset, "_resettable_protocol", lambda: _Resettable)
    backend = _ResettableBackend()
    monkeypatch.setattr(camera_reset, "load_backend", lambda name: backend)
    _run(monkeypatch, tmp_path)
    assert backend.calls == ["configure", "reset", "close"]
    assert _statuses(container)["behaviour"] == "interrupted"
    assert not lock_path("rig").exists()


# --- concurrent acquirers ---

_RACERS = 6


def _mark_recovery(marks: str, old: dict) -> None:
    (Path(marks) / str(os.getpid())).touch()


def _racer_lock(lock_dir: str, marks: str) -> SetupLock:
    return SetupLock(
        "rig", lock_dir=Path(lock_dir), on_stale=partial(_mark_recovery, marks)
    )


def _try_acquire(lock: SetupLock) -> str:
    try:
        lock.acquire()
    except SessionLockError:
        return "refused"
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        return f"error: {exc!r}"
    return "owner"


def _race_acquire(lock_dir, marks, barrier, results, done):
    """One racer (a separate process, so pids differ): take the lock at the barrier,
    report the outcome, hold the lock until every racer has reported."""
    lock = _racer_lock(lock_dir, marks)
    barrier.wait()
    results.put(_try_acquire(lock))
    done.wait(60)
    lock.release()


def _stale_lock(lock_dir: Path) -> Path:
    path = lock_path("rig", lock_dir)
    path.write_text(yaml.safe_dump({"pid": 999999, "host": socket.gethostname()}))
    return path


def _lock_files(lock_dir: Path) -> list[str]:
    return sorted(p.name for p in lock_dir.iterdir())


@pytest.mark.parametrize("owner", ["free", "dead", "live"])
def test_concurrent_acquirers_yield_one_owner(tmp_path, owner):
    lock_dir, marks = tmp_path / "locks", tmp_path / "marks"
    lock_dir.mkdir()
    marks.mkdir()
    if owner == "dead":
        _stale_lock(lock_dir)
    elif owner == "live":  # this test process: alive, and no racer's pid
        lock_path("rig", lock_dir).write_text(
            yaml.safe_dump({"pid": os.getpid(), "host": socket.gethostname()})
        )
    ctx = multiprocessing.get_context("spawn")
    barrier, results, done = ctx.Barrier(_RACERS), ctx.Queue(), ctx.Event()
    procs = [
        ctx.Process(
            target=_race_acquire,
            args=(str(lock_dir), str(marks), barrier, results, done),
        )
        for _ in range(_RACERS)
    ]
    for p in procs:
        p.start()
    try:
        outcomes = sorted(results.get(timeout=120) for _ in procs)
        owner_pid = read_lock(lock_path("rig", lock_dir))["pid"]
    finally:
        done.set()
        for p in procs:
            p.join(30)
    if owner == "live":
        assert outcomes == ["refused"] * _RACERS
        assert owner_pid == os.getpid()
        assert [m.name for m in marks.iterdir()] == []
        assert _lock_files(lock_dir) == ["rig.lock", "rig.lock.guard"]
        return
    assert outcomes == ["owner"] + ["refused"] * (_RACERS - 1)
    assert owner_pid in {p.pid for p in procs}
    # the dead owner's recovery runs once, in the process that took the lock
    expected_marks = [str(owner_pid)] if owner == "dead" else []
    assert [m.name for m in marks.iterdir()] == expected_marks
    assert _lock_files(lock_dir) == ["rig.lock.guard"]


def _pause_after(name, paused, resume):
    """Make ``session_lock.<name>`` signal ``paused`` and wait for ``resume`` after
    its first call returns."""
    original = getattr(session_lock, name)

    def _wrapper(*args, **kwargs):
        out = original(*args, **kwargs)
        if not paused.is_set():
            paused.set()
            resume.wait(60)
        return out

    setattr(session_lock, name, _wrapper)


def _signal_blocked(blocked):
    """Make a failed attempt at the guard set ``blocked``."""
    original = session_lock._try_lock

    def _wrapper(fd):
        taken = original(fd)
        if not taken:
            blocked.set()
        return taken

    session_lock._try_lock = _wrapper


def _paused_acquire(lock_dir, marks, pause_at, paused, resume, results, done):
    _pause_after(pause_at, paused, resume)
    lock = _racer_lock(lock_dir, marks)
    results.put(("first", _try_acquire(lock)))
    done.wait(60)
    lock.release()


def _blocked_acquire(lock_dir, marks, blocked, results, done):
    _signal_blocked(blocked)
    lock = _racer_lock(lock_dir, marks)
    results.put(("second", _try_acquire(lock)))
    done.wait(60)
    lock.release()


@pytest.mark.parametrize(
    "pause_at",
    ["read_lock", "_retry_sharing"],
    ids=["saw_dead_owner", "moved_dead_owner_aside"],
)
def test_takeover_of_a_dead_owner_is_not_interleaved(tmp_path, pause_at):
    """The first run stops right after it read the dead owner's lock, or right
    after it moved that lock aside (the lock name is free then). A second run
    started meanwhile waits for it and then finds a live owner."""
    lock_dir, marks = tmp_path / "locks", tmp_path / "marks"
    lock_dir.mkdir()
    marks.mkdir()
    _stale_lock(lock_dir)
    ctx = multiprocessing.get_context("spawn")
    paused, resume, blocked = ctx.Event(), ctx.Event(), ctx.Event()
    results, done = ctx.Queue(), ctx.Event()
    first = ctx.Process(
        target=_paused_acquire,
        args=(str(lock_dir), str(marks), pause_at, paused, resume, results, done),
    )
    second = ctx.Process(
        target=_blocked_acquire,
        args=(str(lock_dir), str(marks), blocked, results, done),
    )
    procs = [first]
    try:
        first.start()
        assert paused.wait(60)
        procs.append(second)
        second.start()
        assert blocked.wait(60)  # the second run is waiting at the guard
        resume.set()
        outcomes = dict(results.get(timeout=60) for _ in procs)
        owner_pid = read_lock(lock_path("rig", lock_dir))["pid"]
    finally:
        resume.set()
        done.set()
        for p in procs:
            p.join(30)
    assert outcomes == {"first": "owner", "second": "refused"}
    assert owner_pid == first.pid
    assert [m.name for m in marks.iterdir()] == [str(first.pid)]
    assert _lock_files(lock_dir) == ["rig.lock.guard"]
