"""Hook discovery (load/collect) and execution (run pre/post)."""

from __future__ import annotations

import copy
import importlib
import logging
from typing import Any

from murineshiftwork.hooks.base import SessionAbortError, TaskHook
from murineshiftwork.hooks.context import HookContext
from murineshiftwork.logic.task_settings import attribute_changes


def load_hooks(dotted_paths: list[str]) -> list[TaskHook]:
    """Import and instantiate hook classes from dotted import paths.

    Unknown / un-importable paths are logged as WARNING and skipped.
    """
    hooks: list[TaskHook] = []
    for path in dotted_paths:
        if not path:
            continue
        try:
            module_path, _, class_name = path.rpartition(".")
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            hooks.append(cls())
            logging.debug(f"Loaded hook: {path}")
        except Exception as exc:
            logging.warning(f"Could not load hook '{path}': {exc}")
    return hooks


def collect_hooks(
    setup_config: Any,
    task_settings: dict,
) -> tuple[list[TaskHook], list[TaskHook]]:
    """Collect pre and post hooks from setup config and task settings.

    Order: global (setup YAML) first, then task-specific (task settings).
    """
    pre_paths: list[str] = []
    post_paths: list[str] = []

    if (
        setup_config is not None
        and hasattr(setup_config, "hooks")
        and setup_config.hooks is not None
    ):
        pre_paths.extend(setup_config.hooks.pre_task)
        post_paths.extend(setup_config.hooks.post_task)

    pre_paths.extend(task_settings.get("HOOKS_PRE_TASK") or [])
    post_paths.extend(task_settings.get("HOOKS_POST_TASK") or [])

    return load_hooks(pre_paths), load_hooks(post_paths)


def _snapshot(settings: dict) -> dict | None:
    try:
        return copy.deepcopy(settings)
    except Exception:  # unpicklable injected objects: skip attribution, never the hook
        return None


def run_pre_hooks(
    hooks: list[TaskHook], ctx: HookContext, provenance: dict | None = None
) -> None:
    """Run pre-session hooks in order.

    Non-fatal failures log WARNING and are skipped.
    Fatal failures raise SessionAbortError (caller must clean up hardware).
    When ``provenance`` (the task-settings provenance map) is given, every setting a hook
    changes is attributed to ``hook:<HookClass>``.
    """
    if not hooks:
        return
    logging.info("Pre-hooks: %d to run", len(hooks))
    for hook in hooks:
        name = type(hook).__name__
        logging.info("Pre-hook: %s", name)
        before = _snapshot(ctx.task_settings) if provenance is not None else None
        try:
            hook.pre_run(ctx)
            logging.info("Pre-hook done: %s", name)
            if before is not None:
                changed = attribute_changes(
                    provenance, before, ctx.task_settings, f"hook:{name}"
                )
                if changed:
                    logging.info("Pre-hook %s changed task settings: %s", name, changed)
        except SessionAbortError:
            raise
        except Exception as exc:
            if getattr(hook, "fatal", False):
                raise SessionAbortError(
                    f"Fatal pre-hook {name} aborted session: {exc}"
                ) from exc
            logging.warning(
                f"Pre-hook {name} raised (skipped): {exc}",
                exc_info=True,
            )


def run_post_hooks(hooks: list[TaskHook], ctx: HookContext) -> None:
    """Run post-session hooks in order.

    Non-fatal failures log WARNING and are skipped.
    Fatal failures raise SessionAbortError after all remaining hooks have run.
    """
    if not hooks:
        return
    logging.info("Post-hooks: %d to run", len(hooks))
    first_fatal: SessionAbortError | None = None
    for hook in hooks:
        name = type(hook).__name__
        logging.info("Post-hook: %s", name)
        try:
            hook.post_run(ctx)
            logging.info("Post-hook done: %s", name)
        except SessionAbortError:
            raise
        except Exception as exc:
            if getattr(hook, "fatal", False):
                err = SessionAbortError(
                    f"Fatal post-hook {name} aborted session: {exc}"
                )
                err.__cause__ = exc
                if first_fatal is None:
                    first_fatal = err
            else:
                logging.warning(
                    f"Post-hook {name} raised (skipped): {exc}",
                    exc_info=True,
                )
    if first_fatal is not None:
        raise first_fatal
