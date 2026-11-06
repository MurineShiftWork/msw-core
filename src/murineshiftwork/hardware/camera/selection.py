"""Choose which of a setup's cameras a task records.

A camera's name is its view: the FLIR ``CameraUnit.name`` or the RCE agent's
``camera_config.description`` in the ensemble YAML. The choice is the task setting
``record_cameras``, resolved like any other setting (task.yaml, the setup's
``task_overrides``, the subject's ``task_overrides`` or
``-ts record_cameras=top,bottom``):

- unset: the task records all cameras, as before;
- ``all``: the task records every camera of the setup's camera config;
- a list of names: the task records only those cameras.

The choice is resolved and checked once, before any hardware starts: an unknown
name, an empty list or a setup whose cameras cannot be named refuses the run.
For a list of names the task receives a copy of the setup's ``CameraConfig`` with
``selected`` set, and the camera clients start only those cameras.

Reading the RCE ensemble YAML here is plain YAML: no rpi_camera_ensemble import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraConfig

log = logging.getLogger(__name__)

RECORD_CAMERAS_KEY = "record_cameras"
#: ``record_cameras`` value meaning every camera of the setup's camera config
RECORD_ALL = "all"


class CameraSelectionError(ValueError):
    """The requested camera selection cannot be applied to this setup."""


def parse_record_cameras(value: Any) -> list[str] | str:
    """Normalise a ``record_cameras`` value to a list of names, or ``"all"``.

    Accepts ``all``, a list of strings or a string as given on the command line
    (``top``, ``top,bottom`` or ``[top, bottom]``).

    Raises:
        CameraSelectionError: If the value is empty or not strings.
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
    return list(dict.fromkeys(names))


def rce_camera_names(ensemble_cfg_file: str | Path) -> dict[str, str]:
    """Return ``{agent instance: camera name}`` from an RCE ensemble YAML.

    Agents without a ``camera_config.description`` are left out.
    """
    data = yaml.safe_load(Path(ensemble_cfg_file).read_text(encoding="utf-8")) or {}
    agents = data.get("agents") or {}
    names: dict[str, str] = {}
    for instance, agent in agents.items():
        cam = (agent or {}).get("camera_config") or {}
        desc = str(cam.get("description") or "").strip()
        if desc:
            names[str(instance)] = desc
    return names


def camera_names(cameras: CameraConfig, config_file_camera: str = "") -> list[str]:
    """Return the names of the setup's cameras, in configured order.

    Args:
        cameras: The setup's camera config.
        config_file_camera: RCE ensemble YAML used when ``cameras.config`` is empty.

    Raises:
        CameraSelectionError: If the cameras have no usable names: FLIR units
            without names (or the ``n_cameras`` shorthand), a missing RCE ensemble
            YAML, or a name used twice.
    """
    if cameras.backend == "flir_bonsai":
        if not cameras.cameras or not all(c.name for c in cameras.cameras):
            raise CameraSelectionError(
                "cameras: to choose cameras per task, give every FLIR camera a name "
                "(cameras: [{index: 0, name: top}, ...]) instead of n_cameras"
            )
        names = [c.name for c in cameras.cameras]
    elif cameras.backend == "rce":
        cfg_file = cameras.config or config_file_camera
        if not cfg_file or not Path(cfg_file).exists():
            raise CameraSelectionError(
                f"cameras: RCE ensemble config not found ({cfg_file!r}); "
                "cannot check the camera names"
            )
        names = list(rce_camera_names(cfg_file).values())
    else:
        raise CameraSelectionError(
            f"cameras: backend {cameras.backend!r} does not support choosing cameras"
        )
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CameraSelectionError(f"cameras: duplicate camera names {dupes}")
    return names


def resolve_record_cameras(
    cameras: CameraConfig | None,
    value: Any,
    config_file_camera: str = "",
) -> tuple[CameraConfig | None, list[str] | str]:
    """Resolve a set ``record_cameras`` value against the setup's cameras.

    Returns ``(narrowed, recorded)``: ``narrowed`` is the camera config to hand to
    the task (None keeps the setup's own config, recording all cameras) and
    ``recorded`` is the value to write back to the task settings. For ``all`` it
    is the list of the setup's camera names, or ``"all"`` when the cameras have no
    usable names (they all record, as with ``record_cameras`` unset).

    Raises:
        CameraSelectionError: If the setup has no cameras, the value is empty or
            not names, or a name is unknown.
    """
    if cameras is None:
        raise CameraSelectionError(
            f"{RECORD_CAMERAS_KEY} is set but the setup has no cameras"
        )
    names = parse_record_cameras(value)
    if isinstance(names, str):  # RECORD_ALL
        try:
            return None, camera_names(cameras, config_file_camera)
        except CameraSelectionError as exc:
            log.warning(
                "%s: all on cameras without usable names (%s); recording all of them",
                RECORD_CAMERAS_KEY,
                exc,
            )
            return None, RECORD_ALL
    return select_cameras(cameras, names, config_file_camera), names


def select_cameras(
    cameras: CameraConfig, names: list[str], config_file_camera: str = ""
) -> CameraConfig:
    """Return a copy of ``cameras`` narrowed to ``names``.

    Raises:
        CameraSelectionError: If a name is not one of the setup's cameras, or a
            FLIR setup pins a multi-camera Bonsai workflow.
    """
    known = camera_names(cameras, config_file_camera)
    unknown = [n for n in names if n not in known]
    if unknown:
        raise CameraSelectionError(
            f"unknown camera name(s) {unknown}; this setup has {known}"
        )
    if (
        cameras.backend == "flir_bonsai"
        and cameras.workflow
        and not cameras.workflow.endswith("-1cam")
    ):
        raise CameraSelectionError(
            f"cameras.workflow {cameras.workflow!r} records several cameras in one "
            "Bonsai process; choosing cameras needs a -1cam workflow"
        )
    return cameras.model_copy(update={"selected": list(names)})
