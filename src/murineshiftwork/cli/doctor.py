"""``msw doctor``: one report on whether this machine can run a setup.

Checks, in order, each reported as ``ok``, ``warn``, ``fail`` or ``skip``:

1. ``python``: interpreter and platform.
2. ``packages``: msw-core's version and the distributions that register an entry
   point in an ``msw.*`` group (tasks, camera backends, CLI, hooks, ...). The
   entry points are listed, not loaded.
3. ``machine config``: ``~/.murineshiftwork/msw_machine.yaml`` readable as a
   mapping (absent is a warning: env vars or ``-cd`` can stand in).
4. ``config dir`` and ``data dir``: where they resolve and whether they exist.
5. ``config validate``: ``msw config validate`` on the setup; with hardware
   checks, device ports are resolved too (``--check-ports``).
6. Unless ``skip_hardware``:

   - ``devices``: ``preflight_hardware_check`` as ``msw run`` calls it: the data
     dir is created and write-tested, and every declared serial device is opened
     and closed. A port held by another process fails.
   - ``cameras``: ``msw cameras check`` on every group of the setup.
   - ``open ephys``: one GET of ``/api/status`` (3 s timeout), without attaching,
     when the setup's ``open_ephys_url`` or the machine config's
     ``openephys_api_url`` is set. Unreachable is a fail for a setup URL and a
     warning for the machine-config fallback, which non-ephys rigs may carry.

Exit codes: 0 no check failed, 1 a check failed, 2 the config dir or setup is not
found (the report is still printed).
"""

from __future__ import annotations

import contextlib
import io
import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, entry_points, version
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

OE_DEFAULT_PORT = 37497
OE_TIMEOUT_S = 3.0


def _check(name: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, **extra}


def check_python() -> dict[str, Any]:
    return _check(
        "python",
        OK,
        f"{platform.python_version()} ({sys.executable}) on {platform.platform()}",
    )


def check_packages() -> dict[str, Any]:
    """msw-core plus every distribution registering an ``msw.*`` entry point."""
    try:
        core = version("msw-core")
    except PackageNotFoundError:
        core = ""
    plugins: dict[str, dict[str, Any]] = {}
    for ep in entry_points():
        if not ep.group.startswith("msw."):
            continue
        dist = getattr(ep, "dist", None)
        name = dist.metadata["Name"] if dist is not None else f"? ({ep.value})"
        entry = plugins.setdefault(
            name,
            {"version": dist.version if dist is not None else "", "groups": set()},
        )
        entry["groups"].add(ep.group)
    plugins.pop("msw-core", None)
    listed = {
        n: {"version": p["version"], "groups": sorted(p["groups"])}
        for n, p in sorted(plugins.items())
    }
    text = ", ".join(f"{n} {p['version']}" for n, p in listed.items()) or "none"
    return _check(
        "packages",
        OK if core else WARN,
        f"msw-core {core or 'not installed as a distribution'}; plugins: {text}",
        msw_core=core,
        plugins=listed,
    )


def check_machine_config() -> dict[str, Any]:
    import yaml

    from murineshiftwork.logic.machine_config import get_machine_config_path

    path = get_machine_config_path()
    if not path.exists():
        return _check(
            "machine config", WARN, f"{path} not present (env or -cd must set paths)"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return _check("machine config", FAIL, f"{path}: cannot read: {exc}")
    if data is not None and not isinstance(data, dict):
        return _check("machine config", FAIL, f"{path}: not a mapping")
    return _check("machine config", OK, str(path))


def check_dirs(config_dir: str) -> tuple[str, list[dict[str, Any]]]:
    """The resolved config dir (empty when not found) and the two dir checks."""
    from murineshiftwork.logic.machine_config import (
        resolve_config_dir_with_source,
        resolve_data_dir,
    )

    cfg, source = resolve_config_dir_with_source(cli_override=config_dir)
    if cfg and Path(cfg).is_dir():
        cfg_check = _check("config dir", OK, f"{cfg} [{source}]")
    else:
        cfg_check = _check("config dir", FAIL, f"{cfg or 'not set'} [{source}]")
        cfg = ""
    data = resolve_data_dir()
    data_check = _check(
        "data dir",
        OK if Path(data).is_dir() else WARN,
        data if Path(data).is_dir() else f"{data} does not exist yet",
    )
    return cfg, [cfg_check, data_check]


def check_config(cfg: str, setup: str, check_ports: bool) -> dict[str, Any]:
    from murineshiftwork.cli.config_validate import (
        ERROR,
        WARNING,
        UsageError,
        validate_configs,
    )

    try:
        report = validate_configs(cfg, setup=setup, check_ports=check_ports)
    except UsageError as exc:
        return _check("config validate", FAIL, str(exc))
    findings = [f"{f.level}: {f.message}" for rep in report.files for f in rep.findings]
    status = {ERROR: FAIL, WARNING: WARN}.get(report.files[0].status, OK)
    return _check(
        "config validate", status, "; ".join(findings) or "setup config valid"
    )


def check_devices(cfg: str, setup: str, setup_config: Any) -> dict[str, Any]:
    from murineshiftwork.cli.preflight import preflight_hardware_check
    from murineshiftwork.logic.machine_config import resolve_data_dir

    opened: list[str] = []
    problems: list[str] = []
    for name, dev in sorted(setup_config.devices.items()):
        try:
            dev.resolve_port()
        except ValueError:
            problems.append(f"{name} port not resolved on this machine")
        else:
            opened.append(name)
    args = {
        "command": "doctor",
        "setup": setup,
        "subject": "doctor",
        "config_dir": cfg,
        "out_path": resolve_data_dir(),
        "setup_config": setup_config,
    }
    try:
        preflight_hardware_check(args)
    except RuntimeError as exc:
        lines = [ln.strip(" •") for ln in str(exc).splitlines()[1:] if ln.strip()]
        problems = (lines or [str(exc)]) + problems
    if problems:
        return _check("devices", FAIL, "; ".join(problems))
    if not opened:
        return _check("devices", OK, "no devices declared; data dir writable")
    return _check("devices", OK, f"opened {', '.join(opened)}; data dir writable")


def check_cameras(cfg: str, setup: str) -> dict[str, Any]:
    from murineshiftwork.cli.cameras import check_cameras as cameras_check

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cameras_check(setup, config_dir=cfg, as_json=True)
    try:
        out = json.loads(buf.getvalue())
    except ValueError:
        out = {}
    if "error" in out:
        return _check("cameras", FAIL, out["error"])
    units = [(g["name"], u) for g in out.get("groups", []) for u in g["units"]]
    if not units:
        return _check("cameras", OK, "no camera groups")
    bad = [
        f"{g}/{u['name']}: {u['state']}" + (f" ({u['error']})" if u["error"] else "")
        for g, u in units
        if not u["ok"]
    ]
    ready = len(units) - len(bad)
    message = f"{ready}/{len(units)} camera(s) ready"
    if bad:
        message += ": " + "; ".join(bad)
    return _check("cameras", OK if code == 0 else FAIL, message)


def _oe_status_url(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url if "://" in url else f"http://{url}")
    port = parts.port or OE_DEFAULT_PORT
    return f"{parts.scheme}://{parts.hostname}:{port}/api/status"


def check_open_ephys(setup_config: Any) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    from murineshiftwork.logic.machine_config import read_open_ephys_url

    url = getattr(setup_config, "open_ephys_url", "") or ""
    from_setup = bool(url)
    url = url or read_open_ephys_url()
    if not url:
        return _check(
            "open ephys", SKIP, "no open_ephys_url in setup or machine config"
        )
    try:
        status_url = _oe_status_url(url)
    except ValueError as exc:
        return _check("open ephys", FAIL, f"bad URL {url!r}: {exc}")
    source = "setup" if from_setup else "machine config"
    try:
        with urllib.request.urlopen(status_url, timeout=OE_TIMEOUT_S) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return _check(
            "open ephys",
            FAIL if from_setup else WARN,
            f"{status_url} ({source}) not reachable: {reason}",
        )
    try:
        mode = json.loads(body).get("mode", "")
    except (ValueError, AttributeError):
        mode = ""
    return _check(
        "open ephys",
        OK,
        f"{status_url} ({source}) reachable" + (f", mode {mode}" if mode else ""),
    )


def _load_setup(cfg: str, setup: str) -> Any:
    from murineshiftwork.logic.config import load_setup_config

    try:
        return load_setup_config(cfg, setup)
    except Exception:  # noqa: BLE001 - reported by config validate
        return None


def run_checks(
    setup: str, config_dir: str = "", skip_hardware: bool = False
) -> tuple[list[dict[str, Any]], bool]:
    """Every check in order, and whether the config dir and setup were found."""
    checks = [check_python(), check_packages(), check_machine_config()]
    cfg, dir_checks = check_dirs(config_dir)
    checks += dir_checks
    found = bool(cfg) and (Path(cfg) / "setups" / f"{setup}.yaml").is_file()
    if not cfg:
        checks.append(_check("config validate", SKIP, "no config dir"))
    else:
        checks.append(check_config(cfg, setup, check_ports=not skip_hardware))
    hardware = ("devices", "cameras", "open ephys")
    if skip_hardware:
        return checks + [_check(n, SKIP, "--skip-hardware") for n in hardware], found
    setup_config = _load_setup(cfg, setup) if found else None
    if setup_config is None:
        reason = "setup not found" if not found else "setup config invalid"
        return checks + [_check(n, SKIP, reason) for n in hardware], found
    checks.append(check_devices(cfg, setup, setup_config))
    checks.append(check_cameras(cfg, setup))
    checks.append(check_open_ephys(setup_config))
    return checks, found


def doctor(
    setup: str,
    config_dir: str = "",
    skip_hardware: bool = False,
    as_json: bool = False,
) -> int:
    """Run the checks and print the report. Returns the exit code."""
    checks, found = run_checks(setup, config_dir, skip_hardware)
    failed = any(c["status"] == FAIL for c in checks)
    if as_json:
        print(
            json.dumps(
                {"setup": setup, "ok": not failed, "checks": checks},
                indent=2,
                default=str,
            )
        )
    else:
        width = max(len(c["name"]) for c in checks)
        for c in checks:
            print(f"{c['status']:<4}  {c['name']:<{width}}  {c['message']}")
        counts = {s: sum(c["status"] == s for c in checks) for s in (FAIL, WARN)}
        print(f"setup {setup!r}: {counts[FAIL]} fail, {counts[WARN]} warn")
    if not found:
        return EXIT_USAGE
    return EXIT_FAILED if failed else EXIT_OK


def run_doctor(
    setup: str = "",
    config_dir: str = "",
    skip_hardware: bool = False,
    as_json: bool = False,
    **_,
) -> None:
    """Handler for ``msw doctor``."""
    sys.exit(doctor(setup, config_dir, skip_hardware, as_json))
