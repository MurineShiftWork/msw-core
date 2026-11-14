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


def _write_setup(config_dir, name, task_overrides):
    path = config_dir / "setups" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"name": name, "task_overrides": task_overrides}),
        encoding="utf-8",
    )


def test_setup_task_overrides_applied_below_subject(run_evaluate):
    config_dir = run_evaluate.config_dir
    _write_setup(
        config_dir,
        "rig1",
        {"_test_flush_valves": {"N_FLUSH_CYCLES": 5, "record_video": True}},
    )
    _write_subject(config_dir, "subj001")
    out = run_evaluate(setup="rig1")
    assert out["settings.task.patched"]["N_FLUSH_CYCLES"] == 5
    assert out["settings.task.patched"]["record_video"] is True
    label = f"setup:{config_dir / 'setups' / 'rig1.yaml'}"
    assert out["settings.task.provenance"]["N_FLUSH_CYCLES"] == label
    assert out["settings.task.provenance"]["record_video"] == label
    assert out["run_context"].task_settings["record_video"] is True

    _write_subject(
        config_dir,
        "subj001",
        task_overrides={"_test_flush_valves": {"N_FLUSH_CYCLES": 7}},
    )
    out = run_evaluate(setup="rig1")
    assert out["settings.task.patched"]["N_FLUSH_CYCLES"] == 7
    assert out["settings.task.provenance"]["N_FLUSH_CYCLES"].startswith("subject:")
    assert out["settings.task.patched"]["record_video"] is True


def test_setup_without_task_overrides_leaves_patched_unchanged(run_evaluate):
    config_dir = run_evaluate.config_dir
    _write_subject(config_dir, "subj001")
    baseline = run_evaluate()
    path = config_dir / "setups" / "rig1.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"name": "rig1"}), encoding="utf-8")
    out = run_evaluate(setup="rig1")
    # a loaded setup still injects its valve lookup after resolution; compare the rest
    patched = {k: v for k, v in out["settings.task.patched"].items() if not callable(v)}
    assert patched == baseline["settings.task.patched"]
    assert out["settings.task.provenance"] == baseline["settings.task.provenance"]


# --- record_cameras through the settings layers ---


@pytest.fixture
def camera_setup(run_evaluate, tmp_path):
    """A setup "rig1" with RCE cameras top/left/bottom and per-task overrides."""
    ensemble = tmp_path / "ensemble.yaml"
    ensemble.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    f"agent-{i}": {"camera_config": {"description": name}}
                    for i, name in enumerate(["top", "left", "bottom"])
                }
            }
        ),
        encoding="utf-8",
    )
    config_dir = run_evaluate.config_dir

    def _write(task_overrides):
        path = config_dir / "setups" / "rig1.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "name": "rig1",
                    "cameras": {"backend": "rce", "config": str(ensemble)},
                    "task_overrides": task_overrides,
                }
            ),
            encoding="utf-8",
        )
        return f"setup:{path}"

    return _write


def test_record_cameras_all_from_setup_task_overrides(run_evaluate, camera_setup):
    label = camera_setup(
        {"_test_flush_valves": {"record_video": True, "record_cameras": "all"}}
    )
    _write_subject(run_evaluate.config_dir, "subj001")
    out = run_evaluate(setup="rig1")
    assert out["settings.task.patched"]["record_cameras"] == ["top", "left", "bottom"]
    assert out["settings.task.provenance"]["record_cameras"] == label
    assert out["cameras_config"].selected is None


def test_subject_record_cameras_wins_over_setup(run_evaluate, camera_setup):
    camera_setup({"_test_flush_valves": {"record_cameras": "all"}})
    _write_subject(
        run_evaluate.config_dir,
        "subj001",
        task_overrides={"_test_flush_valves": {"record_cameras": ["bottom"]}},
    )
    out = run_evaluate(setup="rig1")
    assert out["settings.task.patched"]["record_cameras"] == ["bottom"]
    assert out["settings.task.provenance"]["record_cameras"].startswith("subject:")
    assert out["cameras_config"].selected == ["bottom"]


def test_cli_record_cameras_all_wins_over_subject(run_evaluate, camera_setup):
    camera_setup({})
    _write_subject(
        run_evaluate.config_dir,
        "subj001",
        task_overrides={"_test_flush_valves": {"record_cameras": ["bottom"]}},
    )
    out = run_evaluate(setup="rig1", task_settings_overrides=["record_cameras=all"])
    assert out["settings.task.patched"]["record_cameras"] == ["top", "left", "bottom"]
    assert out["cameras_config"].selected is None


def test_record_cameras_unset_records_all_cameras(run_evaluate, camera_setup):
    camera_setup({})
    _write_subject(run_evaluate.config_dir, "subj001")
    out = run_evaluate(setup="rig1")
    assert "record_cameras" not in out["settings.task.patched"]
    assert out["cameras_config"].selected is None


def test_unknown_record_camera_from_setup_refuses_the_run(run_evaluate, camera_setup):
    camera_setup({"_test_flush_valves": {"record_cameras": ["front"]}})
    _write_subject(run_evaluate.config_dir, "subj001")
    with pytest.raises(ValueError, match="unknown camera"):
        run_evaluate(setup="rig1")
