"""Pure functions for building the resolved task-settings dict and its provenance.

Priority chain (lowest → highest):
  1. bundled task.yaml default:
  2. config_dir overlay task.yaml default:
  3. subject YAML task_overrides
  4. CLI --task-mode (named preset from the task.yaml mode: section) -- a mode is shorthand
     for a group of settings, applied at the CLI's position in the chain: it overrides the
     subject config, but a single -ts key below still overrides the mode
  5. CLI -ts KEY=VALUE overrides (dotted keys reach nested settings: -ts a.b.c=1)
  6. extra injections (only for keys not already present)

Modes are chosen per run. A ``task_mode`` stored in a subject's ``task_overrides`` (the former
"sticky mode") is rejected with an error, so a mode is never reapplied silently.

Every leaf of the resolved dict is attributed to the layer that last set it (``provenance``:
dotted key → layer label), so tasks can report where each setting came from.
"""

import ast
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from murineshiftwork.logic.config import deep_merge

LAYER_CLI = "cli"
LAYER_INJECTED = "injected"


class StickyTaskModeError(ValueError):
    """A subject config still carries a ``task_mode`` in its ``task_overrides``."""


@dataclass
class ResolvedTaskSettings:
    """The resolved settings dict, per-leaf provenance, and the mode applied (if any)."""

    settings: dict
    provenance: dict[str, str] = field(default_factory=dict)
    mode: str = ""


def parse_key_value_list(kv_list: list) -> dict:
    """Parse ['KEY=VALUE', ...] into a flat dict with type coercion (keys may be dotted)."""
    result = {}
    for item in kv_list:
        item = item.strip().strip("'\"")
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        k = k.strip()
        v = v.strip()
        try:
            result[k] = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            result[k] = v
    return result


def merge_task_modes(bundled: dict, overlay: dict) -> dict:
    """Merge overlay mode definitions into bundled ones, deep-merging per mode (like defaults)."""
    merged = {name: dict(params or {}) for name, params in (bundled or {}).items()}
    for name, params in (overlay or {}).items():
        merged[name] = deep_merge(merged.get(name, {}), params or {})
    return merged


def _record(provenance: dict, layer: dict, label: str, prefix: str = "") -> None:
    """Attribute every leaf of ``layer`` to ``label`` (a replaced subtree drops stale children)."""
    for key, value in layer.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            provenance.pop(path, None)
            _record(provenance, value, label, f"{path}.")
        else:
            for stale in [p for p in provenance if p.startswith(f"{path}.")]:
                del provenance[stale]
            provenance[path] = label


def attribute_changes(
    provenance: dict, before: dict, after: dict, label: str
) -> list[str]:
    """Attribute every leaf that differs between ``before`` and ``after`` to ``label``.

    Used for layers that edit the resolved dict in place (e.g. pre-run hooks). Returns the
    dotted keys that changed.
    """
    changed: list[str] = []

    def walk(old: Any, new: Any, path: str) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in {**old, **new}:
                sub = f"{path}.{key}" if path else str(key)
                if key not in new:
                    changed.append(sub)
                    for stale in [
                        p for p in provenance if p == sub or p.startswith(f"{sub}.")
                    ]:
                        del provenance[stale]
                else:
                    walk(old.get(key, _MISSING), new[key], sub)
        elif old is _MISSING or old != new:
            changed.append(path)
            _record(provenance, {path.rsplit(".", 1)[-1]: new}, label, _parent(path))

    walk(before, after, "")
    return changed


_MISSING = object()


def _parent(path: str) -> str:
    return f"{path.rsplit('.', 1)[0]}." if "." in path else ""


def _set_dotted(settings: dict, key: str, value: Any) -> tuple[dict, dict]:
    """Return (settings with ``key`` set, the single-key layer that was applied).

    A key that exists literally (e.g. ``settings.stage``) or has no dot replaces the value
    outright; otherwise ``a.b.c`` sets the nested leaf, creating intermediate dicts.
    """
    if key in settings or "." not in key:
        return {**settings, key: value}, {key: value}
    *parents, leaf = key.split(".")
    layer: dict = {leaf: value}
    for part in reversed(parents):
        layer = {part: layer}
    return deep_merge(settings, layer), layer


def resolve_task_settings(
    task_name: str,
    default_layers: list[tuple[str, dict]],
    task_modes: dict,
    subject_config: Any = None,
    subject_label: str = "subject",
    task_mode: str = "",
    cli_overrides: list | None = None,
    extra_injections: dict | None = None,
) -> ResolvedTaskSettings:
    """Resolve task settings through the priority chain, tracking per-leaf provenance.

    Args:
        task_name: canonical task name (used to look up subject YAML overrides).
        default_layers: ``[(label, defaults_dict), ...]`` lowest first, e.g. the bundled
            task.yaml default and the config_dir overlay default.
        task_modes: dict of mode_name → override_dict (see ``merge_task_modes``).
        subject_config: SubjectConfig instance or None.
        subject_label: provenance label for the subject layer (e.g. its YAML path).
        task_mode: the --task-mode given for this run (empty string = no mode).
        cli_overrides: list of 'KEY=VALUE' strings from -ts.
        extra_injections: keys injected only if not already present in the resolved dict.
    """
    patched: dict = {}
    provenance: dict[str, str] = {}
    for label, layer in default_layers:
        if layer:
            patched = deep_merge(patched, layer)
            _record(provenance, layer, label)

    subject_patch = (
        dict(subject_config.task_overrides.get(task_name, {})) if subject_config else {}
    )
    if "task_mode" in subject_patch:
        raise StickyTaskModeError(
            f"{subject_label}: task_overrides.{task_name}.task_mode = "
            f"{subject_patch['task_mode']!r} is no longer supported. Modes are chosen per run "
            f"with --task-mode; remove the key from the subject config (the sequence task's "
            f"`python -m murineshiftwork.tasks.sequence.migrate <config_dir>` does this for you)."
        )

    if subject_patch:
        patched = deep_merge(patched, subject_patch)
        _record(provenance, subject_patch, subject_label)
        logging.debug(f"Subject YAML task_overrides for '{task_name}': {subject_patch}")

    if task_mode:
        if task_mode not in task_modes:
            raise ValueError(
                f"Task mode '{task_mode}' not found in task.yaml 'mode:' section. "
                f"Available: {list(task_modes.keys())}"
            )
        mode_overrides = task_modes[task_mode] or {}
        patched = deep_merge(patched, mode_overrides)
        _record(provenance, mode_overrides, f"mode:{task_mode}")
        logging.debug(f"Task mode '{task_mode}' applied: {mode_overrides}")

    resolved_overrides = parse_key_value_list(cli_overrides or [])
    for key, value in resolved_overrides.items():
        patched, layer = _set_dotted(patched, key, value)
        _record(provenance, layer, LAYER_CLI)
    if resolved_overrides:
        logging.debug(f"CLI task-settings overrides applied: {resolved_overrides}")

    if patched:
        logging.debug(
            f"settings.task.patched for '{task_name}':\n"
            + json.dumps(patched, indent=4, sort_keys=True, default=str)
        )

    for key, value in (extra_injections or {}).items():
        if key not in patched:
            patched[key] = value
            provenance[key] = LAYER_INJECTED

    return ResolvedTaskSettings(settings=patched, provenance=provenance, mode=task_mode)


def build_task_settings(
    task_name: str,
    settings_task_default: dict,
    task_modes: dict,
    subject_config: Any = None,
    task_mode: str = "",
    cli_overrides: list | None = None,
    extra_injections: dict | None = None,
) -> dict:
    """Return only the resolved task-settings dict (single merged default layer).

    Thin wrapper over ``resolve_task_settings`` for callers that need no provenance.
    """
    return resolve_task_settings(
        task_name=task_name,
        default_layers=[("task.yaml", settings_task_default)],
        task_modes=task_modes,
        subject_config=subject_config,
        task_mode=task_mode,
        cli_overrides=cli_overrides,
        extra_injections=extra_injections,
    ).settings
