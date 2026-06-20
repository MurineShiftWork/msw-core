"""Calibration fitting and outlier-detection math.

Pure numerical helpers used by the calibration data classes and visualisation:
the exponential valve model and a leave-one-out outlier flagger.
"""

import warnings

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit


def _exponential_function(x, a, b, c):
    return a * np.exp(b * x) + c


def flag_outlier_points(
    times_s,
    ul_values,
    sigma_threshold: float = 2.0,
):
    """Flag calibration points that deviate significantly from the fitted curve.

    Uses a leave-one-out (LOO) strategy: for each point, the curve is re-fitted
    on all *other* points and the residual of the left-out point is evaluated
    against that fit.  This avoids the masking problem where a single outlier
    pulls the global fit toward itself and hides its own residual.

    The spread of LOO residuals is summarised with the median absolute deviation
    (MAD), which is robust to the presence of one or two outliers.

    Parameters
    ----------
    times_s : array-like
        Valve opening times in seconds.
    ul_values : array-like
        Corresponding volume measurements in uL/drop.
    sigma_threshold : float
        Number of MAD-derived standard-deviation equivalents above which a
        point is flagged as an outlier.

    Returns
    -------
    outlier_mask : np.ndarray of bool
        True for each point that is an outlier.
    residuals : np.ndarray of float
        Signed LOO residuals (observed - predicted) for each point.
    """
    times_s = np.asarray(times_s, dtype=float)
    ul_values = np.asarray(ul_values, dtype=float)

    n = len(times_s)
    if n < 3:
        return np.zeros(n, dtype=bool), np.zeros(n, dtype=float)

    # Leave-one-out residuals: fit n-1 points, predict the held-out point
    loo_residuals = np.zeros(n)
    for i in range(n):
        mask = np.ones(n, dtype=bool)
        mask[i] = False
        x_loo = times_s[mask]
        y_loo = ul_values[mask]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", OptimizeWarning)
                popt, _ = curve_fit(
                    _exponential_function,
                    x_loo,
                    y_loo,
                    p0=[0.01, 20.0, 0.0],
                    maxfev=5000,
                )
            predicted_i = _exponential_function(times_s[i], *popt)
        except Exception:
            coeffs = np.polyfit(x_loo, y_loo, 1)
            predicted_i = np.polyval(coeffs, times_s[i])
        loo_residuals[i] = ul_values[i] - predicted_i

    # Robust scale: MAD converted to sigma equivalent (Gaussian normalisation)
    # Floor of 1e-6 prevents division by near-zero for perfectly fitted data
    mad = np.median(np.abs(loo_residuals - np.median(loo_residuals)))
    sigma = max(mad * 1.4826, 1e-6)

    outlier_mask = np.abs(loo_residuals) > sigma_threshold * sigma
    return outlier_mask, loo_residuals
