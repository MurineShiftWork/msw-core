"""A task thread that raises fails the session instead of ending it as a success."""

from __future__ import annotations

import signal
import time
from pathlib import Path

import pytest
import yaml
from murineshiftwork.namespace import msw_file

from murineshiftwork.hardware.bpod.sim import SimBpod
from murineshiftwork.logic.task_process import TaskFailedError, TaskProcess, TaskRunner


@pytest.fixture(autouse=True)
def _no_log_relay(monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_log_config",
        lambda: {"log_url": ""},
    )


@pytest.fixture
def restore_sigint():
    old = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, old)


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


# --- session-manifest status at exit ---


def _status(tp) -> str:
    container = Path(tp.session_paths["session_folder"]).parent
    data = yaml.safe_load((container / "session_manifest.yaml").read_text())
    (entry,) = data["acquisitions"]
    return entry["status"]


class _UntilStopped(TaskRunner):
    def run(self) -> None:
        while self.continue_task:
            time.sleep(0.01)


def test_task_that_ends_itself_is_complete(tmp_path):
    tp = _session(tmp_path, _Fine)
    _run(tp)
    assert _status(tp) == "complete"


def test_operator_stop_is_stopped(tmp_path, restore_sigint):
    tp = _session(tmp_path, _UntilStopped)
    with tp:
        tp.run_task()
        tp.stop_task()  # what a task does on Ctrl-C
        tp.task_runner.join(timeout=5)
    assert _status(tp) == "stopped"


def test_ctrl_c_reaching_the_session_is_stopped(tmp_path):
    tp = _session(tmp_path, _Fine)
    with pytest.raises(KeyboardInterrupt), tp:
        raise KeyboardInterrupt
    assert _status(tp) == "stopped"


def test_task_error_is_aborted(tmp_path):
    tp = _session(tmp_path, _Failing)
    with pytest.raises(TaskFailedError):
        _run(tp)
    assert _status(tp) == "aborted"


def test_session_error_is_aborted(tmp_path):
    tp = _session(tmp_path, _Fine)
    with pytest.raises(RuntimeError), tp:
        raise RuntimeError("hardware fault")
    assert _status(tp) == "aborted"


# --- session.yaml ---


def test_session_yaml_records_the_task_settings(tmp_path):
    sim = SimBpod()
    sim.open()
    tp = TaskProcess(
        out_path=str(tmp_path / "data"),
        subject="_test_subject",
        task="demo_task",
        auto_init=False,
        auto_start=False,
        bpod=sim,
        **{
            "settings.task.patched": {
                "n_trials": 3,
                "valve_s_for_ul": lambda port, ul: 0.1,
                "calibration_file": Path("/cal.csv"),
                "ports": (1, 3),
            }
        },
    )
    with tp:
        pass
    data = yaml.safe_load(
        msw_file(tp.session_paths["session_file_path"], "session.yaml").read_text()
    )
    assert data["task_settings"] == {
        "n_trials": 3,
        "calibration_file": "/cal.csv",
        "ports": [1, 3],
    }
