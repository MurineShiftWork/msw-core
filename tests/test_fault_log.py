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
