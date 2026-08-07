"""`_resolve_hook_setup`: TaskProcess reads the resolved setup from RunContext, then falls back.

Spine refactor - TaskProcess prefers the typed RunContext's setup for hook collection, degrading
to the legacy execution_config bundle when a caller has not threaded the context through.
"""

from __future__ import annotations

from types import SimpleNamespace

from murineshiftwork.logic.task_process import (
    _resolve_hook_setup,
    _resolve_run_identifiers,
)


def test_prefers_run_context_setup():
    rc = SimpleNamespace(setup="RC_SETUP")
    ec = SimpleNamespace(setup="EC_SETUP")
    assert _resolve_hook_setup(rc, ec) == "RC_SETUP"


def test_falls_back_to_execution_config_when_no_context():
    ec = SimpleNamespace(setup="EC_SETUP")
    assert _resolve_hook_setup(None, ec) == "EC_SETUP"


def test_none_when_neither_present():
    assert _resolve_hook_setup(None, None) is None


# --------------------------------------------------------------------------- #
# _resolve_run_identifiers: (debug, session_type, acq_type, session_version)


def test_run_identifiers_prefer_run_context():
    ctx = SimpleNamespace(
        debug=True, session_type="opto", acq_type="video_flir", session_version=3
    )
    # kwargs values are ignored when the context is present
    got = _resolve_run_identifiers(
        {"run_context": ctx, "debug": False, "acq_type": "msw"}
    )
    assert got == (True, "opto", "video_flir", 3)


def test_run_identifiers_fall_back_to_kwargs():
    got = _resolve_run_identifiers(
        {"debug": True, "session_type": "x", "acq_type": "pxi", "session_version": 2}
    )
    assert got == (True, "x", "pxi", 2)


def test_run_identifiers_fallback_defaults():
    # no context, missing keys: debug -> False, the rest -> None (caller applies or-defaults)
    assert _resolve_run_identifiers({}) == (False, None, None, None)
