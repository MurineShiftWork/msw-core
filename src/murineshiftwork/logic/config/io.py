from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import yaml

from murineshiftwork.logic.config.models import (
    SUBJECT_CONFIG_SCHEMA_VERSION,
    SetupConfig,
    SubjectConfig,
    ValveCalibration,
)

_YAML_DUMP_KWARGS: dict = {
    "default_flow_style": False,
    "allow_unicode": True,
    "sort_keys": False,
    "explicit_start": True,
}


def load_setup_config(config_dir: str | Path, setup_name: str) -> SetupConfig | None:
    """Load SetupConfig from {config_dir}/setups/{setup_name}.yaml.

    Returns None silently if the file does not exist, so callers fall through
    to the existing flat-flag path without any change in behaviour.
    """
    if not setup_name or setup_name.startswith("unknown_"):
        return None
    path = Path(config_dir) / "setups" / f"{setup_name}.yaml"
    if not path.exists():
        logging.warning(
            f"Setup '{setup_name}' not found at {path}: bpod port from CLI arg only"
        )
        return None
    with path.open() as f:
        data = yaml.safe_load(f)
    cfg = SetupConfig.model_validate(data)
    logging.debug(f"Loaded SetupConfig '{cfg.name}' from {path}")
    return cfg


def update_valve_calibration(
    config_dir: str | Path,
    setup_name: str,
    valve_id: int | str,
    new_calibration: ValveCalibration,
    force: bool = False,
) -> bool:
    """Write new_calibration for one valve into {config_dir}/setups/{setup_name}.yaml.

    Only the specific valve entry is replaced; all other setup fields are preserved
    verbatim (comments are lost on round-trip through yaml.dump, but structure is kept).

    Validation is run before writing unless force=True.  Returns True if the file
    was written, False if validation failed and force=False.

    Raises FileNotFoundError if the setup YAML does not exist yet (create it first
    or run `murineshiftwork register` for the setup).
    """
    path = Path(config_dir) / "setups" / f"{setup_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Setup config not found: {path}\n"
            f"Create the file first or run: murineshiftwork register --setup {setup_name}"
        )

    is_valid, reason = new_calibration.check_quality()
    if not is_valid:
        if force:
            logging.warning(
                f"Calibration for valve {valve_id} failed validation ({reason}): "
                f"writing anyway because force=True"
            )
        else:
            logging.error(
                f"Calibration for valve {valve_id} failed validation: {reason}. "
                f"Not writing to {path}. Pass force=True to override."
            )
            return False

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    valve_entry: dict = {
        "updated": new_calibration.updated,
        "points": new_calibration.points,
    }
    # Only persist a non-default fit_model so existing exponential setups round-trip
    # unchanged and no surprise key appears in their YAML.
    if new_calibration.fit_model != "exponential":
        valve_entry["fit_model"] = new_calibration.fit_model
    raw.setdefault("calibrations", {}).setdefault("bpod_valve", {})[str(valve_id)] = (
        valve_entry
    )

    with path.open("w") as f:
        yaml.dump(raw, f, **_YAML_DUMP_KWARGS)

    logging.info(
        f"Wrote calibration for valve {valve_id} of setup '{setup_name}' "
        f"({len(new_calibration.points)} points, updated {new_calibration.updated})"
    )
    return True


def _load_or_seed_subject(
    config_dir: str | Path, subject_name: str
) -> tuple[Path, dict]:
    """Return ``(path, raw)`` for a subject YAML: load+migrate if present, else seed a v2 skeleton.

    Shared by the subject writers (``save_subject_task_overrides`` /
    ``save_subject_task_state``). Ensures the subjects dir exists, stamps the current schema
    version, and leaves the caller to add its own section (``task_overrides`` / ``task_state``) -
    the seed deliberately carries only the common skeleton (``task_state`` is added by the state
    writer via ``setdefault``, so both writers produce identical output to before).
    """
    subjects_dir = Path(config_dir) / "subjects"
    subjects_dir.mkdir(parents=True, exist_ok=True)
    path = subjects_dir / f"{subject_name}.yaml"

    if path.exists():
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
        raw = _migrate_subject_config(raw)
    else:
        raw = {
            "schema_version": SUBJECT_CONFIG_SCHEMA_VERSION,
            "name": subject_name,
            "registered": "",
            "project": "",
            "experiment": "",
            "comment": "",
            "aliases": [],
            "task_overrides": {},
        }

    raw["schema_version"] = SUBJECT_CONFIG_SCHEMA_VERSION
    return path, raw


def save_subject_task_overrides(
    config_dir: str | Path,
    subject_name: str,
    task_name: str,
    overrides: dict,
) -> None:
    """Merge *overrides* into subject's task_overrides[task_name].

    Creates the subjects YAML file if it doesn't exist.
    Merges into existing task_overrides without overwriting other keys.
    Typical callers:
      - stage writeback: overrides={"stage_position": "mouse_t001"}
      - sequence level writeback: overrides={"start_level": 7}
      - mode writeback: overrides={"task_mode": "stage10deterministic"}
    """
    path, raw = _load_or_seed_subject(config_dir, subject_name)
    raw.setdefault("task_overrides", {}).setdefault(task_name, {}).update(overrides)

    with path.open("w") as f:
        yaml.dump(raw, f, **_YAML_DUMP_KWARGS)

    logging.info(
        f"Saved task_overrides {overrides} for subject '{subject_name}', task '{task_name}' -> {path}"
    )


def save_subject_task_stage_position(
    config_dir: str | Path,
    subject_name: str,
    task_name: str,
    position_name: str,
) -> None:
    """Write stage_position into subject's task_overrides for the given task."""
    save_subject_task_overrides(
        config_dir, subject_name, task_name, {"stage_position": position_name}
    )


def save_subject_task_state(
    config_dir: str | Path,
    subject_name: str,
    task_name: str,
    state: dict,
) -> None:
    """Deep-merge *state* into the subject's ``task_state[task_name]`` (machine-written progress).

    Companion to :func:`save_subject_task_overrides`, but for *earned state* rather than operator
    overrides. Creates the subjects YAML if absent; migrates it to the current schema first; and
    **deep-merges** so nested maps (e.g. the sequence task's per-sequence level map) update
    key-by-key instead of being replaced. Typical caller: the sequence session-end writeback with
    ``state={"sequences": {"default": {"level": 7, "updated": "..."}}}``.
    """
    from murineshiftwork.logic.config import deep_merge

    path, raw = _load_or_seed_subject(config_dir, subject_name)
    task_states = raw.setdefault("task_state", {})
    task_states[task_name] = deep_merge(task_states.get(task_name) or {}, state)

    with path.open("w") as f:
        yaml.dump(raw, f, **_YAML_DUMP_KWARGS)

    logging.info(
        f"Saved task_state {state} for subject '{subject_name}', task '{task_name}' -> {path}"
    )


def update_stage_config(
    config_dir: str | Path,
    setup_name: str,
    stage_controller_config: dict,
) -> bool:
    """Write updated axis limits and known_positions from a StageController config back to the setup YAML.

    Only axis limits (position_min, position_max, velocity_max, operating_mode) and
    known_positions are written: position_raw is transient hardware state and is skipped.
    Returns True if the file was written.
    """
    path = Path(config_dir) / "setups" / f"{setup_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Setup config not found: {path}")

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    stage = raw.setdefault("devices", {}).setdefault("stage", {})

    for axis_name, axis_data in stage_controller_config.get("axes", {}).items():
        axis = stage.setdefault("axes", {}).setdefault(axis_name, {})
        for key in (
            "position_min",
            "position_max",
            "velocity_max",
            "operating_mode",
        ):
            if key in axis_data:
                axis[key] = axis_data[key]

    known_positions = stage_controller_config.get("known_positions", {})
    if known_positions:
        stage["known_positions"] = known_positions

    with path.open("w") as f:
        yaml.dump(raw, f, **_YAML_DUMP_KWARGS)

    logging.info(f"Updated stage config in setup '{setup_name}' at {path}")
    return True


class SchemaVersionError(RuntimeError):
    """A config's schema version is newer than the running software supports.

    Raised on the forward-incompatible direction (config newer than software). The
    backward direction (config older than software) is handled by migration, not this error.
    """


def migrate_schema(
    raw: dict,
    *,
    target: int,
    steps: dict[int, Callable[[dict], dict]],
    version_key: str = "schema_version",
) -> dict:
    """Chain versioned migration steps on a raw config dict up to ``target``, losslessly.

    ``steps[n]`` upgrades a v(n-1) dict to vn; every step for a version above the dict's current
    ``version_key`` and up to ``target`` runs in order. Each step mutates and returns the raw
    dict; **keys a step does not touch are preserved verbatim** - no value is dropped, which is
    the guarantee callers rely on across schema upgrades. Idempotent once already at ``target``.
    Stamps ``version_key`` to ``target`` at the end.

    Generic (no config-type knowledge) so subject / task / setup configs can all reuse it; keep
    the per-step transforms task-agnostic where possible and let each task fold its own legacy
    state in at runtime.

    Forward-incompatibility guard: a dict whose ``version_key`` is **newer** than ``target``
    (written by newer software) is refused with :class:`SchemaVersionError` rather than silently
    downgraded - the running software cannot understand a future schema, and re-stamping it down
    would make a newer config lie about its version. Update the software instead.
    """
    version = int(raw.get(version_key, 0))
    if version > target:
        raise SchemaVersionError(
            f"config {version_key}={version} is newer than this software supports "
            f"({target}); update the software (a newer schema cannot be safely downgraded)."
        )
    for v in range(version + 1, target + 1):
        step = steps.get(v)
        if step is not None:
            raw = step(raw)
    raw[version_key] = target
    return raw


def _subject_v2(raw: dict) -> dict:
    """v1 -> v2: introduce the machine-written ``task_state`` section (empty container).

    Structural only: earned state (e.g. the sequence task's per-sequence level) is folded in by
    each task on first run under v2, so task-specific knowledge stays out of core. Existing
    ``task_overrides`` and every other key are left untouched.
    """
    raw.setdefault("task_state", {})
    return raw


# steps[n]: v(n-1) -> vn. v1 (from absent/v0) was a pure version stamp, so no transform.
_SUBJECT_MIGRATIONS: dict[int, Callable[[dict], dict]] = {2: _subject_v2}


def _migrate_subject_config(raw: dict) -> dict:
    """Upgrade a raw subject-config dict to SUBJECT_CONFIG_SCHEMA_VERSION, losslessly."""
    return migrate_schema(
        raw, target=SUBJECT_CONFIG_SCHEMA_VERSION, steps=_SUBJECT_MIGRATIONS
    )


def subject_config_schema_version(
    config_dir: str | Path, subject_name: str
) -> int | None:
    """On-disk ``schema_version`` of a subject config, without migrating it.

    Returns ``None`` if the file does not exist (new subject / INI fallback), else the raw
    integer (0 when the stamp is absent = legacy). Lets a caller decide to refuse a legacy
    config and point the operator at ``msw config migrate-subjects`` rather than silently
    coping with it.
    """
    path = Path(config_dir) / "subjects" / f"{subject_name}.yaml"
    if not path.exists():
        return None
    with path.open() as f:
        return int((yaml.safe_load(f) or {}).get("schema_version", 0))


def load_subject_config(
    config_dir: str | Path, subject_name: str
) -> SubjectConfig | None:
    """Load SubjectConfig from {config_dir}/subjects/{subject_name}.yaml.

    Returns None silently if the file does not exist; INI-based subject
    loading in evaluate.py is the fallback.
    Migrates legacy configs (no schema_version) transparently.
    """
    if not subject_name or subject_name.startswith("_test_"):
        return None
    path = Path(config_dir) / "subjects" / f"{subject_name}.yaml"
    if not path.exists():
        logging.debug(f"No subject config at {path}: using INI fallback")
        return None
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    data = _migrate_subject_config(data)
    cfg = SubjectConfig.model_validate(data)
    logging.debug(
        f"Loaded SubjectConfig '{cfg.name}' (schema v{cfg.schema_version}) from {path}"
    )
    return cfg
