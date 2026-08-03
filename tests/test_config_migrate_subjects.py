"""`msw config migrate-subjects`: batch-upgrade subject configs + seed the sequence level."""

from __future__ import annotations

import yaml

from murineshiftwork.cli.config import _migrate_one_subject, run_config_migrate_subjects
from murineshiftwork.logic.config import subject_config_schema_version


def _write(dir_, name, data):
    (dir_ / "subjects").mkdir(exist_ok=True)
    p = dir_ / "subjects" / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


# --------------------------------------------------------------------------- #
# on-disk version helper


def test_schema_version_helper(tmp_path):
    assert subject_config_schema_version(tmp_path, "missing") is None  # no file
    _write(tmp_path, "legacy", {"name": "legacy", "task_overrides": {}})  # no stamp
    assert subject_config_schema_version(tmp_path, "legacy") == 0
    _write(tmp_path, "cur", {"name": "cur", "schema_version": 2})
    assert subject_config_schema_version(tmp_path, "cur") == 2


# --------------------------------------------------------------------------- #
# per-file migration + seed


def test_migrate_seeds_sequence_level_from_start_level(tmp_path):
    p = _write(
        tmp_path,
        "seq001",
        {
            "schema_version": 1,
            "name": "seq001",
            "task_overrides": {"sequence": {"start_level": 12}},
        },
    )
    assert _migrate_one_subject(p, dry_run=False, ts="T") == 1
    out = yaml.safe_load(p.read_text())
    assert out["schema_version"] == 2
    assert out["task_state"]["sequence"]["sequences"]["default"] == {
        "level": 12,
        "updated": "T",
    }
    assert out["task_overrides"]["sequence"]["start_level"] == 12  # preserved
    assert (p.parent / "seq001.yaml.bak.T").exists()  # backup written


def test_migrate_no_start_level_adds_no_sequence_state(tmp_path):
    # a non-sequence subject (no start_level): schema bumps, but no empty sequence scaffold
    p = _write(tmp_path, "sub", {"name": "sub", "task_overrides": {}})
    assert _migrate_one_subject(p, dry_run=False, ts="T") == 1
    out = yaml.safe_load(p.read_text())
    assert out["schema_version"] == 2
    assert "sequence" not in out.get("task_state", {})


def test_migrate_dry_run_writes_nothing(tmp_path):
    p = _write(
        tmp_path,
        "seq002",
        {
            "schema_version": 1,
            "name": "seq002",
            "task_overrides": {"sequence": {"start_level": 5}},
        },
    )
    before = p.read_text()
    assert _migrate_one_subject(p, dry_run=True, ts="T") == 1
    assert p.read_text() == before  # unchanged
    assert not list(p.parent.glob("*.bak.*"))


def test_migrate_idempotent_and_skips_newer(tmp_path, capsys):
    # already-seeded v2: no change
    p = _write(
        tmp_path,
        "seq003",
        {
            "schema_version": 2,
            "name": "seq003",
            "task_overrides": {"sequence": {"start_level": 7}},
            "task_state": {
                "sequence": {"sequences": {"default": {"level": 7, "updated": "x"}}}
            },
        },
    )
    assert _migrate_one_subject(p, dry_run=False, ts="T") == 0

    # newer-than-supported schema: skipped, file untouched
    pf = _write(tmp_path, "future", {"schema_version": 99, "name": "future"})
    before = pf.read_text()
    assert _migrate_one_subject(pf, dry_run=False, ts="T") == 0
    assert pf.read_text() == before
    assert "skip" in capsys.readouterr().out


def test_run_command_migrates_all(tmp_path, capsys):
    _write(
        tmp_path,
        "a",
        {
            "schema_version": 1,
            "name": "a",
            "task_overrides": {"sequence": {"start_level": 3}},
        },
    )
    _write(tmp_path, "b", {"name": "b", "task_overrides": {}})  # v0
    run_config_migrate_subjects(config_dir=str(tmp_path), dry_run=False)
    assert "migrated 2/2" in capsys.readouterr().out
    assert (
        yaml.safe_load((tmp_path / "subjects" / "a.yaml").read_text())["task_state"][
            "sequence"
        ]["sequences"]["default"]["level"]
        == 3
    )
