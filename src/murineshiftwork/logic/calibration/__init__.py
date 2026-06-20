"""Calibration package.

Re-export shim preserving the historical flat-module import surface:
``from murineshiftwork.logic.calibration import CalibrationDataLiquid`` etc.

Submodules:
- ``data``  : measurement containers (CalibrationData and subclasses).
- ``stats`` : fitting / outlier math (_exponential_function, flag_outlier_points).
- ``viz``   : matplotlib / PDF rendering of setup valve calibrations.
"""

from murineshiftwork.logic.calibration.data import (
    CalibrationData,
    CalibrationDataLiquid,
    CalibrationDataSound,
)
from murineshiftwork.logic.calibration.stats import (
    _exponential_function,
    flag_outlier_points,
)
from murineshiftwork.logic.calibration.viz import (
    plot_setup_valve_calibrations,
    save_calibration_pdfs,
)

__all__ = [
    "CalibrationData",
    "CalibrationDataLiquid",
    "CalibrationDataSound",
    "_exponential_function",
    "flag_outlier_points",
    "plot_setup_valve_calibrations",
    "save_calibration_pdfs",
]
