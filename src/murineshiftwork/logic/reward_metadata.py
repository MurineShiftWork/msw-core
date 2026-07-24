"""Assemble the ``metadata.reward`` block recorded per session.

Additive and best-effort: captures the as-run reward so water vs milkshake cohorts
are comparable and the valve-calibration confound is auditable (see
``reviews/REVIEW_sequence_reward_magnitude.md``). Only fields that resolve from the
settings/calibration are included; absent inputs are simply omitted, so old sessions
and reward-free tasks are unaffected.

Single-spout tasks get a flat ``valve``/``valve_open_ms``/``calibrated_volume_ul``;
multi-spout tasks (e.g. left/right in probabilistic switching) get a ``by_valve``
mapping - so the shape works either way.
"""

from __future__ import annotations

from typing import Any


def build_reward_metadata(
    task_settings: dict[str, Any] | None,
    valve_calibrations: dict[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the ``metadata.reward`` dict, or None if no reward info is present.

    Args:
        task_settings: Resolved (patched) task settings. Read keys: ``reward_type``,
            ``reward_substance_note``, ``reward_amount_ul``, ``valve_s_for_ul``
            (callable ``(valve, ul) -> open_seconds``), ``HARDWARE_VALVES_FOR_WATER``.
        valve_calibrations: Optional ``{valve_id: ValveCalibration|dict}`` for
            per-valve provenance (``updated`` date, ``fit_model``).
    """
    ts = task_settings or {}
    reward: dict[str, Any] = {}

    if ts.get("reward_type"):
        reward["type"] = ts["reward_type"]
    if ts.get("reward_substance_note"):
        reward["substance_note"] = ts["reward_substance_note"]

    target_ul: float | None = None
    if ts.get("reward_amount_ul") is not None:
        try:
            target_ul = float(ts["reward_amount_ul"])
            reward["reward_amount_ul"] = target_ul
        except (TypeError, ValueError):
            target_ul = None

    # Per-valve commanded open time from the active calibration (the confound fix:
    # record commanded ms AND the calibrated ul it targets), when available.
    valve_s_for_ul = ts.get("valve_s_for_ul")
    by_valve: dict[Any, Any] = {}
    if callable(valve_s_for_ul) and target_ul is not None:
        for v in ts.get("HARDWARE_VALVES_FOR_WATER") or []:
            try:
                open_ms = round(float(valve_s_for_ul(v, target_ul)) * 1000.0, 3)
            except Exception:
                continue
            entry: dict[str, Any] = {
                "valve_open_ms": open_ms,
                "calibrated_volume_ul": target_ul,
            }
            prov = _calibration_provenance(valve_calibrations, v)
            if prov:
                entry["calibration"] = prov
            by_valve[v] = entry

    if len(by_valve) == 1:
        ((valve, entry),) = by_valve.items()
        reward["valve"] = valve
        reward.update(entry)
    elif by_valve:
        reward["by_valve"] = by_valve

    return reward or None


def _calibration_provenance(valve_calibrations, valve) -> dict[str, Any] | None:
    if not valve_calibrations or valve not in valve_calibrations:
        return None
    cal = valve_calibrations[valve]
    updated = getattr(cal, "updated", None)
    fit_model = getattr(cal, "fit_model", None)
    if isinstance(cal, dict):
        updated = updated or cal.get("updated")
        fit_model = fit_model or cal.get("fit_model")
    prov: dict[str, Any] = {}
    if updated:
        prov["date"] = updated
    if fit_model:
        prov["fit_model"] = fit_model
    return prov or None
