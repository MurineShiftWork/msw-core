"""Hook base class and the session-abort signal."""

from __future__ import annotations

from murineshiftwork.hooks.context import HookContext


class SessionAbortError(RuntimeError):
    """Raised by a fatal hook to abort the session before it starts (pre) or after (post)."""


class TaskHook:
    """Base class for session hooks.  Override pre_run and/or post_run.

    Set ``fatal = True`` on the subclass to abort the session when the hook raises
    instead of logging a warning and continuing.  The Bpod connection is always closed
    cleanly before the SessionAbortError propagates.
    """

    fatal: bool = False

    def pre_run(self, ctx: HookContext) -> None:
        pass

    def post_run(self, ctx: HookContext) -> None:
        pass
