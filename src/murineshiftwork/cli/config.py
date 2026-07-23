"""``msw config upgrade``: add new bundled-default keys to a config overlay.

MSW never writes config files on load; this is the explicit, opt-in path to refresh
an overlay after the bundled ``task.yaml`` gains new fields. It adds only the keys
present in the bundled file but absent from the user's overlay - existing user values
are preserved (the merge lets the overlay win on every key it already has).

Currently supports task overlays (``config_dir/tasks/<name>/task.yaml``). Setup/subject
YAMLs are user-authored without a bundled template, so they are not upgraded here.

Note: the rewrite goes through ``yaml.safe_dump`` and does not preserve YAML comments;
a timestamped ``.bak`` of the previous overlay is written before any change.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml

from murineshiftwork.cli.tasks import _task_yaml_path, find_task_by_name, list_available_tasks
from murineshiftwork.logic.config.ini import deep_merge
from murineshiftwork.logic.machine_config import resolve_config_dir


def _missing_key_paths(bundled: dict, overlay: dict, prefix: str = "") -> list[str]:
    """Dotted paths of keys in ``bundled`` absent from ``overlay`` (recursive)."""
    out: list[str] = []
    for key, val in bundled.items():
        path = f"{prefix}{key}"
        if key not in overlay:
            out.append(path)
        elif isinstance(val, dict) and isinstance(overlay.get(key), dict):
            out.extend(_missing_key_paths(val, overlay[key], path + "."))
    return out


def _upgrade_task_overlay(name: str, config_dir: str, dry_run: bool, yes: bool) -> int:
    """Upgrade one task overlay. Returns 1 if it was (or would be) changed, else 0."""
    resolved = find_task_by_name(task_name=name)
    if not resolved:
        print(f"  skip  {name}  (task not found)")
        return 0
    bundled_path = _task_yaml_path(resolved)
    overlay_path = Path(config_dir) / "tasks" / resolved / "task.yaml"
    if not overlay_path.exists():
        print(f"  skip  {resolved}  (no overlay; run `msw tasks init-configs {resolved}`)")
        return 0

    bundled = yaml.safe_load(bundled_path.read_text()) or {}
    overlay = yaml.safe_load(overlay_path.read_text()) or {}
    missing = _missing_key_paths(bundled, overlay)
    if not missing:
        print(f"  ok    {resolved}  (up to date)")
        return 0

    print(f"  {resolved}: {len(missing)} new bundled key(s):")
    for m in missing:
        print(f"      + {m}")
    if dry_run:
        return 1
    if not yes:
        try:
            if input(f"  Apply to {overlay_path}? [y/N] ").strip().lower() != "y":
                print("  skipped")
                return 0
        except EOFError:
            print("  skipped (no input; use --yes)")
            return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = overlay_path.with_name(f"{overlay_path.name}.bak.{ts}")
    backup.write_text(overlay_path.read_text())
    merged = deep_merge(bundled, overlay)  # bundled keys + user overrides winning
    overlay_path.write_text(
        yaml.safe_dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    print(f"  updated {resolved}  (backup: {backup.name})")
    return 1


def run_config_upgrade(
    kind: str = "",
    name: str = "",
    all: bool = False,
    config_dir: str = "",
    dry_run: bool = False,
    yes: bool = False,
    **kwargs,
) -> None:
    """Handler for ``msw config upgrade``."""
    cfg = resolve_config_dir(cli_override=config_dir)
    if not cfg:
        print("Error: config_dir not set. Run 'msw init <config_dir>' first or pass -cd.", file=sys.stderr)
        sys.exit(1)

    if kind in ("setup", "subject"):
        print(
            f"config upgrade for '{kind}' is not implemented: setup/subject YAMLs are "
            "user-authored without a bundled template. Only task overlays are upgraded.",
            file=sys.stderr,
        )
        sys.exit(2)

    if all or (kind == "task" and not name):
        # every task overlay present in the config dir
        overlay_root = Path(cfg) / "tasks"
        targets = sorted(
            d.name for d in overlay_root.iterdir() if (d / "task.yaml").exists()
        ) if overlay_root.is_dir() else []
        if not targets:
            print(f"No task overlays found under {overlay_root}.")
            return
    elif kind == "task":
        targets = [name]
    else:
        print("Usage: msw config upgrade task <name> [--dry-run] [--yes]  |  --all", file=sys.stderr)
        sys.exit(2)

    changed = sum(_upgrade_task_overlay(t, cfg, dry_run, yes) for t in targets)
    verb = "would update" if dry_run else "updated"
    print(f"\n{verb} {changed} overlay(s).")
    if dry_run:
        print("Dry-run: re-run without --dry-run (add --yes to skip confirmation).")
