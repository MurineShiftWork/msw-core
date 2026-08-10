"""Fault log: faulthandler dumps native crashes to a .fault.log kept only if a fault occurs."""

from __future__ import annotations

import faulthandler

from murineshiftwork.logic.log import _enable_fault_log


def test_fault_log_created_beside_run_log_and_removed_on_clean_exit(tmp_path):
    was_enabled = faulthandler.is_enabled()
    try:
        fault_path, cleanup = _enable_fault_log(tmp_path / "setup--dt--subj--task.log")
        # same stem as the run log, .fault.log suffix, created up front
        assert fault_path == tmp_path / "setup--dt--subj--task.fault.log"
        assert fault_path.exists()
        # a clean exit removes it (no fault happened)
        cleanup()
        assert not fault_path.exists()
    finally:
        if was_enabled and not faulthandler.is_enabled():
            faulthandler.enable()  # restore pytest's faulthandler state


def test_forked_child_does_not_inherit_faulthandler(tmp_path):
    """A forked child (online-plot / relay) must not keep the parent's faulthandler, or its own
    crash would be written into the session fault log and read as a session crash."""
    import os

    import pytest

    if not hasattr(os, "fork"):
        pytest.skip(
            "no os.fork on this platform (Windows uses spawn, so no inheritance)"
        )

    was_enabled = faulthandler.is_enabled()
    fault_path, cleanup = _enable_fault_log(tmp_path / "setup--dt--subj--task.log")
    try:
        assert faulthandler.is_enabled()  # parent has it on
        r, w = os.pipe()
        pid = os.fork()
        if (
            pid == 0
        ):  # child: report whether faulthandler is still enabled, then hard-exit
            os.close(r)
            os.write(w, b"on" if faulthandler.is_enabled() else b"off")
            os.close(w)
            os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
        child_state = os.read(r, 8)
        os.close(r)
        assert child_state == b"off"  # child disabled it via the register_at_fork hook
        assert faulthandler.is_enabled()  # parent unaffected
    finally:
        cleanup()
        if was_enabled and not faulthandler.is_enabled():
            faulthandler.enable()


def test_prune_removes_empty_fault_logs_but_keeps_real_crash_dumps(
    tmp_path, monkeypatch
):
    """Empty .fault.log (SIGTERM, no crash) is pruned; a non-empty one (real crash) is kept."""
    from murineshiftwork.logic import log as logmod

    monkeypatch.setattr(logmod, "_CENTRAL_LOG_DIR", tmp_path)
    empty = tmp_path / "a--dt--s--t.fault.log"
    empty.write_text("")  # 0 bytes -> killed, not crashed
    crash = tmp_path / "b--dt--s--t.fault.log"
    crash.write_text("Fatal Python error: Segmentation fault\n...")  # real dump
    run_log = tmp_path / "c--dt--s--t.log"
    run_log.write_text("some run output")

    logmod._prune_central_logs()

    assert not empty.exists()  # empty fault log pruned
    assert crash.exists()  # real crash dump kept
    assert run_log.exists()  # under the cap, kept
