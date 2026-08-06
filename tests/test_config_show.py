"""`msw config show`: report the effective config/data paths and which layer set them."""

from __future__ import annotations

import pytest

from murineshiftwork.cli.config import run_config_show
from murineshiftwork.logic.machine_config import resolve_config_dir_with_source

# --------------------------------------------------------------------------- #
# the priority-source helper (single source of the resolution chain)


def test_source_helper_cli_wins(monkeypatch):
    monkeypatch.setenv("MSW_CONFIG_DIR", "/env/cfg")
    cfg, src = resolve_config_dir_with_source(cli_override="/cli/cfg")
    assert cfg == "/cli/cfg"
    assert "CLI" in src


def test_source_helper_env_over_default(monkeypatch):
    monkeypatch.setenv("MSW_CONFIG_DIR", "/env/cfg")
    cfg, src = resolve_config_dir_with_source()
    assert cfg == "/env/cfg"
    assert "MSW_CONFIG_DIR" in src


def test_source_helper_unset(monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    # no machine config, no historical default on the test host
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._load_machine_config", lambda: {}
    )
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._HISTORICAL_DEFAULT",
        __import__("pathlib").Path("/nonexistent/msw_configs"),
    )
    cfg, src = resolve_config_dir_with_source()
    assert cfg == ""
    assert src == "unset"


# --------------------------------------------------------------------------- #
# the command output


def test_show_reports_cli_dir_and_source(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    for sub, n in (("setups", 1), ("subjects", 2), ("tasks", 0)):
        (tmp_path / sub).mkdir()
        for i in range(n):
            (tmp_path / sub / f"f{i}.yaml").write_text("{}\n")

    run_config_show(config_dir=str(tmp_path))
    out = capsys.readouterr().out

    assert str(tmp_path) in out
    assert "[--config-dir (CLI)]" in out
    assert "ok" in out  # the dir exists
    assert "subjects" in out and "(2 yaml)" in out
    assert "(1 yaml)" in out  # setups
    assert "data_dir" in out


def test_show_flags_missing_dir(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    missing = tmp_path / "does_not_exist"
    run_config_show(config_dir=str(missing))
    out = capsys.readouterr().out
    assert "MISSING" in out


def test_show_handles_unset(capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._load_machine_config", lambda: {}
    )
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._HISTORICAL_DEFAULT",
        __import__("pathlib").Path("/nonexistent/msw_configs"),
    )
    run_config_show(config_dir="")
    out = capsys.readouterr().out
    assert "<unset>" in out


# --------------------------------------------------------------------------- #
# named-config rendering


def _write_subject(cfg, name, data):
    (cfg / "subjects").mkdir(parents=True, exist_ok=True)
    p = cfg / "subjects" / f"{name}.yaml"
    p.write_text(__import__("yaml").safe_dump(data))
    return p


def test_show_subject_resolved(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    _write_subject(
        tmp_path,
        "seq001",
        {"schema_version": 2, "name": "seq001", "task_overrides": {}},
    )
    run_config_show(config_dir=str(tmp_path), kind="subject", name="seq001")
    out = capsys.readouterr().out
    assert "subject (resolved)" in out
    assert "seq001" in out


def test_show_subject_raw_shows_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    _write_subject(tmp_path, "seq002", {"schema_version": 2, "name": "seq002"})
    run_config_show(config_dir=str(tmp_path), kind="subject", name="seq002", raw=True)
    out = capsys.readouterr().out
    assert "on disk" in out
    assert "seq002" in out


def test_show_subject_missing_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    with pytest.raises(SystemExit):
        run_config_show(config_dir=str(tmp_path), kind="subject", name="nope")


def test_show_kind_without_name_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    with pytest.raises(SystemExit):
        run_config_show(config_dir=str(tmp_path), kind="subject", name="")


def test_show_unknown_kind_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    with pytest.raises(SystemExit):
        run_config_show(config_dir=str(tmp_path), kind="bogus", name="x")


def test_show_includes_machine_config_body(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "murineshiftwork.cli.config.read_machine_config",
        lambda: {"config_dir": str(tmp_path), "ui_url": "http://x"},
    )
    run_config_show(config_dir=str(tmp_path))
    out = capsys.readouterr().out
    assert "machine config (main)" in out
    assert "ui_url" in out
