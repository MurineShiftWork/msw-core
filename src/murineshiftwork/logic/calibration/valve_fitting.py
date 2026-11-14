"""Valve volume-vs-open-time curve fitting.

Pure functions over ``(points, fit_model)`` so the config model
(``ValveCalibration``) stays a plain schema and the heavy numeric imports
(scipy) are only paid when a lookup is actually evaluated.

``points`` is a list of ``[open_time_s, delivered_ul]`` pairs; ``fit_model`` is
``"exponential"`` (``a * exp(b * s) + c``) or ``"linear"`` (``a * s + b``).
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.optimize import curve_fit as _scipy_curve_fit


def _exp_model(x: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """a * exp(b * x) + c : the exponential valve volume-vs-time model."""
    return a * np.exp(b * x) + c


def _linear_model(x: np.ndarray, a: float, b: float) -> np.ndarray:
    """a * x + b : the linear valve volume-vs-time model."""
    return a * x + b


def clean_points(points: list[list[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Return (open_s, ul) arrays sorted by time with dead-zone points dropped.

    Points with volume <= 0 uL are removed before fitting so they do not
    corrupt the initial-guess calculation or pull the optimizer.
    """
    pts = sorted(points, key=lambda p: p[0])
    s = np.array([p[0] for p in pts], dtype=float)
    ul = np.array([p[1] for p in pts], dtype=float)
    mask = ul > 0
    return s[mask], ul[mask]


def fit_exponential(s: np.ndarray, ul: np.ndarray) -> tuple[float, float, float]:
    """Fit a * exp(b * x) + c. Returns (a, b, c).

    Initial guesses are derived from the data so curve_fit converges reliably
    even with sparse calibration sets.
    """
    ul_min, ul_max = float(ul.min()), float(ul.max())
    s_span = float(s.max() - s.min())
    b0 = np.log(ul_max / ul_min) / s_span if s_span > 0 and ul_min > 0 else 5.0
    a0 = ul_min
    c0 = 0.0

    try:
        popt, _ = _scipy_curve_fit(
            _exp_model,
            s,
            ul,
            p0=[a0, b0, c0],
            bounds=([0.0, 0.0, -np.inf], [np.inf, np.inf, np.inf]),
            maxfev=10_000,
        )
        return float(popt[0]), float(popt[1]), float(popt[2])
    except RuntimeError as exc:
        raise ValueError(
            f"Exponential fit failed: check calibration points for valve.\n{exc}"
        ) from exc


def fit_linear(s: np.ndarray, ul: np.ndarray) -> tuple[float, float]:
    """Fit a * x + b via least squares. Returns (a, b)."""
    a, b = np.polyfit(s, ul, 1)
    return float(a), float(b)


def fit(points: list[list[float]], fit_model: str) -> tuple[float, ...]:
    """Fit the selected model to ``points``.

    Returns (a, b, c) for the exponential model or (a, b) for the linear
    model. At least 3 positive-volume points are required after filtering
    dead-zone measurements.
    """
    s, ul = clean_points(points)

    if len(s) < 3:
        raise ValueError(
            f"Too few positive-volume calibration points ({len(s)}) after "
            "filtering dead-zone measurements: need at least 3."
        )

    if fit_model == "linear":
        return fit_linear(s, ul)
    return fit_exponential(s, ul)


def predict(points: list[list[float]], fit_model: str, s: np.ndarray) -> np.ndarray:
    """Evaluate the fitted model at the given open-time array."""
    params = fit(points, fit_model)
    if fit_model == "linear":
        return _linear_model(s, *params)
    return _exp_model(s, *params)


def ul_for_s(points: list[list[float]], fit_model: str, open_s: float) -> float:
    """Delivered volume (uL) for an open time, warning when extrapolating."""
    pts = sorted(points, key=lambda p: p[0])
    s_min, s_max = pts[0][0], pts[-1][0]
    if open_s < s_min or open_s > s_max:
        logging.warning(
            "ValveCalibration.ul_for_s: open_s=%.4f s is outside calibrated "
            "range [%.4f, %.4f] s: extrapolating",
            open_s,
            s_min,
            s_max,
        )
    return float(predict(points, fit_model, np.array([open_s]))[0])


def s_for_ul(points: list[list[float]], fit_model: str, volume_ul: float) -> float:
    """Invert the fitted model numerically via dense sampling.

    Sampling is used rather than an analytical inverse because the analytical
    forms are numerically fragile near degenerate parameters and the same
    code path then works for both the exponential and linear models.

    The sample grid extends 50% beyond the calibrated time range so that
    requests outside the calibrated volume range extrapolate via the fit
    model rather than clamping silently at the boundary.
    """
    pts = sorted(points, key=lambda p: p[0])
    s_min, s_max = pts[0][0], pts[-1][0]
    margin = (s_max - s_min) * 0.5
    s_dense = np.linspace(max(0.0, s_min - margin), s_max + margin, 4000)
    ul_dense = predict(points, fit_model, s_dense)
    ul_lo, ul_hi = float(ul_dense.min()), float(ul_dense.max())
    if volume_ul < ul_lo or volume_ul > ul_hi:
        logging.warning(
            "ValveCalibration.s_for_ul: %.3f uL is outside calibrated "
            "range [%.3f, %.3f] uL: extrapolating",
            volume_ul,
            ul_lo,
            ul_hi,
        )
    return float(np.interp(volume_ul, ul_dense, s_dense))


def check_quality(
    points: list[list[float]], fit_model: str, r2_threshold: float = 0.95
) -> tuple[bool, str]:
    """Return (is_valid, reason).

    Checks:
    - At least 3 calibration points
    - All volumes positive
    - Volume monotonically increases with open time
    - Fit R-squared >= r2_threshold
    - Slope is positive (exponential: b > 0; linear: a > 0)
    """
    s, ul = clean_points(points)

    if len(s) < 3:
        return (
            False,
            f"only {len(s)} positive-volume point(s): need at least 3",
        )

    if np.any(np.diff(ul) <= 0):
        bad = int(np.argmax(np.diff(ul) <= 0)) + 1
        return (
            False,
            f"volume not monotonically increasing: point {bad} breaks order",
        )

    try:
        params = fit(points, fit_model)
    except ValueError as exc:
        return False, str(exc)

    if fit_model == "linear":
        slope = params[0]
        if slope <= 0:
            return (
                False,
                f"fit slope a = {slope:.4f} <= 0 (curve is not increasing)",
            )
    else:
        b = params[1]
        if b <= 0:
            return (
                False,
                f"fit parameter b = {b:.4f} <= 0 (curve is not exponential growth)",
            )

    ul_pred = predict(points, fit_model, s)
    ss_res = float(np.sum((ul - ul_pred) ** 2))
    ss_tot = float(np.sum((ul - ul.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if r2 < r2_threshold:
        return (
            False,
            f"R-squared = {r2:.3f} < {r2_threshold} (poor {fit_model} fit)",
        )

    params_str = ", ".join(
        f"{name}={val:.5f}" for name, val in zip("abc", params, strict=False)
    )
    return True, f"ok ({fit_model}, R-squared = {r2:.3f}, {params_str})"
