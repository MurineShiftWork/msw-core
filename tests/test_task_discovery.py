"""Task discovery through the ``msw.tasks`` entry points: listing, name matching, task dirs."""

from __future__ import annotations

from pathlib import Path

import pytest

from murineshiftwork.cli.evaluate import get_task_dir
from murineshiftwork.cli.tasks import (
    find_task_by_name,
    list_available_tasks,
    load_task_module,
)


def test_list_available_tasks_includes_entry_point_tasks(fake_tasks):
    assert list_available_tasks() == sorted(fake_tasks)


def test_list_available_tasks_detailed_maps_to_task_dirs(fake_tasks):
    detailed = list_available_tasks(detailed=True)
    assert {n: Path(d).resolve() for n, d in detailed.items()} == {
        n: d.resolve() for n, d in fake_tasks.items()
    }


def test_find_task_by_name_exact_and_substring(fake_tasks):
    assert find_task_by_name("_calibration_sound") == "_calibration_sound"
    assert find_task_by_name("flush") == "_test_flush_valves"  # unique substring
    assert find_task_by_name("nonexistent_xyz_task") is None


def test_find_task_by_name_ambiguous(fake_tasks):
    # "o" matches _calibration_sound and demo_sequence: first sorted, or raise on request
    assert find_task_by_name("o") == "_calibration_sound"
    with pytest.raises(ValueError, match="matches multiple tasks"):
        find_task_by_name("o", ignore_error=False)


def test_load_task_module_imports_the_entry_point(fake_tasks):
    mod = load_task_module("demo_sequence")
    assert mod.TASK_NAME == "demo_sequence"


def test_get_task_dir_returns_the_task_package_dir(fake_tasks):
    d = get_task_dir("_test_flush_valves")
    assert Path(d).resolve() == fake_tasks["_test_flush_valves"].resolve()
    assert (Path(d) / "task.yaml").is_file()


def test_get_task_dir_unknown_task_is_empty(fake_tasks):
    assert get_task_dir("nonexistent_xyz_task") == ""
