"""Per-setup session lock: refuse a live owner, recover from a dead one."""

from __future__ import annotations

import importlib
import logging
import os
import socket
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


def test_simulated_run_takes_no_lock(monkeypatch, tmp_path):
    _write_lock(pid=os.getppid())
    _run(monkeypatch, tmp_path, simulate=True)  # a live lock does not refuse it


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
