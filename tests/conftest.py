"""Shared fixtures."""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint

import pytest
import yaml

_FAKE_PKG = "msw_fake_tasks"

# name -> task.yaml contents
_FAKE_TASKS: dict[str, dict] = {
    "_test_flush_valves": {
        "session_type": "flush",
        "version": 1,
        "default": {"N_FLUSH_CYCLES": 3},
    },
    "_calibration_sound": {"default": {}},
    "demo_sequence": {"default": {"iti": 1.0}, "mode": {"fast": {"iti": 0.5}}},
}


@pytest.fixture
def fake_tasks(tmp_path, monkeypatch):
    """Register an importable fake task package under the ``msw.tasks`` entry-point group.

    msw-core ships no tasks and its CI installs no task packages, so task discovery and the
    evaluate pipeline are exercised against these. The filesystem scan for bundled tasks is
    stubbed out so a workspace with task packages installed sees the same three tasks.
    Returns ``{name: task_dir}``.
    """
    root = tmp_path / "fake_task_pkgs"
    pkg = root / _FAKE_PKG
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    dirs = {}
    eps = []
    for name, task_yaml in _FAKE_TASKS.items():
        d = pkg / name
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / "task.py").write_text("TASK_NAME = " + repr(name) + "\n", encoding="utf-8")
        (d / "task.yaml").write_text(yaml.safe_dump(task_yaml), encoding="utf-8")
        dirs[name] = d
        eps.append(EntryPoint(name, f"{_FAKE_PKG}.{name}.task", "msw.tasks"))

    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(
        "murineshiftwork.cli.tasks.entry_points",
        lambda group: eps if group == "msw.tasks" else [],
    )
    monkeypatch.setattr(
        "murineshiftwork.cli.tasks._list_tasks_filesystem",
        lambda detailed=False: {} if detailed else [],
    )
    yield dirs
    for mod in [m for m in sys.modules if m.split(".")[0] == _FAKE_PKG]:
        del sys.modules[mod]
