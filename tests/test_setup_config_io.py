"""Setup-config load / writeback round-trip on a realistic anonymized fixture.

Proves that an existing setup YAML (structure copied from a production rig, all
identifying values replaced with synthetic ones) loads into ``SetupConfig`` with
NO migration, that the embedded ``bpod_valve`` calibration data drives the
``valve_s_for_ul`` / ``valve_ul_for_s`` lookups and ``check_quality``, and that
``update_valve_calibration`` writes a new entry that re-reads correctly.

The fixture lives at ``tests/data/setup_fixture.yaml``.
"""

import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

from murineshiftwork.logic.config.io import (
    load_setup_config,
    update_valve_calibration,
)
from murineshiftwork.logic.config.models import SetupConfig, ValveCalibration

_FIXTURE = Path(__file__).parent / "data" / "setup_fixture.yaml"
_SETUP_NAME = "rig_fixture"


@pytest.fixture
def config_dir(tmp_path):
    """A throwaway config_dir with the fixture installed at setups/rig_fixture.yaml."""
    setups = tmp_path / "setups"
    setups.mkdir()
    shutil.copy(_FIXTURE, setups / f"{_SETUP_NAME}.yaml")
    return tmp_path


# --------------------------------------------------------------------------- #
# (a) load_setup_config parses the realistic YAML into a SetupConfig


def test_load_setup_config_parses_fixture(config_dir):
    cfg = load_setup_config(config_dir, _SETUP_NAME)

    assert isinstance(cfg, SetupConfig)
    assert cfg.name == _SETUP_NAME
    # Devices survive the discriminated-union parse.
    assert cfg.devices["bpod"].type == "bpod"
    assert cfg.devices["stage"].type == "stage_tower"
    assert cfg.devices["scale"].type == "scale"
    # Calibration block loads as-is, no fit_model migration needed.
    assert set(cfg.calibrations.bpod_valve) == {"1", "2", "3"}
    assert cfg.calibrations.fit_model == "exponential"
    assert cfg.calibrations.bpod_valve["1"].fit_model == "exponential"
    assert cfg.calibrations.stale_days == 180


def test_load_setup_config_missing_returns_none(config_dir):
    assert load_setup_config(config_dir, "does_not_exist") is None


# --------------------------------------------------------------------------- #
# (b) calibration data round-trips through the lookup helpers


def test_valve_lookups_round_trip(config_dir):
    cfg = load_setup_config(config_dir, _SETUP_NAME)

    # Pick an open time inside the calibrated range, convert to volume, then back.
    open_s = 0.10
    vol = cfg.valve_ul_for_s("1", open_s)
    assert vol > 0
    recovered_s = cfg.valve_s_for_ul("1", vol)
    assert recovered_s == pytest.approx(open_s, abs=1e-2)


def test_valve_lookup_accepts_int_port(config_dir):
    cfg = load_setup_config(config_dir, _SETUP_NAME)
    # str/int port keys must resolve to the same calibration.
    assert cfg.valve_ul_for_s(1, 0.10) == pytest.approx(cfg.valve_ul_for_s("1", 0.10))


def test_check_quality_passes_on_fixture_data(config_dir):
    cfg = load_setup_config(config_dir, _SETUP_NAME)
    for valve_id, vc in cfg.calibrations.bpod_valve.items():
        is_valid, reason = vc.check_quality()
        assert is_valid, f"valve {valve_id}: {reason}"
        assert "exponential" in reason


# --------------------------------------------------------------------------- #
# (c) writeback via update_valve_calibration writes and re-reads correctly


def test_update_valve_calibration_round_trip(config_dir):
    # A clean exponential monotonic dataset that passes check_quality:
    # ul = 1.0 * exp(15 * open_s) + 0.5, rounded to 3 d.p. like real data.
    times = [0.01, 0.05, 0.08, 0.11, 0.15]
    points = [[t, round(1.0 * np.exp(15.0 * t) + 0.5, 3)] for t in times]
    expected_at_008 = 1.0 * np.exp(15.0 * 0.08) + 0.5
    new_cal = ValveCalibration(
        updated="2026-06-20T09:00:00",
        points=points,
    )
    is_valid, reason = new_cal.check_quality()
    assert is_valid, reason

    wrote = update_valve_calibration(
        config_dir, _SETUP_NAME, valve_id=4, new_calibration=new_cal
    )
    assert wrote is True

    # Re-read the file through the loader: the new valve appears alongside the originals.
    reloaded = load_setup_config(config_dir, _SETUP_NAME)
    assert set(reloaded.calibrations.bpod_valve) == {"1", "2", "3", "4"}
    rt = reloaded.calibrations.bpod_valve["4"]
    assert rt.updated == "2026-06-20T09:00:00"
    assert rt.points == points
    # And the freshly-written calibration is usable.
    assert reloaded.valve_ul_for_s("4", 0.08) == pytest.approx(expected_at_008, abs=0.5)


def test_update_valve_calibration_rejects_bad_fit(config_dir):
    # Non-monotonic / too-few-points data fails check_quality and is not written
    # unless force=True.
    bad_cal = ValveCalibration(points=[[0.01, 5.0], [0.05, 1.0]])
    wrote = update_valve_calibration(
        config_dir, _SETUP_NAME, valve_id=9, new_calibration=bad_cal
    )
    assert wrote is False

    # File untouched: no valve 9 added.
    raw = yaml.safe_load((config_dir / "setups" / f"{_SETUP_NAME}.yaml").read_text())
    assert "9" not in raw["calibrations"]["bpod_valve"]


def test_update_valve_calibration_force_writes_bad_fit(config_dir):
    bad_cal = ValveCalibration(
        updated="2026-06-20T10:00:00", points=[[0.01, 5.0], [0.05, 1.0]]
    )
    wrote = update_valve_calibration(
        config_dir, _SETUP_NAME, valve_id=9, new_calibration=bad_cal, force=True
    )
    assert wrote is True
    reloaded = load_setup_config(config_dir, _SETUP_NAME)
    assert "9" in reloaded.calibrations.bpod_valve
