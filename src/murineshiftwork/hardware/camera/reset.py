"""End camera recordings a dead session left running.

Used by the per-setup session lock (``logic/session_lock.py``) when the previous
owner of a setup died, and by ``msw cameras reset``. Each group's backend is loaded
and configured as for a run; a backend implementing
``msw_plugin_api.camera.ResettableCameraBackend`` is asked to ``reset()`` (each
camera saves what it recorded and goes idle). Every backend is closed afterwards.

``reset_camera_group_reports`` returns one ``GroupReset`` per group, saying what
happened to it (``reset``, ``no_reset``, ``config_error``, ``error``, ``skipped``)
and never raises for a group. ``reset_camera_groups`` is the session lock's view:
it logs each group and returns only the groups that were reset.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from msw_plugin_api.camera import CameraBackend, CameraConfigError, UnitResult

from murineshiftwork.hardware.camera.session import load_backend, unit_specs

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraGroup

log = logging.getLogger(__name__)

#: the backend reset the group; ``units`` holds one result per camera
RESET = "reset"
#: the backend does not implement ``ResettableCameraBackend``
NO_RESET = "no_reset"
#: the backend is not installed, cannot be imported or refused its settings
CONFIG_ERROR = "config_error"
#: the backend raised while configuring or resetting
ERROR = "error"
#: not attempted: the installed msw-plugin-api has no ``ResettableCameraBackend``
SKIPPED = "skipped"

PROTOCOL_MISSING = "the installed msw-plugin-api has no ResettableCameraBackend"


@dataclass
class GroupReset:
    """What a reset did to one camera group."""

    name: str
    backend: str
    status: str
    units: list[UnitResult] = field(default_factory=list)
    error: str = ""


def _resettable_protocol() -> Any:
    try:
        from msw_plugin_api.camera import ResettableCameraBackend
    except ImportError:
        return None
    return ResettableCameraBackend


def reset_supported() -> bool:
    """Whether the installed msw-plugin-api defines ``ResettableCameraBackend``."""
    return _resettable_protocol() is not None


def _reset_one(
    group: CameraGroup, protocol: Any, load: Callable[[str], CameraBackend]
) -> GroupReset:
    out = GroupReset(name=group.name, backend=group.backend, status=ERROR)
    backend = None
    try:
        backend = load(group.backend)
        backend.configure(dict(group.settings), unit_specs(group))
        if not isinstance(backend, protocol):
            out.status = NO_RESET
            return out
        out.units = list(backend.reset() or [])  # type: ignore[attr-defined]
        out.status = RESET
    except CameraConfigError as exc:
        out.status, out.error = CONFIG_ERROR, str(exc)
    except Exception as exc:  # noqa: BLE001 - one group's failure must not stop the others
        out.status, out.error = ERROR, f"{type(exc).__name__}: {exc}"
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                log.debug("camera group %s: close failed", group.name)
    return out


def reset_camera_group_reports(
    groups: Sequence[CameraGroup],
    *,
    backend_loader: Callable[[str], CameraBackend] | None = None,
) -> list[GroupReset]:
    """Reset every resettable group and report every group; never raises for a group."""
    protocol = _resettable_protocol()
    if protocol is None:
        return [
            GroupReset(g.name, g.backend, SKIPPED, error=PROTOCOL_MISSING)
            for g in groups
        ]
    load = backend_loader or load_backend
    return [_reset_one(g, protocol, load) for g in groups]


def reset_camera_groups(
    groups: Sequence[CameraGroup],
    *,
    backend_loader: Callable[[str], CameraBackend] | None = None,
) -> dict[str, list[UnitResult]]:
    """Reset every resettable group, logging each; never raises for a group.

    Returns:
        The reset results per group name (groups not reset are absent).
    """
    if not groups:
        return {}
    reports = reset_camera_group_reports(groups, backend_loader=backend_loader)
    if reports[0].status == SKIPPED:
        log.info("camera reset skipped: %s", PROTOCOL_MISSING)
        return {}
    results: dict[str, list[UnitResult]] = {}
    for rep in reports:
        if rep.status == NO_RESET:
            log.info(
                "camera group %s: backend %s cannot reset, skipped",
                rep.name,
                rep.backend,
            )
        elif rep.status != RESET:
            log.warning("camera group %s: reset failed: %s", rep.name, rep.error)
        else:
            results[rep.name] = rep.units
            for r in rep.units:
                log.log(
                    logging.INFO if r.ok else logging.WARNING,
                    "Camera %s/%s reset: %s%s",
                    rep.name,
                    r.name,
                    r.state,
                    f": {r.error}" if r.error else "",
                )
    return results
