"""Subject-config schema migration + the machine-written ``task_state`` section (schema v2).

Covers the generic, lossless ``migrate_schema`` helper (incl. the forward-incompatibility
guard), the subject v1->v2 upgrade that adds ``task_state`` without touching existing values,
and the ``save_subject_task_state`` writer.
"""

from __future__ import annotations

import pytest
import yaml

from murineshiftwork.logic.config import (
    SUBJECT_CONFIG_SCHEMA_VERSION,
    load_subject_config,
    migrate_schema,
    save_subject_task_state,
)
from murineshiftwork.logic.config.io import (
    _load_or_seed_subject,
    _migrate_subject_config,
)


def test_load_or_seed_subject_seeds_v2_skeleton_when_absent(tmp_path):
    path, raw = _load_or_seed_subject(tmp_path, "newsub")
    assert path == tmp_path / "subjects" / "newsub.yaml"
    assert raw["schema_version"] == SUBJECT_CONFIG_SCHEMA_VERSION
    assert raw["name"] == "newsub"
    assert raw["task_overrides"] == {}
    assert "task_state" not in raw  # state writer adds it via setdefault


def test_load_or_seed_subject_loads_and_migrates_when_present(tmp_path):
    (tmp_path / "subjects").mkdir()
    (tmp_path / "subjects" / "old.yaml").write_text(
        yaml.safe_dump({"name": "old", "task_overrides": {"seq": {"start_level": 3}}})
    )
    _, raw = _load_or_seed_subject(tmp_path, "old")
    assert raw["schema_version"] == SUBJECT_CONFIG_SCHEMA_VERSION  # migrated
    assert raw["task_state"] == {}  # v1->v2 added the container
    assert raw["task_overrides"]["seq"]["start_level"] == 3  # preserved


# --------------------------------------------------------------------------- #
# generic migrate_schema helper


def test_migrate_schema_chains_steps_in_order():
    calls: list[int] = []

    def step2(raw):
        calls.append(2)
        raw["b"] = 2
        return raw

    def step3(raw):
        calls.append(3)
        raw["c"] = 3
        return raw

    out = migrate_schema(
        {"schema_version": 1, "a": 1}, target=3, steps={2: step2, 3: step3}
    )
    assert calls == [2, 3]  # ran in order, only for versions above current
    assert out == {"schema_version": 3, "a": 1, "b": 2, "c": 3}


def test_migrate_schema_is_lossless_for_untouched_keys():
    # a step touches only 'added'; every pre-existing key must survive verbatim
    raw = {"schema_version": 1, "keep": {"nested": [1, 2, 3]}, "other": "x"}
    out = migrate_schema(raw, target=2, steps={2: lambda r: {**r, "added": True}})
    assert out["keep"] == {"nested": [1, 2, 3]}
    assert out["other"] == "x"
    assert out["added"] is True
    assert out["schema_version"] == 2


def test_migrate_schema_absent_version_runs_all_and_is_idempotent():
    steps = {1: lambda r: {**r, "v1": True}, 2: lambda r: {**r, "v2": True}}
    fresh = migrate_schema({}, target=2, steps=steps)  # no schema_version -> version 0
    assert fresh["v1"] and fresh["v2"] and fresh["schema_version"] == 2
    # re-running at target changes nothing (no steps above target)
    again = migrate_schema(dict(fresh), target=2, steps=steps)
    assert again == fresh


# --------------------------------------------------------------------------- #
# subject v1 -> v2: add task_state, preserve everything else


def _v1_config(name="seq001"):
    return {
        "schema_version": 1,
        "name": name,
        "registered": "2026-01-01",
        "project": "dreamteam",
        "experiment": "",
        "comment": "keep me",
        "aliases": ["cage7"],
        "task_overrides": {"sequence": {"start_level": 20, "task_mode": "gpe_somatic"}},
    }


def test_subject_v1_to_v2_adds_task_state_preserving_values():
    out = _migrate_subject_config(_v1_config())
    assert out["schema_version"] == SUBJECT_CONFIG_SCHEMA_VERSION == 2
    assert out["task_state"] == {}  # new empty container
    # every v1 value preserved, including the operator overrides (untouched)
    assert out["task_overrides"] == {
        "sequence": {"start_level": 20, "task_mode": "gpe_somatic"}
    }
    assert out["comment"] == "keep me" and out["aliases"] == ["cage7"]


def test_subject_migration_idempotent_at_v2():
    once = _migrate_subject_config(_v1_config())
    twice = _migrate_subject_config(dict(once))
    assert twice == once


def test_subject_migration_absent_version():
    raw = {"name": "seq002", "task_overrides": {}}  # legacy, no schema_version
    out = _migrate_subject_config(raw)
    assert out["schema_version"] == 2 and out["task_state"] == {}


def test_load_subject_config_migrates_v1_file(tmp_path):
    subjects = tmp_path / "subjects"
    subjects.mkdir()
    (subjects / "seq003.yaml").write_text(yaml.safe_dump(_v1_config("seq003")))
    cfg = load_subject_config(tmp_path, "seq003")
    assert cfg is not None
    assert cfg.schema_version == 2
    assert cfg.task_state == {}  # migrated container is present on the model
    assert cfg.task_overrides["sequence"]["start_level"] == 20  # preserved


# --------------------------------------------------------------------------- #
# save_subject_task_state writer


def test_save_task_state_creates_file_with_state(tmp_path):
    save_subject_task_state(
        tmp_path, "seq010", "sequence", {"sequences": {"default": {"level": 7}}}
    )
    data = yaml.safe_load((tmp_path / "subjects" / "seq010.yaml").read_text())
    assert data["schema_version"] == 2
    assert data["task_state"]["sequence"]["sequences"]["default"]["level"] == 7


def test_save_task_state_deep_merges_without_clobbering_siblings(tmp_path):
    save_subject_task_state(
        tmp_path, "seq011", "sequence", {"sequences": {"A": {"level": 5}}}
    )
    # writing sequence B must not drop A
    save_subject_task_state(
        tmp_path, "seq011", "sequence", {"sequences": {"B": {"level": 3}}}
    )
    seqs = yaml.safe_load((tmp_path / "subjects" / "seq011.yaml").read_text())[
        "task_state"
    ]["sequence"]["sequences"]
    assert seqs["A"]["level"] == 5 and seqs["B"]["level"] == 3


def test_save_task_state_leaves_task_overrides_untouched_and_migrates(tmp_path):
    subjects = tmp_path / "subjects"
    subjects.mkdir()
    (subjects / "seq012.yaml").write_text(yaml.safe_dump(_v1_config("seq012")))
    save_subject_task_state(
        tmp_path, "seq012", "sequence", {"sequences": {"default": {"level": 9}}}
    )
    data = yaml.safe_load((subjects / "seq012.yaml").read_text())
    assert data["schema_version"] == 2  # v1 file migrated on write
    assert data["task_overrides"]["sequence"]["start_level"] == 20  # override untouched
    assert data["task_state"]["sequence"]["sequences"]["default"]["level"] == 9


def test_save_task_state_round_trips_through_model(tmp_path):
    save_subject_task_state(
        tmp_path, "seq013", "sequence", {"sequences": {"default": {"level": 4}}}
    )
    cfg = load_subject_config(tmp_path, "seq013")
    assert cfg.task_state["sequence"]["sequences"]["default"]["level"] == 4


# --------------------------------------------------------------------------- #
# forward-incompatibility guard (config newer than software)


def test_migrate_schema_refuses_newer_than_target():
    from murineshiftwork.logic.config import SchemaVersionError

    future = {"schema_version": 5, "name": "x", "unknown_future_field": 42}
    with pytest.raises(SchemaVersionError):
        migrate_schema(future, target=2, steps={2: lambda r: r})


def test_load_subject_config_refuses_future_schema(tmp_path):
    from murineshiftwork.logic.config import SchemaVersionError

    subjects = tmp_path / "subjects"
    subjects.mkdir()
    (subjects / "seq099.yaml").write_text(
        yaml.safe_dump({"schema_version": 99, "name": "seq099", "task_overrides": {}})
    )
    with pytest.raises(SchemaVersionError):
        load_subject_config(tmp_path, "seq099")
