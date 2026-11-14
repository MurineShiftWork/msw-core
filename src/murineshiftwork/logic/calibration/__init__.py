"""Calibration package.

Re-export shim preserving the historical flat-module import surface:
``from murineshiftwork.logic.calibration import CalibrationDataLiquid`` etc.

Submodules:
- ``data``  : measurement containers (CalibrationData and subclasses).
- ``stats`` : fitting / outlier math (_exponential_function, flag_outlier_points).
- ``viz``   : matplotlib / PDF rendering of setup valve calibrations.
- ``valve_fitting`` : valve volume-vs-open-time curve fitting.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

# Re-exports resolve lazily (PEP 562) so importing ``valve_fitting`` does not load
# matplotlib / pandas through ``data`` and ``viz``.
_LAZY = {
    "CalibrationData": "data",
    "CalibrationDataLiquid": "data",
    "CalibrationDataSound": "data",
    "_exponential_function": "stats",
    "flag_outlier_points": "stats",
    "plot_setup_valve_calibrations": "viz",
    "save_calibration_pdfs": "viz",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        module = importlib.import_module(f"{__name__}.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CalibrationData",
    "CalibrationDataLiquid",
    "CalibrationDataSound",
    "_exponential_function",
    "flag_outlier_points",
    "plot_setup_valve_calibrations",
    "save_calibration_pdfs",
]
