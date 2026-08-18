"""TaskRunner.emit_trial: the single fan-out seam (writer + relay), and guaranteed teardown."""

from __future__ import annotations

import queue

import pytest

from murineshiftwork.logic.task_process import TaskProcess, TaskRunner


class _FakeWriter:
    def __init__(self) -> None:
        self.trials: list[dict] = []
        self.closed = False

    def write_trial(self, trial: dict) -> None:
        self.trials.append(trial)

    @property
    def trial_count(self) -> int:
        return len(self.trials)

    def close(self) -> None:
        self.closed = True


def test_emit_trial_routes_to_injected_writer():
    writer = _FakeWriter()
    runner = TaskRunner(trial_writer=writer)
    runner.emit_trial({"trial_index": 0, "outcome": "hit"})
    runner.emit_trial({"trial_index": 1, "outcome": "miss"})
    assert writer.trials == [
        {"trial_index": 0, "outcome": "hit"},
        {"trial_index": 1, "outcome": "miss"},
    ]


def test_emit_trial_is_noop_without_writer():
    runner = (
        TaskRunner()
    )  # no trial_writer injected -> a task that doesn't emit is unaffected
    runner.emit_trial({"trial_index": 0})  # must not raise


def test_emit_trial_dispatches_relay_event_when_given():
    writer = _FakeWriter()
    relay: queue.Queue = queue.Queue()
    runner = TaskRunner(trial_writer=writer, relay_queue=relay)
    event = {
        "trial_index": 0,
        "outcome": "hit",
    }  # flat monitor event, distinct from the stored dict
    runner.emit_trial({"trial_index": 0, "States timestamps": {}}, relay=event)
    assert writer.trials == [
        {"trial_index": 0, "States timestamps": {}}
    ]  # full dict written
    assert relay.get_nowait() == event  # flat event relayed


def test_emit_trial_without_relay_leaves_queue_untouched():
    relay: queue.Queue = queue.Queue()
    runner = TaskRunner(trial_writer=_FakeWriter(), relay_queue=relay)
    runner.emit_trial({"trial_index": 0})  # no relay= -> nothing dispatched
    assert relay.empty()


def test_emit_trial_relay_failure_is_swallowed():
    class _FullQueue:
        def put_nowait(self, _item):
            raise RuntimeError("monitor down")

    runner = TaskRunner(trial_writer=_FakeWriter(), relay_queue=_FullQueue())
    # A dead/full monitor must never interrupt the task.
    runner.emit_trial({"trial_index": 0}, relay={"trial_index": 0})


def test_exit_safely_runs_even_if_finalize_raises(monkeypatch):
    # A finalize failure still surfaces, but teardown (df flush + hardware close) must run anyway.
    tp = object.__new__(TaskProcess)
    tp._hook_ctx = None
    tp.exiting = False
    writer = _FakeWriter()
    writer.write_trial(
        {"trial_index": 0}
    )  # trial_count > 0 -> must be closed on the way out
    tp._trial_writer = writer
    tp._hw_manager = None
    tp.serial_is_open = False
    tp._relay_queue = None
    tp.session_paths = {
        "session_folder": "/nonexistent/s/acq",
        "session_basename": "acq",
    }

    import murineshiftwork.logic.task_process as tp_mod

    def _boom(*_a, **_k):
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(tp_mod, "finalize_acquisition_in_session", _boom)

    with pytest.raises(RuntimeError, match="finalize failed"):
        tp.__exit__(None, None, None)
    assert (
        writer.closed is True
    )  # exit_safely ran in the finally despite the finalize error
    assert tp._trial_writer is None  # ...and cleared the writer
