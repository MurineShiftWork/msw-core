"""msw post: post-acquisition data pipeline commands.

Commands:
  msw post clean   Remove known noise events from .msw.csv files (pure Python).
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path


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
