"""Characterization tests for the `msw setup` / `msw subject` config commands.

`run_setup`/`run_subject` create + rename YAML config files; these pin the on-disk shape and the
rename-updates-name behaviour so the shared `_write_yaml`/`_rename_config` helpers stay faithful.
"""

from __future__ import annotations

import yaml

from murineshiftwork.cli.execute import run_setup, run_subject


def test_setup_create_writes_bpod_skeleton(tmp_path):
    run_setup(subcommand="create", setup_name="rigX", config_dir=str(tmp_path))
    data = yaml.safe_load((tmp_path / "setups" / "rigX.yaml").read_text())
    assert data["name"] == "rigX"
    assert "bpod" in data["devices"]  # skeleton seeds a bpod device


def test_setup_rename_moves_file_and_updates_name(tmp_path):
    run_setup(subcommand="create", setup_name="rigX", config_dir=str(tmp_path))
    run_setup(
        subcommand="rename",
        setup_name="rigX",
        new_name="rigY",
        config_dir=str(tmp_path),
    )
    assert not (tmp_path / "setups" / "rigX.yaml").exists()
    data = yaml.safe_load((tmp_path / "setups" / "rigY.yaml").read_text())
    assert data["name"] == "rigY"


def test_setup_rename_refuses_existing_without_force(tmp_path):
    run_setup(subcommand="create", setup_name="a", config_dir=str(tmp_path))
    run_setup(subcommand="create", setup_name="b", config_dir=str(tmp_path))
    run_setup(
        subcommand="rename", setup_name="a", new_name="b", config_dir=str(tmp_path)
    )
    # 'a' is left untouched because 'b' already exists and --force was not given
    assert (tmp_path / "setups" / "a.yaml").exists()
    assert yaml.safe_load((tmp_path / "setups" / "b.yaml").read_text())["name"] == "b"


def test_subject_add_writes_registered_and_name(tmp_path):
    run_subject(subcommand="add", subject="m1", config_dir=str(tmp_path))
    data = yaml.safe_load((tmp_path / "subjects" / "m1.yaml").read_text())
    assert data["name"] == "m1"
    assert data["registered"]  # ISO timestamp stamped at creation
    assert data["task_overrides"] == {}


def test_subject_rename_moves_file_and_updates_name(tmp_path):
    run_subject(subcommand="add", subject="m1", config_dir=str(tmp_path))
    run_subject(
        subcommand="rename", subject="m1", new_name="m2", config_dir=str(tmp_path)
    )
    assert not (tmp_path / "subjects" / "m1.yaml").exists()
    assert (
        yaml.safe_load((tmp_path / "subjects" / "m2.yaml").read_text())["name"] == "m2"
    )
