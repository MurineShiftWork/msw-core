"""BpodConnectionError: the library raises it instead of sys.exit; the CLI translates to exit 1.

Bpod connection used to call sys.exit(1) on failure, which kills any GUI/RPC caller. Opening the
device collection now raises the typed BpodConnectionError; run_cli catches it at the CLI boundary
and exits 1 (the boxed message was already printed), while a non-CLI caller can handle the exception.
Any other error is logged (console and central CLI log) and also exits 1.
"""

from __future__ import annotations

import pytest

from murineshiftwork import cli
from murineshiftwork.hardware.bpod import BpodConnectionError


def test_error_is_runtimeerror_subclass():
    # backward-compat: existing `except RuntimeError` handlers still catch it
    assert issubclass(BpodConnectionError, RuntimeError)


def test_run_cli_translates_bpod_connection_error_to_exit_1(monkeypatch):
    def _boom(**kwargs):
        raise BpodConnectionError("bpod not found")

    monkeypatch.setattr(cli, "patch_logging_levels", lambda: None)
    monkeypatch.setattr(cli, "patch_user_settings", lambda: None)
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "x", "func": _boom})

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli("cmd", "arg")  # >1 arg so run_cli does not append -h
    assert excinfo.value.code == 1


def test_run_cli_logs_other_errors_and_exits_1(monkeypatch, caplog):
    def _boom(**kwargs):
        raise ValueError("something else")

    monkeypatch.setattr(cli, "patch_logging_levels", lambda: None)
    monkeypatch.setattr(cli, "patch_user_settings", lambda: None)
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "x", "func": _boom})

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli("cmd", "arg")
    assert excinfo.value.code == 1
    (record,) = [r for r in caplog.records if r.levelname == "ERROR"]
    assert record.getMessage() == "msw x failed"
    assert "something else" in str(record.exc_info[1])


def test_run_cli_logs_errors_raised_while_evaluating_run_args(monkeypatch, caplog):
    def _preflight_fails(args_dict):
        raise RuntimeError("Pre-flight check failed")

    monkeypatch.setattr(cli, "patch_logging_levels", lambda: None)
    monkeypatch.setattr(cli, "patch_user_settings", lambda: None)
    monkeypatch.setattr(cli, "_print_run_banner", lambda: None)
    monkeypatch.setattr(cli, "evaluate_args", _preflight_fails)
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "run"})

    with pytest.raises(SystemExit):
        cli.run_cli("run", "arg")
    assert any("Pre-flight" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)
