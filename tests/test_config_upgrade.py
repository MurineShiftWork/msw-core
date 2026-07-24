"""Tests for `msw config upgrade` (task-overlay key backfill)."""

from __future__ import annotations

import yaml

from murineshiftwork.cli.config import _missing_key_paths, _upgrade_task_overlay


def test_missing_key_paths_recursive():
    bundled = {"a": 1, "b": {"x": 1, "y": 2}, "c": 3}
    overlay = {"a": 9, "b": {"x": 5}}  # user changed a+b.x; missing b.y and c
    assert sorted(_missing_key_paths(bundled, overlay)) == ["b.y", "c"]


def test_missing_key_paths_none_when_complete():
    d = {"a": 1, "b": {"x": 1}}
    assert _missing_key_paths(d, {"a": 2, "b": {"x": 9}}) == []


def _setup(tmp_path, bundled, overlay, monkeypatch):
    """Wire a fake bundled task.yaml + overlay for task 'demo'; return overlay path."""
    from murineshiftwork.cli import config as cfg

    bundled_path = tmp_path / "bundled_task.yaml"
    bundled_path.write_text(yaml.safe_dump(bundled))
    overlay_dir = tmp_path / "cfg" / "tasks" / "demo"
    overlay_dir.mkdir(parents=True)
    overlay_path = overlay_dir / "task.yaml"
    overlay_path.write_text(yaml.safe_dump(overlay))

    monkeypatch.setattr(cfg, "find_task_by_name", lambda task_name: "demo")
    monkeypatch.setattr(cfg, "_task_yaml_path", lambda name: bundled_path)
    return overlay_path


def test_upgrade_adds_missing_keys_preserving_user_values(tmp_path, monkeypatch):
    bundled = {
        "session_type": "demo",
        "reward_ul": 4.0,
        "new_field": True,
        "block": {"a": 1, "b": 2},
    }
    overlay = {
        "session_type": "demo",
        "reward_ul": 9.9,
        "block": {"a": 7},
    }  # user edits + drift
    overlay_path = _setup(tmp_path, bundled, overlay, monkeypatch)

    changed = _upgrade_task_overlay(
        "demo", str(tmp_path / "cfg"), dry_run=False, yes=True
    )
    assert changed == 1
    result = yaml.safe_load(overlay_path.read_text())
    # new keys added
    assert result["new_field"] is True and result["block"]["b"] == 2
    # user values preserved (never overwritten by bundled defaults)
    assert result["reward_ul"] == 9.9 and result["block"]["a"] == 7
    # a timestamped backup was written
    assert list(overlay_path.parent.glob("task.yaml.bak.*"))


def test_upgrade_noop_when_up_to_date(tmp_path, monkeypatch):
    d = {"session_type": "demo", "reward_ul": 4.0}
    overlay_path = _setup(tmp_path, d, dict(d), monkeypatch)
    assert (
        _upgrade_task_overlay("demo", str(tmp_path / "cfg"), dry_run=False, yes=True)
        == 0
    )
    assert not list(overlay_path.parent.glob("task.yaml.bak.*"))  # nothing written


def test_dry_run_reports_but_does_not_write(tmp_path, monkeypatch):
    overlay_path = _setup(tmp_path, {"a": 1, "b": 2}, {"a": 1}, monkeypatch)
    before = overlay_path.read_text()
    assert (
        _upgrade_task_overlay("demo", str(tmp_path / "cfg"), dry_run=True, yes=True)
        == 1
    )
    assert overlay_path.read_text() == before  # unchanged
    assert not list(overlay_path.parent.glob("task.yaml.bak.*"))
