"""Choose which of a setup's cameras a task records.

The choice is the task setting ``record_cameras``, resolved like any other setting
(task.yaml, the setup's ``task_overrides``, the subject's ``task_overrides`` or
``-ts record_cameras=top,bottom``). It applies to the groups that record with video
(``record: with_video``); groups with ``record: always`` start in every task run and
cannot be chosen.

- unset: every camera of every video group records;
- ``all``: the same, with the camera names written to the session settings;
- a list of values, each one of:

  - a camera name (a unit name, or for an RCE group without units, the agent's
    ``camera_config.description`` in the ensemble YAML);
  - ``group:<name>``: every camera of that group;
  - ``role:<role>``: every camera of the video groups with that role.

Values are given as a YAML list (``[top, group:side]``) or a comma-separated string.
A video group none of whose cameras is chosen does not start.

The choice is resolved and checked once, before any hardware starts: an unknown
name, group or role, an empty list or a setup whose cameras cannot be named refuses
the run. Reading the RCE ensemble YAML here is plain YAML: no backend import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraGroup

log = logging.getLogger(__name__)

RECORD_CAMERAS_KEY = "record_cameras"
#: ``record_cameras`` value meaning every camera of the video groups
RECORD_ALL = "all"
GROUP_PREFIX = "group:"
ROLE_PREFIX = "role:"


class CameraSelectionError(ValueError):
    """The requested camera selection cannot be applied to this setup."""


def parse_record_cameras(value: Any) -> list[str] | str:
    """Normalise a ``record_cameras`` value to a list of values, or ``"all"``.

    Accepts ``all``, a list or a string as given on the command line (``top``,
    ``top,group:side`` or ``[top, role:eye]``).

    Raises:
        CameraSelectionError: If the value is empty, not strings, or has a prefix
            other than ``group:`` and ``role:``.
    """
    if isinstance(value, str) and value.strip() == RECORD_ALL:
        return RECORD_ALL
    if isinstance(value, str):
        names = [
            part.strip().strip("'\"").strip()
            for part in value.strip().strip("[]").split(",")
        ]
        names = [n for n in names if n]
    elif isinstance(value, list | tuple):
        if not all(isinstance(n, str) for n in value):
            raise CameraSelectionError(
                f"{RECORD_CAMERAS_KEY} must list camera names, got {value!r}"
            )
        names = [n.strip() for n in value if n.strip()]
    else:
        raise CameraSelectionError(
            f"{RECORD_CAMERAS_KEY} must list camera names, got {value!r}"
        )
    if not names:
        raise CameraSelectionError(
            f"{RECORD_CAMERAS_KEY} is empty; to record no video, set the task "
            "setting record_video: false"
        )
    for n in names:
        if ":" not in n:
            continue
        if not n.startswith((GROUP_PREFIX, ROLE_PREFIX)):
            raise CameraSelectionError(
                f"{RECORD_CAMERAS_KEY} {n!r}: a prefix is {GROUP_PREFIX!r} or "
                f"{ROLE_PREFIX!r}"
            )
        if not n.split(":", 1)[1].strip():
            raise CameraSelectionError(f"{RECORD_CAMERAS_KEY} {n!r} names nothing")
    return list(dict.fromkeys(names))


def _rce_agent_names(ensemble_cfg_file: str | Path) -> dict[str, str]:
    """Return ``{agent instance: camera name}`` for every agent of an RCE ensemble
    YAML, with ``""`` for an agent without a ``camera_config.description``."""
    data = yaml.safe_load(Path(ensemble_cfg_file).read_text(encoding="utf-8")) or {}
    agents = data.get("agents") or {}
    names: dict[str, str] = {}
    for instance, agent in agents.items():
        cam = (agent or {}).get("camera_config") or {}
        names[str(instance)] = str(cam.get("description") or "").strip()
    return names


def rce_camera_names(ensemble_cfg_file: str | Path) -> dict[str, str]:
    """Return ``{agent instance: camera name}`` from an RCE ensemble YAML.

    Agents without a ``camera_config.description`` are left out.
    """
    return {i: n for i, n in _rce_agent_names(ensemble_cfg_file).items() if n}


def camera_names(group: CameraGroup) -> list[str]:
    """Return the names of a group's cameras, in configured order.

    A group lists its cameras as ``units``. An RCE group without units names them
    in its ensemble YAML (``settings.config``).

    Raises:
        CameraSelectionError: If the cameras have no usable names: a FLIR group
            using the ``n_cameras`` shorthand, RCE agents without a
            ``camera_config.description``, a missing RCE ensemble YAML, or a name
            used twice.
    """
    if group.units:
        names = group.unit_names
    elif group.backend == "rce":
        cfg_file = str(group.settings.get("config") or "")
        if not cfg_file or not Path(cfg_file).exists():
            raise CameraSelectionError(
                f"cameras: group {group.name!r}: RCE ensemble config not found "
                f"({cfg_file!r}); cannot check the camera names"
            )
        agents = _rce_agent_names(cfg_file)
        unnamed = sorted(i for i, n in agents.items() if not n)
        if not agents or unnamed:
            raise CameraSelectionError(
                f"cameras: group {group.name!r}: to choose cameras per task, give "
                f"every RCE agent a camera_config.description (unnamed: {unnamed})"
            )
        names = list(agents.values())
    else:
        raise CameraSelectionError(
            f"cameras: group {group.name!r}: to choose cameras per task, list them "
            "as named units (units: [{name: top, ...}])"
        )
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CameraSelectionError(f"cameras: duplicate camera names {dupes}")
    return names


def select_units(group: CameraGroup, names: list[str]) -> CameraGroup:
    """Return a copy of ``group`` that records only the cameras ``names``.

    Raises:
        CameraSelectionError: If a name is not one of the group's cameras, or a
            FLIR group pins a multi-camera Bonsai workflow.
    """
    from murineshiftwork.logic.config.models import CameraUnit

    known = camera_names(group)
    unknown = [n for n in names if n not in known]
    if unknown:
        raise CameraSelectionError(
            f"unknown camera name(s) {unknown}; group {group.name!r} has {known}"
        )
    workflow = str(group.settings.get("workflow") or "")
    if group.backend == "flir_bonsai" and workflow and not workflow.endswith("-1cam"):
        raise CameraSelectionError(
            f"cameras: group {group.name!r} workflow {workflow!r} records several "
            "cameras in one Bonsai process; choosing cameras needs a -1cam workflow"
        )
    if group.units:
        units = [u for u in group.units if u.name in names]
    else:
        units = [CameraUnit(name=n) for n in known if n in names]
    return group.model_copy(update={"units": units})


def resolve_record_cameras(
    groups: list[CameraGroup], value: Any
) -> tuple[list[CameraGroup] | None, list[str] | None]:
    """Resolve a set ``record_cameras`` value against the setup's camera groups.

    Returns ``(narrowed, recorded)``: ``narrowed`` is the list of groups for the run
    (None keeps the setup's groups, recording every camera), and ``recorded`` is the
    value to write back to the task settings. For ``all`` it is the list of camera
    names of the video groups; when those cameras have no usable names it is None:
    they all record, exactly as with ``record_cameras`` unset, and the setting is
    dropped so ``all`` never reaches the task.

    Raises:
        CameraSelectionError: If the setup has no video cameras, the value is
            empty or malformed, or names an unknown camera, group or role, or a
            group that records always.
    """
    video = [g for g in groups if g.record == "with_video"]
    if not video:
        raise CameraSelectionError(
            f"{RECORD_CAMERAS_KEY} is set but the setup has no cameras that record "
            "with video"
        )
    values = parse_record_cameras(value)
    if isinstance(values, str):  # RECORD_ALL
        try:
            return None, [n for g in video for n in camera_names(g)]
        except CameraSelectionError as exc:
            log.warning(
                "%s: all on cameras without usable names (%s); recording all of "
                "them, as when unset",
                RECORD_CAMERAS_KEY,
                exc,
            )
            return None, None

    always = {g.name: g for g in groups if g.record == "always"}
    names_of: dict[str, list[str]] = {}
    naming_errors: list[CameraSelectionError] = []
    for g in video:
        try:
            names_of[g.name] = camera_names(g)
        except CameraSelectionError as exc:
            naming_errors.append(exc)
    # group name -> chosen camera names, or None for the whole group
    chosen: dict[str, list[str] | None] = {}
    recorded: list[str] = []

    def choose_whole(g: CameraGroup) -> None:
        chosen[g.name] = None
        recorded.extend(names_of.get(g.name) or [f"{GROUP_PREFIX}{g.name}"])

    for v in values:
        if v.startswith(GROUP_PREFIX):
            gname = v[len(GROUP_PREFIX) :].strip()
            if gname in always:
                raise CameraSelectionError(
                    f"{RECORD_CAMERAS_KEY} {v!r}: group {gname!r} records always"
                )
            match = [g for g in video if g.name == gname]
            if not match:
                raise CameraSelectionError(
                    f"{RECORD_CAMERAS_KEY} {v!r}: unknown camera group; video groups "
                    f"are {[g.name for g in video]}"
                )
            choose_whole(match[0])
        elif v.startswith(ROLE_PREFIX):
            role = v[len(ROLE_PREFIX) :].strip()
            match = [g for g in video if g.role == role]
            if not match:
                raise CameraSelectionError(
                    f"{RECORD_CAMERAS_KEY} {v!r}: no video group has role {role!r}"
                )
            for g in match:
                choose_whole(g)
        else:
            owner = next((g for g in video if v in names_of.get(g.name, [])), None)
            if owner is None:
                if any(v in g.unit_names for g in always.values()):
                    raise CameraSelectionError(
                        f"{RECORD_CAMERAS_KEY} {v!r}: the camera's group records always"
                    )
                if naming_errors:
                    raise naming_errors[0]
                known = [n for names in names_of.values() for n in names]
                raise CameraSelectionError(
                    f"unknown camera name(s) [{v!r}]; this setup has {known}"
                )
            picked = chosen.setdefault(owner.name, [])
            if picked is not None:
                picked.append(v)
            recorded.append(v)

    narrowed: list[CameraGroup] = []
    for g in groups:
        if g.record == "always":
            narrowed.append(g)
            continue
        if g.name not in chosen:
            continue
        picked = chosen[g.name]
        narrowed.append(g if picked is None else select_units(g, picked))
    return narrowed, list(dict.fromkeys(recorded))
