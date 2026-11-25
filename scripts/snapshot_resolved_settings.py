"""Snapshot the settings ``msw run`` resolves for every combination a config tree defines.

For each task x mode x setup x subject of a config dir, the run arguments are built with the
real ``msw run`` parser and passed through ``evaluate_args``, the same resolution path a run
takes, with no hardware. Each combination is written to its own JSON file:

    <out_dir>/<task>/<mode or _no_mode>/<setup>/<subject>.json

holding the resolved task settings, their provenance, the run-level values the setup resolves
(ports, scale, cameras) and ``valve_s_for_ul`` sampled per calibrated port. A combination that
fails to resolve is written as an ``error`` record. Output is deterministic: sorted keys, and the
config dir, task dirs and output dir replaced by placeholders, so two snapshots of the same tree
compare equal by value.

Machine-local inputs are kept out: the default calibration files under the home directory are
pointed at an empty directory, and ``port_by_path`` ports resolve to their by-path name without
checking that the device exists.

Tasks are those the tree names: overlay dirs under ``tasks/`` and the keys of every
``task_overrides`` in ``setups/`` and ``subjects/``, plus any given with ``--task``. Modes are the
task's bundled and overlay modes, plus no mode.

    uv run python scripts/snapshot_resolved_settings.py <config_dir> <out_dir>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from unittest.mock import patch

import yaml

NO_MODE = "_no_mode"
VALVE_SAMPLE_UL = (1.0, 2.0, 5.0)

# run-level values the setup resolves next to the task settings
_RUN_KEYS = (
    "acq_type",
    "camera_failure",
    "camera_groups",
    "config_file_camera",
    "scale_baudrate",
    "scale_protocol",
    "scale_type",
    "serial_port_bpod",
    "serial_port_pulsepal",
    "serial_port_scale",
    "serial_port_stage",
    "session_version",
)


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _names(config_dir: Path, kind: str) -> list[str]:
    return sorted(p.stem for p in (config_dir / kind).glob("*.yaml"))


def tree_tasks(config_dir: Path) -> list[str]:
    """Tasks the tree names: overlay dirs and ``task_overrides`` keys."""
    tasks = {p.name for p in (config_dir / "tasks").glob("*") if p.is_dir()}
    for kind in ("setups", "subjects"):
        for path in (config_dir / kind).glob("*.yaml"):
            with contextlib.suppress(Exception):
                tasks |= set(_yaml(path).get("task_overrides") or {})
    return sorted(tasks)


def task_modes(config_dir: Path, task: str, task_dir: Path | None) -> list[str]:
    from murineshiftwork.logic.config import read_task_modes
    from murineshiftwork.logic.task_settings import merge_task_modes

    bundled = str(task_dir / "task.yaml") if task_dir else ""
    overlay = config_dir / "tasks" / task / "task.yaml"
    modes = merge_task_modes(
        read_task_modes(bundled) if bundled and Path(bundled).exists() else {},
        read_task_modes(str(overlay)) if overlay.exists() else {},
    )
    return ["", *sorted(modes)]


def combinations(
    config_dir: Path, task_dirs: dict[str, Path], tasks: list[str]
) -> Iterator[tuple[str, str, str, str]]:
    setups = _names(config_dir, "setups")
    subjects = _names(config_dir, "subjects")
    for task in tasks:
        for mode in task_modes(config_dir, task, task_dirs.get(task)):
            for setup in setups:
                for subject in subjects:
                    yield task, mode, setup, subject


class _Normaliser:
    """Turn a resolved value into plain JSON with machine paths replaced by placeholders."""

    def __init__(self, substitutions: dict[Path, str]):
        # both spellings of each path (Windows backslashes, as_posix), longest first, so a
        # path inside another is replaced by its own placeholder
        pairs = {}
        for path, placeholder in substitutions.items():
            pairs[str(path)] = placeholder
            pairs[Path(path).as_posix()] = placeholder
        self._subs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))

    def text(self, s: str) -> str:
        hit = False
        for path, placeholder in self._subs:
            if path in s:
                s = s.replace(path, placeholder)
                hit = True
        return s.replace("\\", "/") if hit else s

    def __call__(self, value):
        from pydantic import BaseModel

        if isinstance(value, BaseModel):
            return self(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {self.text(str(k)): self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted((self(v) for v in value), key=repr)
        if isinstance(value, Path):
            return self.text(str(value))
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if callable(value):
            return "<callable>"
        return f"<{type(value).__name__}>"


def _valve_sample(patched: dict, setup_config) -> dict:
    fn = patched.get("valve_s_for_ul")
    if not callable(fn):
        return {}
    ports = (
        sorted(setup_config.calibrations.bpod_valve)
        if setup_config is not None and setup_config.calibrations.bpod_valve
        else ["1"]
    )
    sample: dict[str, float | str] = {}
    for port in ports:
        for volume in VALVE_SAMPLE_UL:
            key = f"{port}@{volume}"
            try:
                sample[key] = round(float(fn(port, volume)), 6)
            except Exception as exc:
                sample[key] = f"<{type(exc).__name__}>"
    return sample


def _message(exc: BaseException) -> str:
    """The error text without lines that vary by library version (pydantic's doc links)."""
    lines = str(exc).splitlines()
    return "\n".join(ln for ln in lines if "For further information visit" not in ln)


def resolve_one(
    config_dir: Path, out_path: Path, task: str, mode: str, setup: str, subject: str
) -> dict:
    """Resolve one combination through ``msw run``'s parser and ``evaluate_args``."""
    from murineshiftwork.cli import evaluate as ev
    from murineshiftwork.cli.parser import parse_args

    argv = ["run", "-t", task, "-s", subject, "-cd", str(config_dir)]
    argv += ["-o", str(out_path)]
    if setup:
        argv += ["--setup", setup]
    if mode:
        argv += ["--task-mode", mode]
    args = parse_args(args=argv)
    out = ev.evaluate_args(args_dict=args)
    patched = out["settings.task.patched"]
    return {
        "settings": patched,
        "provenance": out["settings.task.provenance"],
        "run": {k: out[k] for k in _RUN_KEYS if k in out},
        "valve_s_for_ul": _valve_sample(patched, out.get("setup_config")),
    }


@contextlib.contextmanager
def _isolated(machine_dir: Path):
    """Stub the run's side effects and machine-local inputs (no hardware, no logging setup)."""
    from murineshiftwork.cli import evaluate as ev
    from murineshiftwork.logic.config.models import SerialDevice

    resolve_port = SerialDevice.resolve_port

    def by_path(self):
        if self.port:
            return resolve_port(self)
        return f"/dev/serial/by-path/{self.port_by_path}"

    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.CRITICAL)
    try:
        with (
            patch.object(ev, "setup_logging"),
            patch.object(ev, "preflight_hardware_check"),
            patch.object(ev, "get_host_name", return_value="host"),
            patch.object(ev, "get_host_ip", return_value="0.0.0.0"),
            patch.object(
                ev,
                "DEFAULT_CALIBRATION_FILE_LIQUID",
                str(machine_dir / "calibration.liquid.default.csv"),
            ),
            patch.object(
                ev,
                "DEFAULT_CALIBRATION_FILE_STAGE",
                str(machine_dir / "calibration.stage.default.yaml"),
            ),
            patch.object(SerialDevice, "resolve_port", by_path),
        ):
            yield
    finally:
        root.setLevel(level)


def snapshot(
    config_dir: str | Path, out_dir: str | Path, extra_tasks: Iterable[str] = ()
) -> Counter:
    """Write one JSON record per combination into ``out_dir``; return counts by outcome."""
    from murineshiftwork.cli.tasks import list_available_tasks

    config_dir = Path(config_dir)
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"{out_dir} is not empty: snapshot into a fresh directory")
    task_dirs = {k: Path(v) for k, v in list_available_tasks(detailed=True).items()}
    tasks = sorted(set(tree_tasks(config_dir)) | set(extra_tasks))
    counts: Counter = Counter()
    with tempfile.TemporaryDirectory() as tmp, _isolated(Path(tmp) / "machine"):
        run_out = Path(tmp) / "out"
        norm = _Normaliser(
            {
                config_dir: "<config_dir>",
                run_out: "<out_path>",
                Path(tmp) / "machine": "<machine>",
                **{d: f"<task_dir:{t}>" for t, d in task_dirs.items()},
            }
        )
        for task, mode, setup, subject in combinations(config_dir, task_dirs, tasks):
            record: dict = {
                "combination": {
                    "task": task,
                    "mode": mode,
                    "setup": setup,
                    "subject": subject,
                }
            }
            try:
                record.update(
                    resolve_one(config_dir, run_out, task, mode, setup, subject)
                )
                counts["resolved"] += 1
            except (Exception, SystemExit) as exc:
                record["error"] = {"type": type(exc).__name__, "message": _message(exc)}
                counts[f"error:{type(exc).__name__}"] += 1
            path = out_dir / task / (mode or NO_MODE) / setup / f"{subject}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(norm(record), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("config_dir")
    p.add_argument("out_dir")
    p.add_argument(
        "--task", action="append", default=[], help="also snapshot this task"
    )
    a = p.parse_args(argv)
    counts = snapshot(a.config_dir, a.out_dir, a.task)
    for outcome, n in sorted(counts.items()):
        print(f"{outcome}\t{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
