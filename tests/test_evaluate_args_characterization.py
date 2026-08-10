"""Characterization of `evaluate_args` output - the resolved args_dict shape.

`evaluate_args` is the resolution pipeline the spine refactor (Phase 2b: build RunContext directly)
will restructure. This snapshots the exact set of keys it produces for a representative run and the
RunContext it derives, so any change to the resolved shape is caught and reviewed deliberately.

Driven in `--simulate --debug` mode with `_test_subject`, which needs no on-disk subject/setup
config and skips preflight; the five I/O side effects (logging setup, preflight, host session, host
name/ip) are mocked so the pipeline is pure and deterministic.
"""

from __future__ import annotations

from unittest.mock import patch

from murineshiftwork.cli import evaluate as ev
from murineshiftwork.logic.run_context import RunContext

# The resolved keys evaluate_args produces for a no-setup simulate/debug run. `original` (a copy of
# the input) is excluded. A no-setup run resolves no serial ports, so `serial_port_*` are absent -
# TaskProcess defaults them to None (see the boundary note below).
_EXPECTED_KEYS = {
    "acq_type",
    "calibration_file_liquid",
    "calibration_file_sound",
    "calibration_file_stage",
    "command",
    "config_dir",
    "config_file_camera",
    "config_file_subjects",
    "config_file_task",
    "config_file_task_overlay",
    "data_dir",
    "debug",
    "execution_config",
    "host",
    "host_ip",
    "host_name",
    "log_file",
    "log_level",
    "meta_experimenter",
    "metadata",
    "out_path",
    "run_context",
    "session_type",
    "session_version",
    "settings.stage",
    "settings.task.default",
    "settings.task.patched",
    "setup",
    "setup_config",
    "simulate",
    "subcommand",
    "subject",
    "subject_config",
    "task",
    "task_dir",
    "task_mode",
    "task_settings_overrides",
}


def _run_evaluate() -> dict:
    args = {
        "command": "run",
        "subcommand": "",
        "task": "periodic_trigger",
        "subject": "_test_subject",
        "setup": "",
        "config_dir": "",
        "out_path": "/tmp/charz",
        "data_dir": "/tmp/charz",
        "simulate": True,
        "debug": True,
        "log_file": "",
        "log_level": None,
        "session_type": "",
        "task_mode": "",
        "task_settings_overrides": [],
        "host": "",
        "meta_experimenter": "",
    }
    with (
        patch.object(ev, "setup_logging"),
        patch.object(ev, "preflight_hardware_check"),
        patch.object(ev, "_resolve_host_session"),
        patch.object(ev, "get_host_name", return_value="H"),
        patch.object(ev, "get_host_ip", return_value="0.0.0.0"),
    ):
        return ev.evaluate_args(args_dict=dict(args))


def test_evaluate_args_resolved_key_shape():
    out = _run_evaluate()
    assert {k for k in out if k != "original"} == _EXPECTED_KEYS


def test_evaluate_args_builds_the_run_context():
    out = _run_evaluate()
    ctx = out["run_context"]
    assert isinstance(ctx, RunContext)
    assert ctx.task_name == "periodic_trigger"
    assert ctx.subject == "_test_subject"
    assert ctx.simulate is True
    assert ctx.debug is True
    assert ctx.acq_type == "msw"
    # execution_config is derived from the same context, not built independently
    assert out["execution_config"] is not None


def test_boundary_projection_vs_resolved_dict():
    """to_task_kwargs's non-port keys are already in the resolved dict; the serial_port_* keys are
    the one addition (evaluate omits them for a no-setup run, TaskProcess defaults them to None,
    to_task_kwargs emits "" - all falsy, so equivalent at the boundary). Pins that relationship."""
    out = _run_evaluate()
    tk = out["run_context"].to_task_kwargs()
    port_keys = {
        "serial_port_bpod",
        "serial_port_stage",
        "serial_port_scale",
        "serial_port_pulsepal",
    }
    assert set(tk) - port_keys <= set(out)
    assert set(tk) - set(out) == port_keys  # the only keys to_task_kwargs adds
