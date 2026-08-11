"""Characterization of the task-boundary kwargs `execute.run_task` hands to a task.

`execute.run_task` ends with `mod.run_task(**args_dict)`, where `mod` is a task in the separate
msw-tasks-lab repo - a cross-package public contract. The spine refactor (RunContext, Phases 2-4)
must preserve exactly the keys and scalar values a task reads. This test snapshots that boundary so
any change to how the kwargs are assembled is caught and reviewed deliberately (update the snapshot
here in lockstep, and confirm tasks still receive what they read).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from murineshiftwork.cli import execute
from murineshiftwork.logic.run_context import RunContext


def _capture_boundary(monkeypatch, args: dict) -> dict:
    captured: dict = {}
    fake_mod = SimpleNamespace(run_task=lambda **kw: captured.update(kw))
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake_mod)
    execute.run_task(**args)
    return captured


def _simulate_args() -> dict:
    """A resolved run as evaluate_args would produce for `msw run --simulate -t periodic_trigger`."""
    ctx = RunContext(
        command="run",
        subject="_test_subject",
        task_name="periodic_trigger",
        setup_name="setup-test",
        out_path="/tmp/x",
        simulate=True,
        task_settings={"n_max_trials": 3},
    )
    return {
        "command": "run",
        "task": "periodic_trigger",
        "subject": "_test_subject",
        "setup": "setup-test",
        "config_dir": "",
        "out_path": "/tmp/x",
        "simulate": True,
        "debug": False,
        "session_type": "",
        "serial_port_bpod": "",
        "serial_port_stage": "",
        "serial_port_scale": "",
        "serial_port_pulsepal": "",
        "settings.task.patched": {"n_max_trials": 3},
        "run_context": ctx,
    }


def test_boundary_is_input_keys_plus_device_list_and_context_projection(monkeypatch):
    args = _simulate_args()
    ctx = args["run_context"]
    captured = _capture_boundary(monkeypatch, args)

    # Contract: run_task forwards every input key, adds `device_list`, and overlays the RunContext
    # projection (to_task_kwargs) - the authoritative source for the identity + port keys it owns.
    assert set(captured) == set(args) | {"device_list"} | set(ctx.to_task_kwargs())


def test_boundary_preserves_scalar_values_and_context(monkeypatch):
    args = _simulate_args()
    captured = _capture_boundary(monkeypatch, args)

    assert captured["subject"] == "_test_subject"
    assert captured["task"] == "periodic_trigger"
    assert captured["simulate"] is True
    assert captured["settings.task.patched"] == {"n_max_trials": 3}
    assert captured["run_context"] is args["run_context"]
    # device descriptors are added here (unopened); opened later inside TaskProcess
    assert isinstance(captured["device_list"], list)


def test_boundary_builds_bpod_descriptor_for_a_real_port(monkeypatch):
    """Non-simulate, a bpod port set, no setup -> a single bpod descriptor is handed through."""
    ctx = RunContext(
        command="run",
        subject="_test_subject",
        task_name="periodic_trigger",
        out_path="/tmp/x",
        simulate=False,
    )
    ctx.ports.bpod = "/dev/ttyACM0"
    args = {
        "command": "run",
        "task": "periodic_trigger",
        "subject": "_test_subject",
        "out_path": "/tmp/x",
        "simulate": False,
        "serial_port_bpod": "/dev/ttyACM0",
        "settings.task.patched": {},
        "run_context": ctx,
    }
    captured = _capture_boundary(monkeypatch, args)

    device_list = captured["device_list"]
    assert len(device_list) == 1
    assert device_list[0].name == "bpod"
