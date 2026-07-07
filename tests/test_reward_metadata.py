"""Tests for build_reward_metadata (the metadata.reward block)."""

from __future__ import annotations

from murineshiftwork.logic.reward_metadata import build_reward_metadata


def _s_for_ul(valve, ul):  # fake calibration: 10 ms/ul, valve-independent
    return ul * 0.010


def test_none_when_no_reward_info():
    assert build_reward_metadata({}) is None
    assert build_reward_metadata(None) is None


def test_type_and_amount_only():
    r = build_reward_metadata({"reward_type": "milkshake", "reward_amount_ul": 3.0})
    assert r == {"type": "milkshake", "reward_amount_ul": 3.0}


def test_single_valve_is_flat():
    r = build_reward_metadata(
        {
            "reward_type": "water",
            "reward_amount_ul": 3.0,
            "valve_s_for_ul": _s_for_ul,
            "HARDWARE_VALVES_FOR_WATER": [1],
        }
    )
    assert r["type"] == "water"
    assert r["valve"] == 1
    assert r["valve_open_ms"] == 30.0  # 3 ul * 10 ms/ul
    assert r["calibrated_volume_ul"] == 3.0
    assert "by_valve" not in r


def test_multi_valve_uses_by_valve_and_provenance():
    class Cal:
        def __init__(self, updated, fit):
            self.updated = updated
            self.fit_model = fit

    r = build_reward_metadata(
        {
            "reward_amount_ul": 2.0,
            "valve_s_for_ul": _s_for_ul,
            "HARDWARE_VALVES_FOR_WATER": [1, 2],
        },
        valve_calibrations={1: Cal("2026-06-18", "exponential"), 2: {"updated": "2026-06-19"}},
    )
    assert set(r["by_valve"]) == {1, 2}
    assert r["by_valve"][1]["valve_open_ms"] == 20.0
    assert r["by_valve"][1]["calibration"] == {"date": "2026-06-18", "fit_model": "exponential"}
    assert r["by_valve"][2]["calibration"] == {"date": "2026-06-19"}
    assert "valve" not in r  # not flattened when multi-spout


def test_bad_amount_is_ignored():
    r = build_reward_metadata({"reward_type": "water", "reward_amount_ul": "n/a"})
    assert r == {"type": "water"}
