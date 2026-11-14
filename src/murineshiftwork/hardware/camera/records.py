"""Typed records of what the cameras did in one task run.

``CamerasRecord`` is written as a self-contained top-level ``cameras:`` block in the
behaviour acquisition's ``acquisition_manifest.yaml``. It holds the failure policy and,
per camera group, the backend and its version, the camera acquisition basename, clock
anchors at start and stop, and the final state of every camera.

Clock anchors pair the wall clock (``time.time_ns`` and its ISO form) with the
monotonic clock (``time.monotonic_ns``) read at the same instant, so camera timestamps
taken on either clock can be placed on the session timeline.

Older session manifests store a camera peer's cameras as a plain list of names. The
records accept that form when reading: a list of names becomes units with only a name.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

ACQUISITION_MANIFEST = "acquisition_manifest.yaml"
CAMERAS_KEY = "cameras"


class ClockAnchor(BaseModel):
    """Wall and monotonic clocks read at one instant."""

    wall_ns: int
    monotonic_ns: int
    wall_iso: str

    @classmethod
    def now(cls) -> ClockAnchor:
        wall_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        iso = datetime.fromtimestamp(wall_ns / 1e9, UTC).isoformat(
            timespec="microseconds"
        )
        return cls(wall_ns=wall_ns, monotonic_ns=monotonic_ns, wall_iso=iso)


class CameraUnitRecord(BaseModel):
    """Final state of one camera."""

    name: str
    hw_id: str = ""
    ok: bool | None = None
    state: str = ""
    error: str = ""
    error_code: str = ""
    files: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _from_name(cls, value: Any) -> Any:
        return {"name": value} if isinstance(value, str) else value


class CameraGroupRecord(BaseModel):
    """One camera group in one run.

    ``status`` is ``complete`` (every camera ok), ``partial`` (some failed) or
    ``failed`` (none ok, or the backend raised).
    """

    name: str = ""
    backend: str = ""
    backend_version: str = ""
    role: str = ""
    record: str = ""
    acq_type: str = ""
    basename: str = ""
    status: str = ""
    start: ClockAnchor | None = None
    stop: ClockAnchor | None = None
    units: list[CameraUnitRecord] = Field(default_factory=list)


class CamerasRecord(BaseModel):
    """The ``cameras:`` block of a behaviour acquisition manifest."""

    policy: str = ""
    groups: list[CameraGroupRecord] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_names(cls, value: Any) -> Any:
        # legacy: a plain list of camera names, as on older session-manifest peers
        if isinstance(value, list | tuple):
            return {"groups": [{"units": list(value)}]}
        return value

    @property
    def camera_names(self) -> list[str]:
        return [u.name for g in self.groups for u in g.units]


def peer_cameras(entry: dict[str, Any]) -> CameraGroupRecord:
    """Read a camera peer entry of ``session_manifest.yaml`` as a group record.

    The entry's ``cameras`` key is a list of names, or absent on peers written
    before cameras were named.
    """
    return CameraGroupRecord(
        name=str(entry.get("group") or ""),
        backend=str(entry.get("backend") or ""),
        backend_version=str(entry.get("backend_version") or ""),
        role=str(entry.get("role") or ""),
        basename=str(entry.get("basename") or ""),
        status=str(entry.get("status") or ""),
        units=list(entry.get(CAMERAS_KEY) or []),
    )


def write_cameras_record(acquisition_folder: str | Path, record: CamerasRecord) -> None:
    """Set the top-level ``cameras:`` block of an acquisition manifest.

    The rest of the manifest is kept. The file is replaced atomically, so a reader
    never sees half a manifest. Does nothing when the manifest does not exist.
    """
    path = Path(acquisition_folder) / ACQUISITION_MANIFEST
    if not path.exists():
        log.warning("no %s in %s: camera record not written", path.name, path.parent)
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data[CAMERAS_KEY] = record.model_dump(mode="json")
    tmp = path.with_name(path.name + ".cameras.tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(
                data, default_flow_style=False, allow_unicode=True, sort_keys=False
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_cameras_record(acquisition_folder: str | Path) -> CamerasRecord | None:
    """Read the ``cameras:`` block of an acquisition manifest, if it has one."""
    path = Path(acquisition_folder) / ACQUISITION_MANIFEST
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = data.get(CAMERAS_KEY)
    return None if block is None else CamerasRecord.model_validate(block)
