"""BpodConnectionError: the library raises it instead of sys.exit; the CLI translates to exit 1.

connect_bpod used to call sys.exit(1) on a failed connection, which kills any GUI/RPC caller.
It now raises the typed BpodConnectionError; run_cli catches it at the CLI boundary and exits 1
(the boxed message was already printed), while a non-CLI caller can handle the exception.
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


def test_run_cli_does_not_swallow_other_errors(monkeypatch):
    def _boom(**kwargs):
        raise ValueError("something else")

    monkeypatch.setattr(cli, "patch_logging_levels", lambda: None)
    monkeypatch.setattr(cli, "patch_user_settings", lambda: None)
    monkeypatch.setattr(cli, "parse_args", lambda args: {"command": "x", "func": _boom})

    with pytest.raises(ValueError):  # only BpodConnectionError is translated
        cli.run_cli("cmd", "arg")
