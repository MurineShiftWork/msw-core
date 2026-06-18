"""Bpod hardware driver namespace.

pybpod-api depends on `sca` (Sanworks Safe-and-Collaborative Architecture),
which is not on PyPI. Bpod symbols are imported lazily so the CLI starts
without pybpod-api importable; ImportError surfaces only when hardware is
first accessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from murineshiftwork.hardware.bpod.device import BpodDevice as BpodDevice
    from murineshiftwork.hardware.bpod.factory import BpodFactory as BpodFactory
    from murineshiftwork.hardware.bpod.override import (
        BpodOverrideAPI as BpodOverrideAPI,
    )
    from murineshiftwork.hardware.bpod.ttl import (
        add_trial_onset_ttl as add_trial_onset_ttl,
    )
    from murineshiftwork.hardware.bpod.valve import (
        make_sma_for_drop_of_water as make_sma_for_drop_of_water,
    )
    from murineshiftwork.hardware.bpod.valve import (
        make_sma_for_valve_pulse as make_sma_for_valve_pulse,
    )

_LAZY: dict[str, str] = {
    "BpodDevice": "murineshiftwork.hardware.bpod.device",
    "BpodFactory": "murineshiftwork.hardware.bpod.factory",
    "BpodOverrideAPI": "murineshiftwork.hardware.bpod.override",
    "add_trial_onset_ttl": "murineshiftwork.hardware.bpod.ttl",
    "make_sma_for_drop_of_water": "murineshiftwork.hardware.bpod.valve",
    "make_sma_for_valve_pulse": "murineshiftwork.hardware.bpod.valve",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> object:
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(_LAZY[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def patch_user_settings() -> None:
    """Patch MSW pybpod user settings into the confapp configuration."""
    from confapp import conf

    conf += "murineshiftwork.hardware.bpod.user_settings"
