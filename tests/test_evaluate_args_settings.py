"""evaluate_args: what lands in ``settings.task.patched`` for a registered subject.

Runs the full pipeline against the ``fake_tasks`` entry-point package with a config_dir holding a
subject YAML. Logging setup, host lookups and preflight are stubbed so the run is pure.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml

from murineshiftwork.cli import evaluate as ev


def _write_subject(config_dir, name, task_overrides=None):
    path = config_dir / "subjects" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"name": name, "aliases": [], "task_overrides": task_overrides or {}}
        ),
        encoding="utf-8",
    )


@pytest.fixture
def run_evaluate(tmp_path, fake_tasks):
    """Return ``run(**overrides)``: evaluate a run of _test_flush_valves for subject subj001."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    def _run(**overrides):
        args = {
            "command": "run",
            "subject": "subj001",
            "task": "_test_flush_valves",
            "task_dir": "",
            "setup": "",
            "config_dir": str(config_dir),
            "out_path": str(tmp_path / "data"),
            "serial_port_stage": "/dev/ttyUSB0",
            "log_level": "INFO",
            "log_file": "",
            "debug": True,
            "task_settings_overrides": [],
        }
        args.update(overrides)
        with (
            patch.object(ev, "setup_logging"),
            patch.object(ev, "preflight_hardware_check"),
            patch.object(ev, "get_host_name", return_value="H"),
            patch.object(ev, "get_host_ip", return_value="0.0.0.0"),
        ):
            return ev.evaluate_args(args_dict=args)

    _run.config_dir = config_dir  # type: ignore[attr-defined]
    return _run


def test_task_yaml_defaults_reach_patched(run_evaluate):
    _write_subject(run_evaluate.config_dir, "subj001")
    out = run_evaluate()
    assert out["subject"] == "subj001"  # registered subject is not replaced in debug
    assert out["settings.task.patched"]["N_FLUSH_CYCLES"] == 3


def test_calibration_file_and_ports_injected_into_patched(run_evaluate, tmp_path):
    _write_subject(run_evaluate.config_dir, "subj001")
    cal = tmp_path / "cal.csv"
    cal.touch()
    patched = run_evaluate(calibration_file_liquid=str(cal))["settings.task.patched"]
    assert patched["calibration_file_liquid"] == str(cal)
    assert patched["serial_port_stage"] == "/dev/ttyUSB0"


def test_cli_override_beats_injected_calibration_file(run_evaluate, tmp_path):
    _write_subject(run_evaluate.config_dir, "subj001")
    default, override = tmp_path / "default.csv", tmp_path / "override.csv"
    default.touch()
    override.touch()
    patched = run_evaluate(
        calibration_file_liquid=str(default),
        task_settings_overrides=[f"calibration_file_liquid={override}"],
    )["settings.task.patched"]
    assert patched["calibration_file_liquid"] == str(override)


def test_subject_task_overrides_applied(run_evaluate):
    _write_subject(
        run_evaluate.config_dir,
        "subj001",
        task_overrides={"_test_flush_valves": {"N_FLUSH_CYCLES": 7}},
    )
    patched = run_evaluate()["settings.task.patched"]
    assert patched["N_FLUSH_CYCLES"] == 7


def test_unknown_task_lists_available(run_evaluate):
    with pytest.raises(ValueError, match="Unknown task 'no_such_task'") as exc:
        run_evaluate(task="no_such_task")
    assert "demo_sequence" in str(exc.value)
