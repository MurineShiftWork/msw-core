"""msw post: post-acquisition data pipeline commands.

Commands:
  msw post clean   Remove known noise events from .msw.csv files (pure Python).
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path


def _check_setup_locks(dirty_files):
    """Refuse when a file to change belongs to a session whose setup is in use.

    A session's setup is ``process.setup`` of the ``.msw.session.yaml`` next to its
    ``.msw.csv``; a file with no readable session YAML has no known setup and is not
    checked. The lock check is ``logic.session_lock.owner_state``: a live owner, or
    one on another host (liveness unknown), blocks; a dead owner's lock does not.
    """
    import yaml

    from murineshiftwork.logic.session_lock import (
        FOREIGN,
        LIVE,
        lock_path,
        owner_state,
        read_lock,
    )

    held = {}
    for f in dirty_files:
        session_yaml = f.with_name(f.name.removesuffix(".csv") + ".session.yaml")
        try:
            setup = (yaml.safe_load(session_yaml.read_text()) or {})["process"]["setup"]
        except (OSError, yaml.YAMLError, KeyError, TypeError):
            continue
        if not setup or setup in held:
            continue
        path = lock_path(str(setup))
        if owner_state(read_lock(path)) in (LIVE, FOREIGN):
            held[setup] = (f, path)
    if held:
        lines = [
            f"setup {setup!r} is held by a running session (lock {path}); "
            f"not cleaning {f}"
            for setup, (f, path) in held.items()
        ]
        raise SystemExit(
            "msw post clean refused:\n  "
            + "\n  ".join(lines)
            + "\nEnd the session first; if its owner is gone, remove the lock file."
        )


def run_post_clean(**args_dict):
    """Remove noise-event rows from .msw.csv files under data_dir.

    Backs up each modified file as <file>.bak.<timestamp> before editing.
    """
    data_dir = Path(args_dict["data_dir"]).expanduser().resolve()
    event = args_dict.get("event", "Port4")
    dry_run = args_dict.get("dry_run", False)

    if not data_dir.exists():
        raise FileNotFoundError(f"data-dir not found: {data_dir}")

    csv_files = sorted(data_dir.rglob("*.msw.csv"))
    dirty_files = [
        f for f in csv_files if event in f.read_text(encoding="utf-8", errors="replace")
    ]
    _check_setup_locks(dirty_files)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_scanned = 0
    n_modified = 0

    for f in csv_files:
        n_scanned += 1
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
        dirty = [line for line in lines if event in line]
        if not dirty:
            continue
        clean = [line for line in lines if event not in line]
        if dry_run:
            logging.info(f"[dry-run] would remove {len(dirty)} row(s) from {f}")
        else:
            shutil.copy2(f, f.with_suffix(f".bak.{ts}"))
            f.write_text("".join(clean), encoding="utf-8")
            n_modified += 1
            logging.info(f"Cleaned {len(dirty)} row(s): {f}")

    if dry_run:
        logging.info(f"Dry run complete. Scanned {n_scanned} file(s).")
    else:
        logging.info(f"Done. Scanned {n_scanned} file(s), modified {n_modified}.")
