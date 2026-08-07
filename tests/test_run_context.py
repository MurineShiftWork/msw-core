"""RunContext: typed run object built from the resolved args_dict (spine refactor, Phase 1).

Dual-carrier PoC - proves the object extracts the well-known keys (incl. the typed device ports
that replace the ``serial_port_{type}`` magic keys) and projects them back verbatim via
``to_task_kwargs()`` (the frozen task boundary for the fields it owns so far).
"""

from __future__ import annotations

from murineshiftwork.logic.run_context import DevicePorts, RunContext


def test_from_args_dict_extracts_scalars_and_ports():
    d = {
        "command": "run",
        "subject": "seq001",
        "task": "sequence",
        "config_dir": "/cfg",
        "out_path": "/data",
        "debug": True,
        "simulate": False,
        "serial_port_bpod": "/dev/ttyACM0",
        "serial_port_scale": "/dev/ttyUSB1",
        "settings.task.patched": {"start_level": 7},
    }
    ctx = RunContext.from_args_dict(d)
    assert ctx.command == "run"
    assert ctx.subject == "seq001"
    assert ctx.task_name == "sequence"  # falls back to "task"
    assert ctx.out_path == "/data"
    assert ctx.debug is True
    assert ctx.ports.bpod == "/dev/ttyACM0"
    assert ctx.ports.scale == "/dev/ttyUSB1"
    assert ctx.ports.stage == ""  # absent -> default
    assert ctx.task_settings == {"start_level": 7}


def test_from_args_dict_extracts_run_identifiers():
    d = {
        "setup": "rig_a",
        "session_type": "opto",
        "acq_type": "video_flir",
        "session_version": 3,
    }
    ctx = RunContext.from_args_dict(d)
    assert ctx.setup_name == "rig_a"
    assert ctx.session_type == "opto"
    assert ctx.acq_type == "video_flir"
    assert ctx.session_version == 3


def test_run_identifier_defaults_match_evaluate():
    ctx = RunContext.from_args_dict({})
    assert ctx.setup_name == ""
    assert ctx.session_type == ""
    assert ctx.acq_type == "msw"  # evaluate defaults acq_type to "msw"
    assert ctx.session_version is None  # callers default to 1
    # to_task_kwargs projects them back to their args_dict keys
    kw = ctx.to_task_kwargs()
    assert kw["setup"] == "" and kw["session_type"] == ""
    assert kw["acq_type"] == "msw" and kw["session_version"] is None


def test_device_ports_get_mirrors_serial_port_lookup():
    ports = DevicePorts(bpod="/dev/b", pulsepal="/dev/p")
    # known fields resolve; unknown device types degrade to "" (like args_dict.get default)
    assert ports.get("bpod") == "/dev/b"
    assert ports.get("pulsepal") == "/dev/p"
    assert ports.get("stage") == ""  # field exists, unset
    assert ports.get("stage_tower") == ""  # no such field
    assert ports.get("camera") == ""


def test_to_execution_config_projects_the_bundle():
    from murineshiftwork.logic.config import ExecutionConfig, SubjectConfig

    subj = SubjectConfig(name="seq001")
    ctx = RunContext.from_args_dict(
        {
            "task": "sequence",
            "subject_config": subj,
            "settings.task.patched": {"start_level": 7},
        }
    )
    ec = ctx.to_execution_config()
    assert isinstance(ec, ExecutionConfig)
    assert ec.task_name == "sequence"
    assert ec.subject is subj  # same instance (pydantic revalidate_instances='never')
    assert ec.task_settings == {"start_level": 7}
    assert ec.setup is None


def test_from_args_dict_tolerates_empty_dict():
    ctx = RunContext.from_args_dict({})
    assert ctx.command == "" and ctx.task_name == ""
    assert ctx.ports == DevicePorts()
    assert ctx.task_settings == {}


def test_task_name_prefers_task_name_key_over_task():
    ctx = RunContext.from_args_dict({"task_name": "resolved", "task": "raw"})
    assert ctx.task_name == "resolved"


def test_to_task_kwargs_round_trips_owned_fields():
    d = {
        "command": "run",
        "subject": "s",
        "task": "t",
        "config_dir": "/c",
        "out_path": "/o",
        "debug": False,
        "simulate": True,
        "serial_port_bpod": "/dev/b",
        "serial_port_stage": "/dev/s",
        "serial_port_scale": "/dev/sc",
        "serial_port_pulsepal": "/dev/p",
    }
    kw = RunContext.from_args_dict(d).to_task_kwargs()
    # every field RunContext owns projects back to its original args_dict value
    for key in (
        "command",
        "subject",
        "config_dir",
        "out_path",
        "debug",
        "simulate",
        "serial_port_bpod",
        "serial_port_stage",
        "serial_port_scale",
        "serial_port_pulsepal",
    ):
        assert kw[key] == d[key]
    assert kw["task_name"] == "t"  # normalized from "task"
