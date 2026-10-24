"""resolve_data_dir: CLI > MSW_DATA_DIR > machine config > historical default > ~/data."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from murineshiftwork.logic import machine_config as mc


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No env var, no machine config file, no historical default on the test host."""
    monkeypatch.delenv("MSW_DATA_DIR", raising=False)
    monkeypatch.setattr(mc, "_MACHINE_CONFIG_FILE", tmp_path / "msw_machine.yaml")
    monkeypatch.setattr(mc, "_HISTORICAL_DATA_DEFAULT", tmp_path / "no_historical")
    return tmp_path


def test_cli_override_wins(isolated, monkeypatch):
    monkeypatch.setenv("MSW_DATA_DIR", str(isolated / "env"))
    assert mc.resolve_data_dir(cli_override=str(isolated / "cli")) == str(
        isolated / "cli"
    )


def test_unknown_cli_placeholder_is_ignored(isolated, monkeypatch):
    monkeypatch.setenv("MSW_DATA_DIR", str(isolated / "env"))
    assert mc.resolve_data_dir(cli_override="unknown_out_path") == str(isolated / "env")


def test_env_var_beats_machine_config(isolated, monkeypatch):
    mc._MACHINE_CONFIG_FILE.write_text(yaml.dump({"data_dir": str(isolated / "mc")}))
    monkeypatch.setenv("MSW_DATA_DIR", str(isolated / "env"))
    assert mc.resolve_data_dir() == str(isolated / "env")


def test_machine_config_beats_historical_default(isolated, monkeypatch):
    historical = isolated / "historical"
    historical.mkdir()
    monkeypatch.setattr(mc, "_HISTORICAL_DATA_DEFAULT", historical)
    mc._MACHINE_CONFIG_FILE.write_text(yaml.dump({"data_dir": str(isolated / "mc")}))
    assert mc.resolve_data_dir() == str(isolated / "mc")


def test_historical_default_used_when_it_exists(isolated, monkeypatch):
    historical = isolated / "historical"
    historical.mkdir()
    monkeypatch.setattr(mc, "_HISTORICAL_DATA_DEFAULT", historical)
    assert mc.resolve_data_dir() == str(historical)


def test_falls_back_to_home_data(isolated, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: isolated / "home")
    assert mc.resolve_data_dir() == str(isolated / "home" / "data")
