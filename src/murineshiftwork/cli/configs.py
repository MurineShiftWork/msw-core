"""``msw configs archive``: retire config files into ``archive/`` of the configs repo.

Each file moves with ``git mv`` to ``archive/<original relative path>``, so history
follows and the move is staged but never committed (the operator reviews and commits).

Note: no loader needs an archive exclusion. Subject, setup, task-overlay and device
lookups only read ``<config_dir>/<kind>/...`` (non-recursive), and ``archive/`` is a
sibling of those directories, so archived files are never found. Keep it that way:
do not add a recursive glob from the config root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from murineshiftwork.logic.machine_config import resolve_config_dir

ARCHIVE_DIR = "archive"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def _locate(arg: str, root: Path) -> Path:
    """Resolve a path argument: relative to cwd if it exists there, else to the repo."""
    p = Path(arg).expanduser()
    if not p.is_absolute() and not (Path.cwd() / p).exists():
        p = root / p
    return p.resolve()


def _plan_move(arg: str, root: Path) -> tuple[Path, Path]:
    """Return ``(source, target)`` for one path, or raise ValueError saying why not."""
    src = _locate(arg, root)
    try:
        rel = src.relative_to(root)
    except ValueError:
        raise ValueError(f"{arg}: outside the configs repo ({root})") from None
    if rel.parts[0] == ARCHIVE_DIR:
        raise ValueError(f"{arg}: already under {ARCHIVE_DIR}/")
    if not src.is_file():
        raise ValueError(f"{arg}: not a file")
    if _git(root, "ls-files", "--error-unmatch", "--", str(rel)).returncode != 0:
        raise ValueError(f"{arg}: not tracked by git")
    target = root / ARCHIVE_DIR / rel
    if target.exists():
        raise ValueError(f"{arg}: target exists ({ARCHIVE_DIR}/{rel})")
    return src, target


def run_configs_archive(
    paths: list[str] | None = None,
    config_dir: str = "",
    dry_run: bool = False,
    **kwargs,
) -> None:
    """Handler for ``msw configs archive``.

    Validates every path first and moves nothing unless all of them pass, so a
    typo never leaves a half-archived batch.
    """
    cfg = resolve_config_dir(cli_override=config_dir)
    if not cfg:
        print(
            "Error: config_dir not set. Run 'msw init <config_dir>' first or pass -cd.",
            file=sys.stderr,
        )
        sys.exit(1)
    root = Path(cfg).expanduser().resolve()
    if _git(root, "rev-parse", "--show-toplevel").returncode != 0:
        print(f"Error: {root} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    plan, errors = [], []
    for arg in paths or []:
        try:
            plan.append(_plan_move(arg, root))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        print("Nothing archived.", file=sys.stderr)
        sys.exit(1)

    for src, target in plan:
        rel_src, rel_target = src.relative_to(root), target.relative_to(root)
        if dry_run:
            print(f"  would move  {rel_src} -> {rel_target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        res = _git(root, "mv", "--", str(rel_src), str(rel_target))
        if res.returncode != 0:
            print(f"Error: git mv {rel_src}: {res.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        print(f"  moved  {rel_src} -> {rel_target}")

    if dry_run:
        print("Dry-run: nothing moved.")
    else:
        print(
            f"\nArchived {len(plan)} file(s). Moves are staged, not committed: "
            "review with `git status` and commit them yourself."
        )
