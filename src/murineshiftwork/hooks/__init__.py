"""Pre/post task hook system.

Hooks run around every task session at two call-points:
  pre_run : after Bpod connects, before TaskRunner.prepare(); may mutate task_settings
  post_run: in TaskProcess.__exit__, before Bpod disconnects; may read session output

Hook classes are referenced by dotted import path and instantiated at session start.

Error handling:
  fatal = False (default): a raising hook logs WARNING and is skipped; session continues.
  fatal = True:            a raising hook raises SessionAbortError; session is aborted.
                           Pre-hook abort: TaskProcess closes Bpod before propagating.
                           Post-hook abort: Bpod is closed, then exception propagates.

Layout:
  context : HookContext (shared per-session state)
  base    : TaskHook base class + SessionAbortError
  runner  : load_hooks / collect_hooks / run_pre_hooks / run_post_hooks
"""

from murineshiftwork.hooks.base import SessionAbortError, TaskHook
from murineshiftwork.hooks.context import HookContext
from murineshiftwork.hooks.runner import (
    collect_hooks,
    load_hooks,
    run_post_hooks,
    run_pre_hooks,
)

__all__ = [
    "HookContext",
    "SessionAbortError",
    "TaskHook",
    "collect_hooks",
    "load_hooks",
    "run_post_hooks",
    "run_pre_hooks",
]
