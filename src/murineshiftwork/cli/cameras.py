"""``msw cameras check``: preflight the cameras of a setup without starting a run.

The command resolves the camera groups the way a run does (relative ensemble
paths against the config dir, the run's ``data_dir`` for RCE groups and, with
``--task``, the task's ``record_video`` and ``record_cameras``), then runs
``CameraSession.preflight``: backends are loaded, configured and asked for each
camera's state, and nothing records. The failure policy is not applied, so every
camera is reported. The backends are closed whatever happens, Ctrl+C included.

Exit codes: 0 every camera ready, 1 a camera missing or failed, 2 a config error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_CONFIG_ERROR = 2


class _ConfigError(Exception):
    """The setup, task or camera configuration cannot be checked."""


def _task_settings(task: str, config_dir: str, setup_config) -> dict:
    """The task's settings as a run on this setup resolves them, without a subject."""
    from murineshiftwork.cli.tasks import _task_yaml_path, find_task_by_name
    from murineshiftwork.logic.config import read_config
    from murineshiftwork.logic.task_settings import resolve_task_settings

    name = find_task_by_name(task_name=task)
    if not name:
        raise _ConfigError(f"unknown task {task!r}")
    layers = [(f"task.yaml:{name}", read_config(file=str(_task_yaml_path(name))))]
    overlay = Path(config_dir) / "tasks" / name / "task.yaml"
    if overlay.exists():
        layers.append((f"overlay:{overlay}", read_config(file=str(overlay))))
    resolved = resolve_task_settings(
        task_name=name,
        default_layers=layers,
        task_modes={},
        setup_config=setup_config,
    )
    return resolved.settings


def _session(setup: str, task: str, config_dir: str):
    """The ``CameraSession`` of the groups a run of ``task`` on ``setup`` starts
    (every group without a task)."""
    from murineshiftwork.cli.evaluate import (
        _resolve_camera_groups,
        _resolve_record_cameras,
    )
    from murineshiftwork.hardware.camera.session import CameraSession
    from murineshiftwork.logic.config import load_setup_config
    from murineshiftwork.logic.machine_config import resolve_data_dir

    if not (Path(config_dir) / "setups" / f"{setup}.yaml").exists():
        raise _ConfigError(f"no setup config {setup!r} in {config_dir}/setups")
    setup_config = load_setup_config(config_dir, setup)
    args: dict[str, Any] = {
        "config_dir": config_dir,
        "config_file_camera": "",
        "out_path": resolve_data_dir(),
        "task": task,
    }
    _resolve_camera_groups(args, setup_config)
    if not task:
        return CameraSession(args["camera_groups"])
    patched = _task_settings(task, config_dir, setup_config)
    args["settings.task.patched"] = patched
    _resolve_record_cameras(args, patched)
    groups = args["camera_groups"]
    if patched.get("record_video") and not any(
        g.record == "with_video" for g in groups
    ):
        raise _ConfigError("record_video is on but the setup has no video cameras")
    return CameraSession.from_run(args)


def _report(session) -> list[dict[str, Any]]:
    """Per group and camera: backend, version and the preflight state."""
    groups = []
    for g in session.record().groups:
        groups.append(
            {
                "name": g.name,
                "backend": g.backend,
                "backend_version": g.backend_version,
                "role": g.role,
                "record": g.record,
                "units": [
                    {
                        "name": u.name,
                        "hw_id": u.hw_id,
                        "ok": bool(u.ok),
                        "state": u.state,
                        "error_code": u.error_code,
                        "error": u.error,
                        "extra": u.extra,
                    }
                    for u in g.units
                ],
            }
        )
    return groups


def _print_table(groups: list[dict[str, Any]]) -> None:
    header = (
        "group",
        "backend",
        "version",
        "camera",
        "hw_id",
        "state",
        "code",
        "error",
    )
    rows = [
        (
            g["name"],
            g["backend"],
            g["backend_version"] or "-",
            u["name"],
            u["hw_id"] or "-",
            u["state"] or "-",
            u["error_code"] or "-",
            u["error"],
        )
        for g in groups
        for u in g["units"]
    ]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(7)]
    for row in [header, *rows]:
        cells = [str(c).ljust(w) for c, w in zip(row[:7], widths, strict=True)]
        print("  ".join([*cells, str(row[7])]).rstrip())


def check_cameras(
    setup: str, task: str = "", config_dir: str = "", as_json: bool = False
) -> int:
    """Preflight the cameras a run would start and print one line per camera.

    Returns:
        ``EXIT_READY``, ``EXIT_NOT_READY`` or ``EXIT_CONFIG_ERROR``.
    """
    from msw_plugin_api.camera import CameraConfigError
    from pydantic import ValidationError

    from murineshiftwork.cli.preflight import camera_group_file_error
    from murineshiftwork.hardware.camera.selection import CameraSelectionError
    from murineshiftwork.logic.machine_config import resolve_config_dir

    out: dict[str, Any] = {"setup": setup, "task": task or None}
    session = None
    try:
        cfg = resolve_config_dir(cli_override=config_dir)
        if not cfg or not Path(cfg).is_dir():
            raise _ConfigError(
                "config_dir not set: run 'msw init <config_dir>', set "
                "MSW_CONFIG_DIR, or pass -cd"
            )
        session = _session(setup, task, cfg)
        errors = [e for e in map(camera_group_file_error, session.groups) if e]
        if errors:
            raise _ConfigError("\n".join(errors))
        session.preflight()
    except (_ConfigError, CameraConfigError, CameraSelectionError) as exc:
        return _config_error(out, str(exc), as_json)
    except ValidationError as exc:
        return _config_error(out, f"setup {setup!r}: {exc}", as_json)
    except Exception as exc:  # noqa: BLE001 - a backend refusing its settings
        return _config_error(out, f"{type(exc).__name__}: {exc}", as_json)
    finally:
        if session is not None:
            session.stop("camera check")

    groups = _report(session)
    units = [u for g in groups for u in g["units"]]
    ready = sum(u["ok"] for u in units)
    out.update(ok=ready == len(units), groups=groups)
    if as_json:
        print(json.dumps(out, indent=2, default=str))
    elif not groups:
        print(f"no camera group starts for setup {setup!r}" + _for_task(task))
    else:
        _print_table(groups)
        print(
            f"{ready}/{len(units)} camera(s) ready in {len(groups)} group(s)"
            + _for_task(task)
        )
    return EXIT_READY if out["ok"] else EXIT_NOT_READY


def _for_task(task: str) -> str:
    return f" for task {task!r}" if task else ""


def _config_error(out: dict[str, Any], message: str, as_json: bool) -> int:
    if as_json:
        out.update(ok=False, error=message, groups=[])
        print(json.dumps(out, indent=2))
    else:
        print(f"camera config error: {message}", file=sys.stderr)
    return EXIT_CONFIG_ERROR


def run_cameras_check(
    setup: str = "", task: str = "", config_dir: str = "", as_json: bool = False, **_
) -> None:
    """Handler for ``msw cameras check``."""
    sys.exit(check_cameras(setup, task=task, config_dir=config_dir, as_json=as_json))
