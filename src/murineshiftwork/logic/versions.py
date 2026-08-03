"""Installed MSW package versions, for the CLI banner and session-log provenance.

One place that enumerates the ``murineshiftwork`` + ``msw-*`` distributions actually installed
in the running environment, so `msw --help`, `msw -v`, the per-run banner, and the session log
all report the exact software provenance from the same source.
"""

from __future__ import annotations

from importlib.metadata import distributions

LICENCE = "BSD-3-Clause-NonCommercial"
COPYRIGHT = f"© Lars B. Rollik | {LICENCE} | github.com/MurineShiftWork/murineshiftwork"


def msw_installed_versions() -> list[tuple[str, str]]:
    """``(name, version)`` for every installed ``murineshiftwork`` / ``msw-*`` distribution.

    ``murineshiftwork`` (the umbrella) first, the rest alphabetical.
    """
    found: dict[str, str] = {}
    for dist in distributions():
        name = (dist.metadata["Name"] or "").strip()
        if name == "murineshiftwork" or name.startswith("msw-"):
            found.setdefault(name, dist.version)  # first wins if duplicated on the path
    ordered: list[tuple[str, str]] = []
    if "murineshiftwork" in found:
        ordered.append(("murineshiftwork", found.pop("murineshiftwork")))
    ordered.extend(sorted(found.items()))
    return ordered


def msw_version_banner(width: int = 78) -> str:
    """Multi-line banner: the installed msw-* versions wrapped to ``width`` + the copyright line.

    ``name version`` pairs are kept intact when wrapping. Used verbatim by the `--help` header,
    `-v`, the per-run banner, and the session log.
    """
    items = [f"{n} {v}" for n, v in msw_installed_versions()] or [
        "murineshiftwork (version unknown)"
    ]
    lines: list[str] = []
    cur = ""
    for it in items:
        add = ("   " + it) if cur else it
        if cur and len(cur) + len(add) > width:
            lines.append(cur)
            cur = it
        else:
            cur += add
    if cur:
        lines.append(cur)
    return "\n".join(lines) + "\n" + COPYRIGHT
