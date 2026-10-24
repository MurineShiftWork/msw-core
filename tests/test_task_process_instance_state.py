"""TaskRunner / TaskProcess keep their mutable state per instance, never on the class."""

from __future__ import annotations

from murineshiftwork.logic.task_process import TaskProcess, TaskRunner


def _class_level_containers(cls) -> dict:
    return {
        name: value
        for klass in cls.__mro__
        if klass is not object
        for name, value in vars(klass).items()
        if isinstance(value, dict | list | set) and not name.startswith("__")
    }


def test_task_runner_has_no_class_level_mutable_defaults():
    assert _class_level_containers(TaskRunner) == {}


def test_task_process_has_no_class_level_mutable_defaults():
    assert _class_level_containers(TaskProcess) == {}


def test_task_runner_input_kwargs_are_per_instance():
    a = TaskRunner(foo=1)
    b = TaskRunner(bar=2)
    a.input_kwargs["added"] = True
    assert b.input_kwargs == {"bar": 2}
    assert a.input_kwargs == {"foo": 1, "added": True}
