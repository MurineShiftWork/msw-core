"""Whole-file rewrites (session YAML, subject/setup YAML) land atomically.

A crash or exception mid-write must leave the previous file intact, never a truncated YAML,
and must not leave temporary files behind in the session or config directory.
"""

from __future__ import annotations

import stat
import sys

import pytest
import yaml

import murineshiftwork.logic.config.io as config_io
import murineshiftwork.logic.task_process as tp_mod
from murineshiftwork.logic.paths import atomic_open_w


def _leftovers(directory) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_atomic_open_w_writes_and_cleans_up(tmp_path):
    target = tmp_path / "f.yaml"
    with atomic_open_w(target) as f:
        f.write("a: 1\n")
    assert target.read_text() == "a: 1\n"
    assert _leftovers(tmp_path) == []


def test_atomic_open_w_failure_keeps_original(tmp_path):
    target = tmp_path / "f.yaml"
    target.write_text("original: true\n")
    with pytest.raises(RuntimeError), atomic_open_w(target) as f:
        f.write("partial")
        raise RuntimeError("crash mid-write")
    assert target.read_text() == "original: true\n"
    assert _leftovers(tmp_path) == []


def test_atomic_open_w_failure_on_new_file_creates_nothing(tmp_path):
    target = tmp_path / "new.yaml"
    with pytest.raises(RuntimeError), atomic_open_w(target) as f:
        f.write("partial")
        raise RuntimeError("crash mid-write")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_atomic_open_w_preserves_existing_mode(tmp_path):
    target = tmp_path / "f.yaml"
    target.write_text("x: 1\n")
    target.chmod(0o640)
    with atomic_open_w(target) as f:
        f.write("x: 2\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def _dump_then_crash(data, stream, **_kwargs):
    stream.write("truncated: [")
    raise OSError("disk full")


def test_update_session_yaml_failure_keeps_previous_file(tmp_path, monkeypatch):
    session_file_path = tmp_path / "sess"
    tp_mod.update_session_yaml(session_file_path, task_settings={"a": 1})
    yaml_path = tmp_path / "sess.msw.session.yaml"
    before = yaml_path.read_text()

    monkeypatch.setattr(tp_mod.yaml, "safe_dump", _dump_then_crash)
    with pytest.raises(OSError, match="disk full"):
        tp_mod.update_session_yaml(session_file_path, stage={"b": 2})

    assert yaml_path.read_text() == before
    assert yaml.safe_load(before)["task_settings"] == {"a": 1}
    assert _leftovers(tmp_path) == []


def test_update_session_yaml_merges_sections(tmp_path):
    session_file_path = tmp_path / "sess"
    tp_mod.update_session_yaml(session_file_path, task_settings={"a": 1})
    tp_mod.update_session_yaml(session_file_path, stage={"b": 2})
    data = yaml.safe_load((tmp_path / "sess.msw.session.yaml").read_text())
    assert data == {
        "msw_format_version": 2,
        "task_settings": {"a": 1},
        "stage": {"b": 2},
    }


def test_subject_writeback_failure_keeps_previous_file(tmp_path, monkeypatch):
    config_io.save_subject_task_overrides(tmp_path, "m1", "seq", {"task_mode": "a"})
    subject_yaml = tmp_path / "subjects" / "m1.yaml"
    before = subject_yaml.read_text()

    monkeypatch.setattr(config_io.yaml, "dump", _dump_then_crash)
    with pytest.raises(OSError, match="disk full"):
        config_io.save_subject_task_state(tmp_path, "m1", "seq", {"level": 3})

    assert subject_yaml.read_text() == before
    assert _leftovers(subject_yaml.parent) == []


def test_atomic_open_w_blocked_replace_falls_back_to_in_place(tmp_path, monkeypatch):
    # Windows: a reader holding the file open makes os.replace raise PermissionError.
    import murineshiftwork.logic.paths as paths_mod

    target = tmp_path / "f.yaml"
    target.write_text("x: 1\n")

    def _blocked(self, _target):
        raise PermissionError("in use")

    monkeypatch.setattr(paths_mod.Path, "replace", _blocked)
    monkeypatch.setattr(paths_mod, "_REPLACE_RETRY_DELAY_S", 0)
    with atomic_open_w(target) as f:
        f.write("x: 2\n")
    assert target.read_text() == "x: 2\n"
    assert _leftovers(tmp_path) == []
