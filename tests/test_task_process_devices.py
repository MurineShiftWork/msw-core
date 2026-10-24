"""TaskProcess device injection: an injected `bpod` or pre-opened `devices` collection is used as-is.

Constructed with auto_init/auto_start off, so no task module is loaded; only the device-acquisition
step of __init__ runs. The caller keeps ownership of injected handles.
"""

from __future__ import annotations

import pytest

from murineshiftwork.hardware.bpod.sim import SimBpod
from murineshiftwork.logic.task_process import TaskProcess


@pytest.fixture(autouse=True)
def _no_log_relay(monkeypatch):
    # a machine config with log_url set would otherwise spawn the log relay process
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_log_config",
        lambda: {"log_url": ""},
    )


def _open_sim():
    sim = SimBpod()
    sim.open()
    return sim


def _task_process(tmp_path, **kwargs):
    return TaskProcess(
        serial_port_bpod=None,
        out_path=str(tmp_path / "data"),
        subject="_test_subject",
        task="demo_task",
        auto_init=False,
        auto_start=False,
        require_bpod=False,
        **kwargs,
    )


def test_devices_collection_supplies_bpod(tmp_path):
    sim = _open_sim()
    tp = _task_process(tmp_path, devices={"bpod": sim})
    assert tp.bpod is sim
    assert tp.serial_is_open is True
    assert tp.input_kwargs["devices"] == {"bpod": sim}


def test_explicit_bpod_takes_priority_over_devices(tmp_path):
    explicit, in_devices = _open_sim(), _open_sim()
    tp = _task_process(tmp_path, bpod=explicit, devices={"bpod": in_devices})
    assert tp.bpod is explicit


def test_devices_without_bpod_leaves_bpod_unset(tmp_path):
    tp = _task_process(tmp_path, devices={"scale": object()})
    assert tp.bpod is None
    assert not tp.serial_is_open
