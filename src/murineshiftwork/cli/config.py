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

from murineshiftwork.cli.tasks import (
    _task_yaml_path,
    find_task_by_name,
)
from murineshiftwork.logic.config import (
    load_setup_config,
    load_subject_config,
)
from murineshiftwork.logic.config.ini import deep_merge
from murineshiftwork.logic.machine_config import (
    get_machine_config_path,
    read_machine_config,
    read_open_ephys_url,
    read_ui_url,
    resolve_config_dir,
    resolve_config_dir_with_source,
    resolve_data_dir,
)


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
        print(
            f"  skip  {resolved}  (no overlay; run `msw tasks init-configs {resolved}`)"
        )
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
        yaml.safe_dump(
            merged, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
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
        print(
            "Error: config_dir not set. Run 'msw init <config_dir>' first or pass -cd.",
            file=sys.stderr,
        )
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
        targets = (
            sorted(d.name for d in overlay_root.iterdir() if (d / "task.yaml").exists())
            if overlay_root.is_dir()
            else []
        )
        if not targets:
            print(f"No task overlays found under {overlay_root}.")
            return
    elif kind == "task":
        targets = [name]
    else:
        print(
            "Usage: msw config upgrade task <name> [--dry-run] [--yes]  |  --all",
            file=sys.stderr,
        )
        sys.exit(2)

    changed = sum(_upgrade_task_overlay(t, cfg, dry_run, yes) for t in targets)
    verb = "would update" if dry_run else "updated"
    print(f"\n{verb} {changed} overlay(s).")
    if dry_run:
        print("Dry-run: re-run without --dry-run (add --yes to skip confirmation).")


def _migrate_one_subject(path: Path, dry_run: bool, ts: str) -> int:
    """Migrate one subject YAML to the current schema in place. Returns 1 if (would be) changed.

    Upgrades the schema (v1 -> v2 adds the ``task_state`` container) and seeds the sequence
    task's earned level: ``task_overrides.sequence.start_level`` ->
    ``task_state.sequence.sequences.default.level`` (start_level is the most-recent earned level
    from the retired writeback; the machine-local level JSON is intentionally NOT read here). A
    timestamped ``.bak`` is written before any change.
    """
    import copy

    from murineshiftwork.logic.config.io import (
        SchemaVersionError,
        _migrate_subject_config,
    )

    raw = yaml.safe_load(path.read_text()) or {}
    before = raw.get("schema_version", 0)
    try:
        migrated = _migrate_subject_config(copy.deepcopy(raw))
    except SchemaVersionError as exc:
        print(f"  skip  {path.name}  ({exc})")
        return 0

    # Sequence level seed (only for subjects that carry a sequence start_level).
    start_level = ((migrated.get("task_overrides") or {}).get("sequence") or {}).get(
        "start_level"
    )
    seqs = (
        migrated.setdefault("task_state", {})
        .setdefault("sequence", {})
        .setdefault("sequences", {})
    )
    seeded = False
    if start_level is not None and "default" not in seqs:
        seqs["default"] = {"level": int(start_level), "updated": ts}
        seeded = True
    if not seqs:  # nothing seeded -> drop the empty scaffold we just created
        migrated.get("task_state", {}).pop("sequence", None)

    if migrated == raw:
        return 0
    note = f", seed sequence level={start_level}" if seeded else ""
    print(f"  {path.name}: schema v{before} -> v{migrated['schema_version']}{note}")
    if not dry_run:
        path.with_name(f"{path.name}.bak.{ts}").write_text(path.read_text())
        path.write_text(
            yaml.safe_dump(
                migrated, default_flow_style=False, allow_unicode=True, sort_keys=False
            )
        )
    return 1


def run_config_migrate_subjects(
    config_dir: str = "", dry_run: bool = False, **kwargs
) -> None:
    """Handler for ``msw config migrate-subjects`` - batch-upgrade subject configs in place."""
    cfg = resolve_config_dir(cli_override=config_dir)
    if not cfg:
        print(
            "Error: config_dir not set. Run 'msw init <config_dir>' first or pass -cd.",
            file=sys.stderr,
        )
        sys.exit(1)
    subjects_dir = Path(cfg) / "subjects"
    files = sorted(subjects_dir.glob("*.yaml")) if subjects_dir.is_dir() else []
    if not files:
        print(f"No subject configs under {subjects_dir}.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    changed = sum(_migrate_one_subject(f, dry_run, ts) for f in files)
    verb = "would migrate" if dry_run else "migrated"
    print(f"\n{verb} {changed}/{len(files)} subject config(s).")
    if dry_run:
        print(
            "Dry-run: re-run without --dry-run to apply (a .bak is written per file)."
        )


def _dump_yaml(title: str, path: Path, data: dict) -> None:
    print(f"# {title}: {path}")
    print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), end="")


def _show_named_config(cfg: str, kind: str, name: str, raw: bool) -> None:
    """Render one subject/setup/task config as YAML (resolved by default, on-disk with --raw)."""
    if kind in ("subject", "setup"):
        sub, loader = (
            ("subjects", load_subject_config)
            if kind == "subject"
            else ("setups", load_setup_config)
        )
        path = Path(cfg) / sub / f"{name}.yaml"
        if not path.exists():
            print(f"No {kind} config at {path}", file=sys.stderr)
            sys.exit(1)
        if raw:
            print(f"# {kind} (on disk): {path}")
            print(path.read_text(), end="")
            return
        obj = loader(cfg, name)
        if obj is None:
            print(f"Could not load {kind} config {name}", file=sys.stderr)
            sys.exit(1)
        _dump_yaml(f"{kind} (resolved)", path, obj.model_dump(mode="json"))
        return

    # task: prefer the config-dir overlay (what the operator edits); fall back to bundled.
    overlay = Path(cfg) / "tasks" / name / "task.yaml"
    if overlay.exists():
        src, label = overlay, "task overlay"
    else:
        try:
            src, label = _task_yaml_path(name), "task (bundled default)"
        except Exception:
            print(
                f"No task overlay at {overlay} and no bundled task '{name}'",
                file=sys.stderr,
            )
            sys.exit(1)
    print(f"# {label}: {src}")
    print(src.read_text(), end="")


def run_config_show(
    config_dir: str = "", kind: str = "", name: str = "", raw: bool = False, **kwargs
) -> None:
    """Handler for ``msw config show``.

    With no target: print how the config/data paths resolve - the effective ``config_dir``
    *and which layer set it* (CLI > env > machine config > default), the overlay sub-dirs with
    their YAML counts, the resolved data dir + service URLs, and the main machine config body.
    With ``subject|setup|task <name>``: print that config as YAML (resolved/validated; ``--raw``
    shows the on-disk file instead). So an operator can inspect any config without guessing.
    """
    cfg, source = resolve_config_dir_with_source(cli_override=config_dir)

    if kind:
        if kind not in ("subject", "setup", "task"):
            print(
                f"Error: unknown kind '{kind}' (use subject | setup | task).",
                file=sys.stderr,
            )
            sys.exit(1)
        if not name:
            print(f"Error: 'msw config show {kind}' needs a name.", file=sys.stderr)
            sys.exit(1)
        if not cfg:
            print(
                "Error: config_dir not set. Run 'msw init <config_dir>', set MSW_CONFIG_DIR, "
                "or pass -cd.",
                file=sys.stderr,
            )
            sys.exit(1)
        _show_named_config(cfg, kind, name, raw)
        return

    mc_path = get_machine_config_path()
    print(
        f"machine config : {mc_path} "
        f"({'exists' if mc_path.exists() else 'not present'})"
    )
    if cfg:
        state = "ok" if Path(cfg).is_dir() else "MISSING"
        print(f"config_dir     : {cfg}  [{source}]  {state}")
        for sub in ("setups", "subjects", "tasks"):
            d = Path(cfg) / sub
            n = len(list(d.glob("*.yaml"))) if d.is_dir() else 0
            print(f"  {sub:<8}     : {d}  ({n} yaml)")
    else:
        print(
            "config_dir     : <unset> - run 'msw init <config_dir>', set MSW_CONFIG_DIR, "
            "or pass -cd"
        )
    # resolve_data_dir's override is the --out-path/data dir, NOT config_dir; show the
    # machine-resolved default here (no data override is passed to `config show`).
    print(f"data_dir       : {resolve_data_dir()}")
    print(f"ui_url         : {read_ui_url()}")
    print(f"open_ephys_url : {read_open_ephys_url()}")

    mc = read_machine_config()
    if mc:
        print()
        _dump_yaml("machine config (main)", mc_path, mc)
