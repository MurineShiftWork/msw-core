"""TaskRunner.emit_trial routes scored trials to the injected framework writer."""

from __future__ import annotations

from murineshiftwork.logic.task_process import TaskRunner


class _FakeWriter:
    def __init__(self) -> None:
        self.trials: list[dict] = []

    def write_trial(self, trial: dict) -> None:
        self.trials.append(trial)


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
