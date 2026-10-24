"""The session YAML ``process`` block records which code produced the session."""

from __future__ import annotations

import yaml

import murineshiftwork.logic.task_process as tp_mod
from murineshiftwork.logic.task_process import TaskProcess


def _bare_task_process(tmp_path) -> TaskProcess:
    tp = object.__new__(TaskProcess)
    tp.session_uuid = "uuid"
    tp.task_name = "my_task"
    tp.subject = "m1"
    tp.input_kwargs = {"setup": "rig1"}
    tp.serial_port = ""
    tp.out_path = str(tmp_path)
    tp.task_version = 3
    tp.session_paths = {"session_file_path": str(tmp_path / "sess")}
    return tp


def test_process_block_records_package_versions_and_task_package(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tp_mod, "_get_package_versions", lambda: {"msw-core": "1.0", "msw-io": "2.0"}
    )
    monkeypatch.setattr(
        tp_mod,
        "_get_task_package",
        lambda task: (
            {"name": "msw-tasks-lab", "version": "0.1"} if task == "my_task" else {}
        ),
    )
    _bare_task_process(tmp_path).persist_settings()

    data = yaml.safe_load((tmp_path / "sess.msw.session.yaml").read_text())
    process = data["process"]
    assert process["package_versions"] == {"msw-core": "1.0", "msw-io": "2.0"}
    assert process["task_package"] == {"name": "msw-tasks-lab", "version": "0.1"}
    # back-compat keys are still written
    for key in ("msw_version", "git_commit", "task_version"):
        assert key in process
    assert process["task_version"] == 3


def test_process_block_with_real_probes_is_yaml_safe(tmp_path):
    _bare_task_process(tmp_path).persist_settings()
    process = yaml.safe_load((tmp_path / "sess.msw.session.yaml").read_text())[
        "process"
    ]
    assert "msw-core" in process["package_versions"]
    assert process["task_package"] == {}  # "my_task" is not an installed task
