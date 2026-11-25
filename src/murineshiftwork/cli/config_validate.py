"""``msw config validate``: check setup and subject YAMLs without starting a run.

Each file is read and validated through the same pydantic models a run loads it
with (``SetupConfig``, ``SubjectConfig`` after the subject schema migration), so a
file that passes here loads in a run. On top of the model, a file gets:

- setup: ``name`` matching the file stem; ``task_overrides`` naming tasks not
  installed on this machine; no ``bpod_valve`` calibration on a setup with a Bpod
  (a run then falls back to generic open times); calibrations older than
  ``stale_days``; with ``check_ports``, device ports that do not resolve on this
  machine (an unresolvable ``port_by_path``, a missing ``/dev`` node). Ports are
  checked only for the one setup asked for: a config dir is shared by the machines
  of several setups and records no setup to machine binding, so over every setup
  the ports of other machines' setups would fail here. Without a setup the report
  notes that ports were not checked.
- subject: ``schema_version`` missing, older (a run migrates it in memory; ``msw
  config migrate-subjects`` writes it) or newer than this software
  (``SUBJECT_CONFIG_SCHEMA_VERSION``); ``name`` matching the file stem;
  ``task_overrides`` naming tasks not installed here.

Findings are ``error`` (the file does not load, or a run on it fails) or
``warning`` (it loads, but something is stale or does not match). Task names are
checked against the tasks installed on this machine, which differ between rigs
sharing a config dir, so an unknown task is a warning. Which valves a task
rewards is not checked: a setup names no tasks of its own and tasks declare their
valves under task-specific keys.

Exit codes: 0 no errors (and no warnings with ``strict``), 1 findings, 2 usage or
I/O (config dir not set, setup not found, nothing to validate).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_USAGE = 2

ERROR = "error"
WARNING = "warning"


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class FileReport:
    kind: str
    name: str
    path: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, message: str) -> None:
        self.findings.append(Finding(level, message))

    @property
    def status(self) -> str:
        levels = {f.level for f in self.findings}
        if ERROR in levels:
            return ERROR
        return WARNING if WARNING in levels else "ok"


@dataclass
class ValidationReport:
    config_dir: str
    files: list[FileReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def count(self, level: str) -> int:
        return sum(f.status == level for f in self.files)

    def ok(self, strict: bool = False) -> bool:
        bad = {ERROR, WARNING} if strict else {ERROR}
        return not any(f.status in bad for f in self.files)

    def to_dict(self, strict: bool = False) -> dict[str, Any]:
        return {
            "config_dir": self.config_dir,
            "ok": self.ok(strict),
            "files": [{**asdict(f), "status": f.status} for f in self.files],
            "notes": self.notes,
        }


class UsageError(Exception):
    """Nothing can be validated: no config dir, an unknown setup, no files."""


class _Tasks:
    """The task names installed on this machine, discovered once on first use."""

    def __init__(self) -> None:
        self._names: set[str] | None = None

    def names(self, report: ValidationReport) -> set[str]:
        if self._names is None:
            from murineshiftwork.cli.tasks import list_available_tasks

            try:
                self._names = set(list_available_tasks())
            except Exception as exc:  # noqa: BLE001 - a broken task package
                report.notes.append(f"task discovery failed ({exc})")
                self._names = set()
            if not self._names:
                report.notes.append(
                    "no tasks installed: task_overrides task names not checked"
                )
        return self._names


def _read_yaml(path: Path, rep: FileReport) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        rep.add(ERROR, f"cannot read: {exc}")
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        rep.add(ERROR, f"top level is a {type(data).__name__}, not a mapping")
        return None
    return data


def _model_errors(exc: Any, rep: FileReport) -> None:
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        rep.add(ERROR, f"{loc}: {err.get('msg', '')}")


def _check_name(data_name: str, path: Path, rep: FileReport) -> None:
    if data_name != path.stem:
        rep.add(WARNING, f"name {data_name!r} does not match the file name")


def _check_task_names(
    overrides: dict, tasks: _Tasks, report: ValidationReport, rep: FileReport
) -> None:
    if not overrides:
        return
    installed = tasks.names(report)
    if not installed:
        return
    for task in sorted(set(overrides) - installed):
        rep.add(WARNING, f"task_overrides.{task}: no such task installed here")


def _check_calibrations(cfg: Any, rep: FileReport) -> None:
    has_bpod = any(d.type == "bpod" for d in cfg.devices.values())
    valves = cfg.calibrations.bpod_valve
    if has_bpod and not valves:
        rep.add(
            WARNING,
            "no calibrations.bpod_valve: a run uses generic valve open times",
        )
    limit = datetime.now() - timedelta(days=cfg.calibrations.stale_days)
    for port, vc in sorted(valves.items()):
        if not vc.points:
            rep.add(ERROR, f"calibrations.bpod_valve.{port}: no points")
        if not vc.updated:
            continue
        try:
            updated = datetime.fromisoformat(vc.updated)
        except ValueError:
            rep.add(WARNING, f"calibrations.bpod_valve.{port}: bad date {vc.updated!r}")
            continue
        if updated.tzinfo is not None:
            updated = updated.replace(tzinfo=None)
        if updated < limit:
            rep.add(
                WARNING,
                f"calibrations.bpod_valve.{port}: updated {vc.updated[:10]}, older "
                f"than stale_days={cfg.calibrations.stale_days}",
            )


def _check_ports(cfg: Any, rep: FileReport) -> None:
    from murineshiftwork.logic.misc import serial_port_present

    for name, dev in sorted(cfg.devices.items()):
        try:
            port = dev.resolve_port()
        except ValueError as exc:
            rep.add(ERROR, f"devices.{name}: {exc}")
            continue
        if not serial_port_present(port):
            rep.add(ERROR, f"devices.{name}: port {port!r} not present")


def validate_setup_file(
    path: Path,
    tasks: _Tasks,
    report: ValidationReport,
    check_ports: bool = False,
) -> FileReport:
    from pydantic import ValidationError

    from murineshiftwork.logic.config import SetupConfig

    rep = FileReport("setup", path.stem, str(path))
    data = _read_yaml(path, rep)
    if data is None:
        return rep
    try:
        cfg = SetupConfig.model_validate(data)
    except ValidationError as exc:
        _model_errors(exc, rep)
        return rep
    _check_name(cfg.name, path, rep)
    _check_task_names(cfg.task_overrides, tasks, report, rep)
    _check_calibrations(cfg, rep)
    if check_ports:
        _check_ports(cfg, rep)
    return rep


def validate_subject_file(
    path: Path, tasks: _Tasks, report: ValidationReport
) -> FileReport:
    from pydantic import ValidationError

    from murineshiftwork.logic.config import (
        SUBJECT_CONFIG_SCHEMA_VERSION,
        SchemaVersionError,
        SubjectConfig,
    )
    from murineshiftwork.logic.config.io import _migrate_subject_config

    rep = FileReport("subject", path.stem, str(path))
    data = _read_yaml(path, rep)
    if data is None:
        return rep
    current = SUBJECT_CONFIG_SCHEMA_VERSION
    raw_version = data.get("schema_version")
    try:
        version = int(raw_version or 0)
    except (TypeError, ValueError):
        rep.add(ERROR, f"schema_version {raw_version!r} is not an integer")
        return rep
    if raw_version is None:
        rep.add(
            WARNING,
            f"schema_version missing (current {current}): run "
            "'msw config migrate-subjects'",
        )
    elif version < current:
        rep.add(
            WARNING,
            f"schema_version {version} older than {current}: run "
            "'msw config migrate-subjects'",
        )
    try:
        cfg = SubjectConfig.model_validate(_migrate_subject_config(dict(data)))
    except SchemaVersionError as exc:
        rep.add(ERROR, str(exc))
        return rep
    except ValidationError as exc:
        _model_errors(exc, rep)
        return rep
    _check_name(cfg.name, path, rep)
    _check_task_names(cfg.task_overrides, tasks, report, rep)
    return rep


def validate_configs(
    config_dir: str = "",
    setup: str = "",
    subjects: bool = False,
    check_ports: bool = False,
) -> ValidationReport:
    """Validate one setup (``setup``) or every setup, and with ``subjects`` every
    subject config.

    Raises:
        UsageError: No config dir, ``setup`` not found, or no file to validate.
    """
    from murineshiftwork.logic.machine_config import resolve_config_dir

    cfg = resolve_config_dir(cli_override=config_dir)
    if not cfg or not Path(cfg).is_dir():
        raise UsageError(
            f"config_dir not found ({cfg or 'not set'}): run 'msw init <config_dir>', "
            "set MSW_CONFIG_DIR, or pass -cd"
        )
    root = Path(cfg)
    if setup:
        path = root / "setups" / f"{setup}.yaml"
        if not path.is_file():
            raise UsageError(f"no setup config {setup!r} in {root / 'setups'}")
        setup_files = [path]
    else:
        setup_files = sorted((root / "setups").glob("*.yaml"))
    subject_files = sorted((root / "subjects").glob("*.yaml")) if subjects else []
    if not setup_files and not subject_files:
        raise UsageError(f"no setup or subject configs under {root}")

    report = ValidationReport(config_dir=str(root))
    tasks = _Tasks()
    ports = check_ports and bool(setup)
    for path in setup_files:
        report.files.append(validate_setup_file(path, tasks, report, ports))
    for path in subject_files:
        report.files.append(validate_subject_file(path, tasks, report))
    if check_ports and not setup:
        report.notes.append(
            "device ports not checked: with several setups the ports of other "
            "machines' setups would fail here; pass --setup <setup> --check-ports"
        )
    elif not check_ports:
        report.notes.append("device ports not checked (pass --check-ports)")
    return report


def _print_report(report: ValidationReport) -> None:
    for f in report.files:
        print(f"{f.status:<8} {f.kind:<8} {f.name}  ({f.path})")
        for finding in f.findings:
            print(f"    {finding.level:<8} {finding.message}")
    for note in report.notes:
        print(f"note: {note}")
    print(
        f"{len(report.files)} file(s): {report.count(ERROR)} with errors, "
        f"{report.count(WARNING)} with warnings only"
    )


def config_validate(
    config_dir: str = "",
    setup: str = "",
    subjects: bool = False,
    check_ports: bool = False,
    strict: bool = False,
    as_json: bool = False,
) -> int:
    """Validate and print the report. Returns the exit code."""
    try:
        report = validate_configs(config_dir, setup, subjects, check_ports)
    except UsageError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc), "files": []}, indent=2))
        else:
            print(f"config validate: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if as_json:
        print(json.dumps(report.to_dict(strict), indent=2))
    else:
        _print_report(report)
    return EXIT_OK if report.ok(strict) else EXIT_ISSUES


def run_config_validate(
    config_dir: str = "",
    setup: str = "",
    subjects: bool = False,
    check_ports: bool = False,
    strict: bool = False,
    as_json: bool = False,
    **_,
) -> None:
    """Handler for ``msw config validate``."""
    sys.exit(config_validate(config_dir, setup, subjects, check_ports, strict, as_json))
