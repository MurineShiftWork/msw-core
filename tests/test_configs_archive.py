"""`msw configs archive`: git mv config files into archive/; loaders ignore archive/."""

from __future__ import annotations

import subprocess

import pytest

from murineshiftwork.cli.configs import run_configs_archive
from murineshiftwork.logic.config import load_subject_config


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "cfg"
    for sub in ("subjects", "setups", "tasks/seq", "device_configs"):
        (root / sub).mkdir(parents=True)
    files = [
        "subjects/s001.yaml",
        "setups/rig1.yaml",
        "tasks/seq/task.yaml",
        "device_configs/dev.yaml",
    ]
    for f in files:
        (root / f).write_text("name: x\n")
    (root / "subjects/untracked.yaml").write_text("name: u\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", "--", *files)
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "init",
    )
    return root


@pytest.mark.parametrize(
    "rel",
    [
        "subjects/s001.yaml",
        "setups/rig1.yaml",
        "tasks/seq/task.yaml",
        "device_configs/dev.yaml",
    ],
)
def test_archive_moves_with_git_mv(repo, rel, capsys):
    run_configs_archive(paths=[str(repo / rel)], config_dir=str(repo))
    assert not (repo / rel).exists()
    assert (repo / "archive" / rel).exists()
    assert f"R  {rel} -> archive/{rel}" in _git(repo, "status", "--short")
    assert "not committed" in capsys.readouterr().out


def test_archive_relative_to_repo_and_cwd(repo, monkeypatch):
    run_configs_archive(paths=["subjects/s001.yaml"], config_dir=str(repo))
    assert (repo / "archive/subjects/s001.yaml").exists()
    monkeypatch.chdir(repo / "setups")
    run_configs_archive(paths=["rig1.yaml"], config_dir=str(repo))
    assert (repo / "archive/setups/rig1.yaml").exists()


def test_dry_run_moves_nothing(repo, capsys):
    run_configs_archive(
        paths=["subjects/s001.yaml"], config_dir=str(repo), dry_run=True
    )
    assert (repo / "subjects/s001.yaml").exists()
    assert not (repo / "archive").exists()
    assert "would move" in capsys.readouterr().out


def test_refuses_and_moves_nothing(repo, tmp_path):
    (repo / "archive/subjects").mkdir(parents=True)
    (repo / "archive/subjects/s001.yaml").write_text("name: old\n")
    outside = tmp_path / "other.yaml"
    outside.write_text("x: 1\n")
    for bad in (
        str(outside),  # outside the repo
        "archive/subjects/s001.yaml",  # already archived
        "subjects/untracked.yaml",  # untracked
        "subjects/s001.yaml",  # target exists
        "subjects/missing.yaml",  # no such file
    ):
        with pytest.raises(SystemExit):
            run_configs_archive(paths=["setups/rig1.yaml", bad], config_dir=str(repo))
        assert (repo / "setups/rig1.yaml").exists()


def test_refuses_non_git_dir(tmp_path):
    (tmp_path / "subjects").mkdir()
    (tmp_path / "subjects/a.yaml").write_text("x: 1\n")
    with pytest.raises(SystemExit):
        run_configs_archive(paths=["subjects/a.yaml"], config_dir=str(tmp_path))


def test_loader_ignores_archived_subject(repo):
    (repo / "subjects/s001.yaml").write_text("subject_name: s001\n")
    run_configs_archive(paths=["subjects/s001.yaml"], config_dir=str(repo))
    assert load_subject_config(str(repo), "s001") is None


def test_listings_ignore_archive(repo, capsys):
    from murineshiftwork.cli.config import run_config_show

    (repo / "archive/subjects").mkdir(parents=True)
    (repo / "archive/subjects/old.yaml").write_text("x: 1\n")
    (repo / "archive/tasks").mkdir(parents=True)
    (repo / "archive/tasks/old.yaml").write_text("x: 1\n")
    run_config_show(config_dir=str(repo))
    out = capsys.readouterr().out
    assert "subjects" in out and "(1 yaml)" in out
    assert "/archive" not in out
