"""A task thread that raises fails the session instead of ending it as a success."""

from __future__ import annotations

import pytest

from murineshiftwork.hardware.bpod.sim import SimBpod
from murineshiftwork.logic.task_process import TaskFailedError, TaskProcess, TaskRunner


@pytest.fixture(autouse=True)
def _no_log_relay(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_log_config",
        lambda: {"log_url": ""},
    )


class _Failing(TaskRunner):
    def run(self) -> None:
        raise ImportError("pypulsepal is required for stimulation")


class _Fine(TaskRunner):
    def run(self) -> None:
        pass


class _Interrupted(TaskRunner):
    def run(self) -> None:
        raise KeyboardInterrupt


def _session(tmp_path, runner_cls):
    sim = SimBpod()
    sim.open()
    tp = TaskProcess(
        serial_port_bpod=None,
        out_path=str(tmp_path / "data"),
        subject="_test_subject",
        task="demo_task",
        auto_init=False,
        auto_start=False,
        require_bpod=False,
        bpod=sim,
    )
    tp.task_runner = runner_cls(bpod=sim)
    return tp


def _run(tp):
    with tp:
        tp.run_task()
        tp.task_runner.join(timeout=5)


def test_task_exception_fails_the_session(tmp_path):
    tp = _session(tmp_path, _Failing)
    with pytest.raises(TaskFailedError, match="pypulsepal"):
        _run(tp)
    assert isinstance(tp.task_runner.exception, ImportError)


def test_clean_task_ends_without_error(tmp_path):
    tp = _session(tmp_path, _Fine)
    _run(tp)
    assert tp.task_runner.exception is None


def test_ctrl_c_in_the_task_is_a_stop_not_a_failure(tmp_path):
    tp = _session(tmp_path, _Interrupted)
    _run(tp)
    assert tp.task_runner.exception is None
