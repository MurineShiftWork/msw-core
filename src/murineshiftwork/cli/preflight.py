"""Pre-flight hardware checks run before any session files are created."""

import contextlib
from pathlib import Path

from murineshiftwork.logic.misc import test_serial_port_is_accessible
from murineshiftwork.logic.paths import test_path_is_writable
from murineshiftwork.logic.run_context import RunContext


def preflight_hardware_check(args_dict: dict) -> None:
    """Check devices and output dir before any session files are created.

    Raises RuntimeError listing ALL failing checks so the user can fix them at once.
    Skipped entirely when ``debug=True`` or subject is ``_test_subject``.
    """
    ctx = args_dict.get("run_context") or RunContext.from_args_dict(args_dict)
    if ctx.simulate or ctx.debug or ctx.subject == "_test_subject":
        return

    errors: list[str] = []
    # setup_config and settings.task.patched are not RunContext-owned scalar keys (the latter is
    # the hook-mutable dict), so they stay dict reads; only the scalar identity + ports come off ctx.
    setup_config = args_dict.get("setup_config")
    task_settings = args_dict.get("settings.task.patched", {})

    # --- Output directory writable ---
    out_path = Path(ctx.out_path)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        errors.append(f"Cannot create output dir {out_path}: {exc}")
    else:
        write_test = out_path / ".msw_preflight_write_test"
        if not test_path_is_writable(write_test):
            errors.append(f"Output directory not writable: {out_path}")

    # --- Serial devices ---
    # Data-driven over every device declared in the setup, so a new device type is checked
    # automatically (no per-type block here). Each declared device's resolved port must be
    # openable. Port resolution matches the session's device build: run context by type, then by
    # name, then the config's own device_port. Without a setup, still check the CLI bpod port.
    if setup_config is not None:
        for dev_name, dev_cfg in getattr(setup_config, "devices", {}).items():
            port = ctx.ports.get(dev_cfg.type) or ctx.ports.get(dev_name)
            if not port:
                with contextlib.suppress(Exception):
                    port = setup_config.device_port(dev_name)
            if port and not test_serial_port_is_accessible(port):
                errors.append(f"{dev_name} serial port not accessible: {port!r}")
    else:
        bpod_port = ctx.ports.bpod
        if bpod_port and not test_serial_port_is_accessible(bpod_port):
            errors.append(f"bpod serial port not accessible: {bpod_port!r}")

    # --- Cameras: every group this run starts needs its backend plugin installed ---
    errors += _camera_errors(args_dict, bool(task_settings.get("record_video", False)))

    if errors:
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        raise RuntimeError(
            f"Pre-flight check failed: fix before session can start:\n{bullet_list}"
        )


def _camera_errors(args_dict: dict, record_video: bool) -> list[str]:
    from msw_plugin_api.camera import camera_backend_names

    from murineshiftwork.hardware.camera.session import backend_install_hint

    groups = args_dict.get("camera_groups") or []
    if record_video and not any(g.record == "with_video" for g in groups):
        return ["record_video is on but the setup has no video cameras"]
    installed = set(camera_backend_names())
    return [
        f"camera group {g.name!r}: backend {g.backend!r} is not installed: "
        f"{backend_install_hint(g.backend)}"
        for g in groups
        if (record_video or g.record == "always") and g.backend not in installed
    ]
