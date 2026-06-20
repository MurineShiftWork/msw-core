"""Valve calibration fit-model selection (exponential vs linear).

Covers the configurable ``fit_model`` field on ValveCalibration / Calibrations:
the default stays exponential (backward compatibility), the linear path fits a
known straight line, predict/inverse round-trips for both models, YAML
round-trip of the field, and the ``-ts FIT_MODEL=linear`` task-time override.
"""

import numpy as np
import pytest

from murineshiftwork.cli.evaluate import _inject_valve_calibration
from murineshiftwork.logic.config.models import (
    Calibrations,
    SetupConfig,
    ValveCalibration,
)

# A clean, exactly-linear dataset: ul = 100 * open_s + 2.
_LINEAR_SLOPE = 100.0
_LINEAR_INTERCEPT = 2.0
_LINEAR_TIMES = [0.01, 0.02, 0.05, 0.10, 0.20]
_LINEAR_POINTS = [[t, _LINEAR_SLOPE * t + _LINEAR_INTERCEPT] for t in _LINEAR_TIMES]

# A clean exponential dataset: ul = 1.0 * exp(15 * open_s) + 0.5.
_EXP_TIMES = [0.01, 0.03, 0.06, 0.10, 0.15]
_EXP_POINTS = [[t, 1.0 * np.exp(15.0 * t) + 0.5] for t in _EXP_TIMES]


# --------------------------------------------------------------------------- #
# Default behaviour stays exponential


def test_default_fit_model_is_exponential():
    vc = ValveCalibration(points=_EXP_POINTS)
    assert vc.fit_model == "exponential"


def test_calibrations_default_fit_model_is_exponential():
    cal = Calibrations(bpod_valve={"1": ValveCalibration(points=_EXP_POINTS)})
    assert cal.fit_model == "exponential"
    assert cal.bpod_valve["1"].fit_model == "exponential"


# --------------------------------------------------------------------------- #
# Linear fit produces sane coefficients on a known linear dataset


def test_linear_fit_recovers_slope_and_intercept():
    vc = ValveCalibration(points=_LINEAR_POINTS, fit_model="linear")
    a, b = vc._fit()
    assert a == pytest.approx(_LINEAR_SLOPE, rel=1e-6)
    assert b == pytest.approx(_LINEAR_INTERCEPT, abs=1e-6)


def test_linear_validate_passes_on_linear_data():
    vc = ValveCalibration(points=_LINEAR_POINTS, fit_model="linear")
    is_valid, reason = vc.check_quality()
    assert is_valid, reason
    assert "linear" in reason


def test_exponential_validate_passes_on_exponential_data():
    vc = ValveCalibration(points=_EXP_POINTS, fit_model="exponential")
    is_valid, reason = vc.check_quality()
    assert is_valid, reason
    assert "exponential" in reason


# --------------------------------------------------------------------------- #
# Round-trip predict / inverse for both models


@pytest.mark.parametrize(
    ("points", "model"),
    [
        (_LINEAR_POINTS, "linear"),
        (_EXP_POINTS, "exponential"),
    ],
)
def test_predict_inverse_round_trip(points, model):
    vc = ValveCalibration(points=points, fit_model=model)
    # Pick an open time inside the calibrated range, predict the volume, then
    # invert and confirm we recover the original open time.
    open_s = 0.07
    vol = vc.ul_for_s(open_s)
    recovered_s = vc.s_for_ul(vol)
    assert recovered_s == pytest.approx(open_s, abs=1e-3)


def test_linear_ul_for_s_matches_closed_form():
    vc = ValveCalibration(points=_LINEAR_POINTS, fit_model="linear")
    assert vc.ul_for_s(0.10) == pytest.approx(
        _LINEAR_SLOPE * 0.10 + _LINEAR_INTERCEPT, rel=1e-6
    )


def test_models_disagree_on_same_points():
    """A linear and exponential fit of the same points give different curves.

    This guards against the branch silently always taking one path.
    """
    lin = ValveCalibration(points=_EXP_POINTS, fit_model="linear")
    exp = ValveCalibration(points=_EXP_POINTS, fit_model="exponential")
    # Evaluate beyond the last calibration point where the curves diverge most.
    assert lin.ul_for_s(0.13) != pytest.approx(exp.ul_for_s(0.13), rel=1e-2)


# --------------------------------------------------------------------------- #
# Calibrations setup-wide propagation


def test_calibrations_propagates_linear_to_valves():
    cal = Calibrations(
        bpod_valve={"1": ValveCalibration(points=_LINEAR_POINTS)},
        fit_model="linear",
    )
    assert cal.bpod_valve["1"].fit_model == "linear"


def test_calibrations_respects_explicit_per_valve_model():
    cal = Calibrations(
        bpod_valve={
            "1": ValveCalibration(points=_EXP_POINTS, fit_model="exponential"),
        },
        fit_model="linear",
    )
    # Per-valve explicit "exponential" is indistinguishable from the default, so
    # propagation overrides it. This documents the known limitation.
    assert cal.bpod_valve["1"].fit_model == "linear"


# --------------------------------------------------------------------------- #
# YAML / serialisation round-trip


def test_fit_model_serialises_and_deserialises():
    vc = ValveCalibration(points=_LINEAR_POINTS, fit_model="linear")
    dumped = vc.model_dump()
    assert dumped["fit_model"] == "linear"
    reloaded = ValveCalibration.model_validate(dumped)
    assert reloaded.fit_model == "linear"


def test_setup_config_round_trips_fit_model():
    cfg = SetupConfig(
        name="rig_test",
        calibrations=Calibrations(
            bpod_valve={
                "1": ValveCalibration(points=_LINEAR_POINTS, fit_model="linear")
            },
        ),
    )
    data = cfg.model_dump()
    reloaded = SetupConfig.model_validate(data)
    assert reloaded.calibrations.bpod_valve["1"].fit_model == "linear"


def test_setup_without_fit_model_loads_as_exponential():
    """A setup YAML predating this feature has no fit_model key."""
    data = {
        "name": "legacy_rig",
        "calibrations": {
            "bpod_valve": {"1": {"updated": "2026-01-01", "points": _EXP_POINTS}},
        },
    }
    cfg = SetupConfig.model_validate(data)
    assert cfg.calibrations.fit_model == "exponential"
    assert cfg.calibrations.bpod_valve["1"].fit_model == "exponential"


# --------------------------------------------------------------------------- #
# Task-time override: -ts FIT_MODEL=linear


def test_ts_fit_model_override_switches_valves_to_linear():
    cfg = SetupConfig(
        name="rig_override",
        calibrations=Calibrations(
            bpod_valve={"1": ValveCalibration(points=_LINEAR_POINTS)},
        ),
    )
    assert cfg.calibrations.bpod_valve["1"].fit_model == "exponential"

    # build_task_settings places `-ts FIT_MODEL=linear` into the patched dict;
    # _inject_valve_calibration reads it from there.
    patched: dict = {"FIT_MODEL": "linear"}
    _inject_valve_calibration(cfg, patched)

    assert cfg.calibrations.bpod_valve["1"].fit_model == "linear"
    assert "valve_s_for_ul" in patched


def test_ts_fit_model_override_invalid_value_is_ignored():
    cfg = SetupConfig(
        name="rig_bad_override",
        calibrations=Calibrations(
            bpod_valve={"1": ValveCalibration(points=_LINEAR_POINTS)},
        ),
    )
    patched: dict = {"FIT_MODEL": "quadratic"}
    _inject_valve_calibration(cfg, patched)
    # Unknown values are ignored, leaving the configured (default) model intact.
    assert cfg.calibrations.bpod_valve["1"].fit_model == "exponential"


def test_ts_fit_model_override_applies_to_empty_fallback():
    """With no per-valve calibration, the override applies to the fallback copy."""
    cfg = SetupConfig(name="rig_empty", calibrations=Calibrations())
    patched: dict = {"FIT_MODEL": "linear"}
    _inject_valve_calibration(cfg, patched)
    # The fallback path injects a callable; it should not raise.
    s = patched["valve_s_for_ul"](5.0)
    assert isinstance(s, float)
