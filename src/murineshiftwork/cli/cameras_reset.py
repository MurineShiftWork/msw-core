"""``msw cameras reset``: end camera recordings a dead session left running.

When a session's process dies, cameras that record on their own hardware (an
RCE Raspberry Pi) keep recording until told to stop. This command resolves the
setup's camera groups the way ``msw cameras check`` does, then per group loads
the backend, ``configure``-s it and, when the backend implements
``msw_plugin_api.camera.ResettableCameraBackend``, calls ``reset()``, which ends
any recording the units hold and reports one ``UnitResult`` per camera. A backend
without ``reset`` is listed as ``no reset``. Every backend is closed afterwards.

A group whose backend cannot be loaded or refuses its settings is reported and
the other groups are still reset.

When every camera reset (or the setup has none to reset), the setup's session
lock is cleared if its owner is dead: that session's ``running`` acquisitions are
marked ``interrupted`` and the lock file removed, as the next run would on taking
it. A live session's lock is never touched; after a failed reset a dead owner's
lock stays, so the next run tries the recovery again.

Exit codes: 0 every reset camera ok, 1 a camera failed to reset, 2 a config
error (setup not found, a backend missing or refusing its settings, an installed
msw-plugin-api without ``ResettableCameraBackend``).
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG_ERROR = 2

NO_RESET = "no reset"


def _plugin_api_version() -> str:
    try:
        return version("msw-plugin-api")
    except PackageNotFoundError:
        return "unknown"


def _group_dict(rep: Any) -> dict[str, Any]:
    """One ``GroupReset`` as the CLI reports it."""
    from murineshiftwork.hardware.camera import reset as camera_reset

    units = [
        {
            "name": r.name,
            "ok": bool(r.ok),
            "state": r.state,
            "error_code": r.error_code,
            "error": r.error,
            "extra": r.extra,
        }
        for r in rep.units
    ]
    if rep.status == camera_reset.RESET:
        result = "ok" if all(u["ok"] for u in units) else "failed"
    else:
        result = {
            camera_reset.NO_RESET: NO_RESET,
            camera_reset.ERROR: "failed",
        }.get(rep.status, "config error")
    return {
        "name": rep.name,
        "backend": rep.backend,
        "result": result,
        "error": rep.error,
        "units": units,
    }


def _reset_groups(groups: list[Any]) -> list[dict[str, Any]]:
    """Reset the groups whose files exist; report the others as config errors."""
    from murineshiftwork.cli.preflight import camera_group_file_error
    from murineshiftwork.hardware.camera.reset import reset_camera_group_reports

    file_errors = {g.name: camera_group_file_error(g) for g in groups}
    reports = {
        rep.name: _group_dict(rep)
        for rep in reset_camera_group_reports(
            [g for g in groups if not file_errors[g.name]]
        )
    }
    return [
        reports.get(g.name)
        or {
            "name": g.name,
            "backend": g.backend,
            "result": "config error",
            "error": file_errors[g.name],
            "units": [],
        }
        for g in groups
    ]


def _print_table(groups: list[dict[str, Any]]) -> None:
    header = ("group", "backend", "camera", "result", "state", "code", "error")
    rows: list[tuple[str, ...]] = []
    for g in groups:
        if not g["units"]:
            rows.append(
                (g["name"], g["backend"], "-", g["result"], "-", "-", g["error"])
            )
        for u in g["units"]:
            rows.append(
                (
                    g["name"],
                    g["backend"],
                    u["name"],
                    "ok" if u["ok"] else "failed",
                    u["state"] or "-",
                    u["error_code"] or "-",
                    u["error"],
                )
            )
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(6)]
    for row in [header, *rows]:
        cells = [str(c).ljust(w) for c, w in zip(row[:6], widths, strict=True)]
        print("  ".join([*cells, str(row[6])]).rstrip())


def reset_cameras(setup: str, config_dir: str = "", as_json: bool = False) -> int:
    """Reset every camera group of ``setup`` and print one line per camera.

    Returns:
        ``EXIT_OK``, ``EXIT_FAILED`` or ``EXIT_CONFIG_ERROR``.
    """
    from msw_plugin_api.camera import CameraConfigError
    from pydantic import ValidationError

    from murineshiftwork.cli.cameras import _ConfigError, _session
    from murineshiftwork.hardware.camera.reset import reset_supported
    from murineshiftwork.logic.machine_config import resolve_config_dir

    out: dict[str, Any] = {"setup": setup}
    try:
        if not reset_supported():
            raise _ConfigError(
                f"the installed msw-plugin-api ({_plugin_api_version()}) is too old: "
                "it has no ResettableCameraBackend; upgrade msw-plugin-api"
            )
        cfg = resolve_config_dir(cli_override=config_dir)
        if not cfg or not Path(cfg).is_dir():
            raise _ConfigError(
                "config_dir not set: run 'msw init <config_dir>', set "
                "MSW_CONFIG_DIR, or pass -cd"
            )
        groups = _session(setup, "", cfg).groups
    except (_ConfigError, CameraConfigError) as exc:
        return _config_error(out, str(exc), as_json)
    except ValidationError as exc:
        return _config_error(out, f"setup {setup!r}: {exc}", as_json)

    reports = _reset_groups(groups)
    results = {r["result"] for r in reports}
    if "config error" in results:
        code = EXIT_CONFIG_ERROR
    elif "failed" in results:
        code = EXIT_FAILED
    else:
        code = EXIT_OK
    cleared = _clear_stale_lock(setup) if code == EXIT_OK else None
    out.update(ok=code == EXIT_OK, groups=reports, stale_lock_cleared=bool(cleared))
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return code
    if cleared is not None:
        print(
            f"removed the lock of a dead session (pid {cleared.get('pid', '?')}, "
            f"session {cleared.get('session_container') or '(no folder)'})"
        )
    if not reports:
        print(f"no camera groups in setup {setup!r}")
        return code
    _print_table(reports)
    units = [u for r in reports for u in r["units"]]
    no_reset = [r["name"] for r in reports if r["result"] == NO_RESET]
    summary = f"{sum(u['ok'] for u in units)}/{len(units)} camera(s) reset"
    if no_reset:
        summary += f"; no reset: {', '.join(no_reset)}"
    print(summary)
    return code


def _clear_stale_lock(setup: str) -> dict[str, Any] | None:
    from murineshiftwork.logic.session_lock import clear_stale_lock

    try:
        return clear_stale_lock(setup)
    except Exception as exc:  # noqa: BLE001 - the cameras are reset; report, go on
        print(f"stale session lock not cleared: {exc}", file=sys.stderr)
        return None


def _config_error(out: dict[str, Any], message: str, as_json: bool) -> int:
    if as_json:
        out.update(ok=False, error=message, groups=[])
        print(json.dumps(out, indent=2))
    else:
        print(f"camera config error: {message}", file=sys.stderr)
    return EXIT_CONFIG_ERROR


def run_cameras_reset(
    setup: str = "", config_dir: str = "", as_json: bool = False, **_
) -> None:
    """Handler for ``msw cameras reset``."""
    sys.exit(reset_cameras(setup, config_dir=config_dir, as_json=as_json))
