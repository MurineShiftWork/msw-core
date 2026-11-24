"""End camera recordings a dead session left running.

Used by the per-setup session lock (``logic/session_lock.py``) when the previous
owner of a setup died. Each group's backend is loaded and configured as for a run;
a backend implementing ``msw_plugin_api.camera.ResettableCameraBackend`` is asked to
``reset()`` (each camera saves what it recorded and goes idle). Backends without it
are skipped with a log line, as is everything when the installed msw-plugin-api
predates the protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from msw_plugin_api.camera import CameraBackend, UnitResult

from murineshiftwork.hardware.camera.session import load_backend, unit_specs

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraGroup

log = logging.getLogger(__name__)


def _resettable_protocol() -> Any:
    try:
        from msw_plugin_api.camera import ResettableCameraBackend
    except ImportError:
        return None
    return ResettableCameraBackend


def reset_camera_groups(
    groups: Sequence[CameraGroup],
    *,
    backend_loader: Callable[[str], CameraBackend] | None = None,
) -> dict[str, list[UnitResult]]:
    """Reset every resettable group; never raises for a group.

    Returns:
        The reset results per group name (groups not reset are absent).
    """
    if not groups:
        return {}
    protocol = _resettable_protocol()
    if protocol is None:
        log.info(
            "camera reset skipped: the installed msw-plugin-api has no "
            "ResettableCameraBackend"
        )
        return {}
    load = backend_loader or load_backend
    results: dict[str, list[UnitResult]] = {}
    for group in groups:
        backend = None
        try:
            backend = load(group.backend)
            backend.configure(dict(group.settings), unit_specs(group))
            if not isinstance(backend, protocol):
                log.info(
                    "camera group %s: backend %s cannot reset, skipped",
                    group.name,
                    group.backend,
                )
                continue
            results[group.name] = list(backend.reset() or [])
        except Exception:  # noqa: BLE001 - one group's failure must not stop the others
            log.warning("camera group %s: reset failed", group.name, exc_info=True)
            continue
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:  # noqa: BLE001
                    log.debug("camera group %s: close failed", group.name)
        for r in results[group.name]:
            log.log(
                logging.INFO if r.ok else logging.WARNING,
                "Camera %s/%s reset: %s%s",
                group.name,
                r.name,
                r.state,
                f": {r.error}" if r.error else "",
            )
    return results
