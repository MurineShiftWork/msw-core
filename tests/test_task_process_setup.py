"""`_resolve_hook_setup`: TaskProcess reads the resolved setup from RunContext, then falls back.

Spine refactor - TaskProcess prefers the typed RunContext's setup for hook collection, degrading
to the legacy execution_config bundle when a caller has not threaded the context through.
"""

from __future__ import annotations

from types import SimpleNamespace

from murineshiftwork.logic.task_process import _resolve_hook_setup


def test_prefers_run_context_setup():
    rc = SimpleNamespace(setup="RC_SETUP")
    ec = SimpleNamespace(setup="EC_SETUP")
    assert _resolve_hook_setup(rc, ec) == "RC_SETUP"


def test_falls_back_to_execution_config_when_no_context():
    ec = SimpleNamespace(setup="EC_SETUP")
    assert _resolve_hook_setup(None, ec) == "EC_SETUP"


def test_none_when_neither_present():
    assert _resolve_hook_setup(None, None) is None
