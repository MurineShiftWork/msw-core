"""One camera session for every task: start, check and stop the setup's camera groups.

A task drives every camera through one ``CameraSession``::

    cams = CameraSession.from_run(args_dict)  # no camera groups: a no-op object
    cams.open()  # before TaskProcess: load backends, configure, preflight
    try:
        with TaskProcess(**args_dict) as tp:
            cams.start(tp.session_paths)  # paths, peers, start
            ...
    finally:
        cams.stop("completed")  # stops every started group, writes the record

Each group is driven by a backend plugin found under the ``msw.camera`` entry point.
Backends report one ``UnitResult`` per camera and never raise for a camera failure;
the session applies the run's failure policy:

- ``raise``: any camera not ok at preflight or start stops every started group,
  finalises their peers and raises ``CameraStartError`` listing the cameras.
- ``continue``: each failed camera is logged as a warning and the run goes on; a
  group left without a working camera is stopped.

An exception escaping a backend counts as every camera of that group failing.

Each started group is a peer acquisition in ``session_manifest.yaml`` (finalised as
``complete``, ``partial`` or ``failed``) and the behaviour acquisition's
``acquisition_manifest.yaml`` gets a ``cameras:`` block (``CamerasRecord``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from msw_plugin_api.camera import (
    UNIT_FAILED,
    CameraBackend,
    CameraConfigError,
    CameraTarget,
    CameraUnitSpec,
    UnitResult,
    load_camera_backend,
)
from murineshiftwork.namespace.manifest import (
    append_acquisition_to_session,
    finalize_acquisition_in_session,
)

from murineshiftwork.hardware.camera.records import (
    CameraGroupRecord,
    CamerasRecord,
    CameraUnitRecord,
    ClockAnchor,
    write_cameras_record,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from murineshiftwork.logic.config.models import CameraGroup

log = logging.getLogger(__name__)

POLICY_RAISE = "raise"
POLICY_CONTINUE = "continue"
POLICIES = (POLICY_RAISE, POLICY_CONTINUE)

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: what to install for the first-party backends
BACKEND_PACKAGES = {
    "flir_bonsai": "msw-flir-bonsai (extra: murineshiftwork[flir])",
    "rce": "rpi-camera-ensemble (extra: murineshiftwork[rce])",
}


class CameraStartError(RuntimeError):
    """Cameras failed at preflight or start under the ``raise`` policy."""

    def __init__(self, phase: str, failures: list[tuple[str, UnitResult]]) -> None:
        self.phase = phase
        self.failures = failures
        lines = [
            f"{group}/{r.name}: {r.state}" + (f": {r.error}" if r.error else "")
            for group, r in failures
        ]
        super().__init__(
            f"camera {phase} failed for {len(lines)} camera(s):\n  "
            + "\n  ".join(lines)
        )


def backend_install_hint(name: str) -> str:
    """What to install for camera backend ``name``."""
    package = BACKEND_PACKAGES.get(name)
    return f"install {package}" if package else "install the package that provides it"


def load_backend(name: str) -> CameraBackend:
    """Create the installed camera backend ``name``.

    Raises:
        CameraConfigError: The backend is not installed, or is not a CameraBackend.
    """
    try:
        return load_camera_backend(name)
    except LookupError as exc:
        raise CameraConfigError(
            f"camera backend {name!r} is not installed: {backend_install_hint(name)}"
        ) from exc
    except TypeError as exc:
        raise CameraConfigError(str(exc)) from exc


def unit_specs(group: CameraGroup) -> list[CameraUnitSpec]:
    """The group's cameras as the backend receives them."""
    return [
        CameraUnitSpec(name=u.name, hw_id=u.hw_id, settings=u.unit_settings)
        for u in group.units
    ]


@dataclass
class _GroupRun:
    """One group's state within a session."""

    group: CameraGroup
    specs: list[CameraUnitSpec]
    backend: CameraBackend | None = None
    units: dict[str, CameraUnitRecord] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    raised: bool = False
    skipped: bool = False
    started: bool = False
    stopped: bool = False
    acqdir: str = ""
    basename: str = ""
    record: CameraGroupRecord = field(default_factory=CameraGroupRecord)

    @property
    def label(self) -> str:
        return self.group.name

    @property
    def camera_names(self) -> list[str]:
        return [s.name for s in self.specs] or list(self.units)

    @property
    def any_ok(self) -> bool:
        names = self.camera_names
        return not self.raised and (
            not names or any(n not in self.failed for n in names)
        )

    @property
    def status(self) -> str:
        names = self.camera_names
        if self.raised or (names and all(n in self.failed for n in names)):
            return STATUS_FAILED
        return STATUS_PARTIAL if self.failed else STATUS_COMPLETE


class CameraSession:
    """Drive a run's camera groups through their backends.

    Args:
        groups: The groups this run records.
        policy: ``raise`` or ``continue``; see the module docstring.
        backend_loader: Creates a backend from its name (tests replace it).
    """

    def __init__(
        self,
        groups: Sequence[CameraGroup] = (),
        policy: str = POLICY_RAISE,
        *,
        backend_loader: Callable[[str], CameraBackend] = load_backend,
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(f"camera failure policy {policy!r}; expected {POLICIES}")
        self.policy = policy
        self._load = backend_loader
        self._runs = [_GroupRun(group=g, specs=unit_specs(g)) for g in groups]
        self._opened = False
        self._closed = False
        self._host_folder: str = ""

    # -- construction ------------------------------------------------------

    @classmethod
    def from_run(
        cls, args_dict: dict[str, Any], record_video: bool | None = None
    ) -> CameraSession:
        """The session of a resolved run.

        Groups with ``record: always`` start in every task run; ``with_video`` groups
        (already narrowed by ``record_cameras``) start when the task records video.

        Args:
            args_dict: The resolved run (``camera_groups``, ``camera_failure``).
            record_video: Whether the task records video; defaults to the task
                setting ``record_video``.
        """
        if record_video is None:
            patched = args_dict.get("settings.task.patched") or {}
            record_video = bool(patched.get("record_video", False))
        groups = [
            g
            for g in args_dict.get("camera_groups") or []
            if g.record == "always" or record_video
        ]
        return cls(groups, policy=run_camera_failure(args_dict))

    # -- public API --------------------------------------------------------

    @property
    def groups(self) -> list[CameraGroup]:
        return [r.group for r in self._runs]

    def __bool__(self) -> bool:
        return bool(self._runs)

    def __enter__(self) -> CameraSession:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop("completed" if exc is None else f"error: {exc_type.__name__}")

    def acq_type(self, group_name: str) -> str:
        """The acquisition type the group records under (loads its backend)."""
        run = next(r for r in self._runs if r.group.name == group_name)
        return self._backend(run).acq_type

    def open(self) -> None:
        """Load and configure every backend, then preflight every camera.

        Raises:
            CameraConfigError: A backend is missing or rejects its settings.
            CameraStartError: A camera failed preflight under the ``raise`` policy.
        """
        if self._opened or not self._runs:
            return
        self._opened = True
        try:
            for run in self._runs:
                self._backend(run).configure(dict(run.group.settings), run.specs)
        except BaseException:
            self.stop("camera configuration failed")
            raise
        failures: list[tuple[str, UnitResult]] = []
        for run in self._runs:
            results = self._call(run, "preflight")
            failures += self._apply(run, results, "preflight", log_ok=False)
        self._enforce("preflight", failures)

    def start(self, session_paths: dict[str, Any]) -> None:
        """Start every group into its own camera acquisition next to the behaviour one.

        ``session_paths`` is the behaviour acquisition's ``TaskProcess.session_paths``.

        Raises:
            CameraStartError: A camera failed to start under the ``raise`` policy.
        """
        if not self._runs:
            return
        from murineshiftwork.namespace.paths import generate_session_paths

        self.open()
        sp = session_paths
        targets: dict[str, CameraTarget] = {}
        try:
            for run in self._active():
                paths = generate_session_paths(
                    subject=sp["subject"],
                    task=sp["task"],
                    basepath=sp["basepath"],
                    acq_type=self._backend(run).acq_type,
                    linked_to=sp["host_session_name"],
                    datetime_str=sp["datetime"],
                    printout=False,
                )
                targets[run.group.name] = CameraTarget(
                    acqdir=str(paths["session_folder"]),
                    basename=str(paths["session_basename"]),
                    host_relpath=str(sp["session_folder_relative"]),
                    host_name=str(sp["session_basename"]),
                )
        except BaseException:
            self.stop("camera start failed")
            raise
        self.start_targets(targets, host_folder=str(sp["session_folder"]))

    def start_targets(
        self, targets: dict[str, CameraTarget], host_folder: str = ""
    ) -> None:
        """Start groups into already-built targets.

        Args:
            targets: Target per group name.
            host_folder: Behaviour acquisition folder that receives the camera
                record; empty to write none.
        """
        if self._closed:
            return
        self._host_folder = host_folder
        failures: list[tuple[str, UnitResult]] = []
        try:
            for run in self._active():
                if run.group.name in targets:
                    failures += self._start_group(run, targets[run.group.name])
        except BaseException:
            self.stop("camera start failed")
            raise
        self._write_record()
        self._enforce("start", failures)

    def stop(self, reason: str = "completed") -> None:
        """Stop every started group, finalise the peers, close the backends and
        write the camera record. Safe to call repeatedly."""
        if self._closed or not self._runs:
            return
        self._closed = True
        for run in self._runs:
            if run.started and not run.stopped:
                self._stop_group(run, reason)
        for run in self._runs:
            if run.backend is not None:
                try:
                    run.backend.close()
                except Exception:  # noqa: BLE001 - closing must not mask the stop
                    log.warning(
                        "camera group %s: close failed", run.label, exc_info=True
                    )
        self._write_record()

    def record(self) -> CamerasRecord:
        """The camera record as it stands."""
        groups = []
        for run in self._runs:
            rec = run.record.model_copy(
                update={
                    "name": run.group.name,
                    "backend": run.group.backend,
                    "role": run.group.role,
                    "record": run.group.record,
                    "basename": run.basename,
                    "status": run.status if (run.started or run.failed) else "",
                    "units": [
                        u.model_copy(update={"ok": u.name not in run.failed})
                        for u in run.units.values()
                    ],
                }
            )
            if run.backend is not None:
                rec.backend_version = str(getattr(run.backend, "version", "") or "")
                rec.acq_type = str(getattr(run.backend, "acq_type", "") or "")
            groups.append(rec)
        return CamerasRecord(policy=self.policy, groups=groups)

    # -- internals ---------------------------------------------------------

    def _backend(self, run: _GroupRun) -> CameraBackend:
        if run.backend is None:
            run.backend = self._load(run.group.backend)
        return run.backend

    def _active(self) -> list[_GroupRun]:
        return [r for r in self._runs if not r.skipped]

    def _call(self, run: _GroupRun, method: str, *args: Any) -> list[UnitResult]:
        """Call a backend method; an exception fails every camera of the group."""
        try:
            return list(getattr(self._backend(run), method)(*args) or [])
        except Exception as exc:  # noqa: BLE001 - a backend failure is a result
            log.warning("camera group %s: %s raised", run.label, method, exc_info=True)
            run.raised = True
            names = run.camera_names or [run.group.name]
            return [
                UnitResult(
                    name=n,
                    ok=False,
                    state=UNIT_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    error_code="backend_exception",
                )
                for n in names
            ]

    def _apply(
        self,
        run: _GroupRun,
        results: list[UnitResult],
        phase: str,
        *,
        log_ok: bool = True,
    ) -> list[tuple[str, UnitResult]]:
        """Record results; return the failed ones (a configured camera with no
        result counts as failed)."""
        reported = {r.name for r in results}
        results = results + [
            UnitResult(
                name=s.name,
                ok=False,
                state=UNIT_FAILED,
                error=f"no {phase} result reported",
                error_code="no_result",
            )
            for s in run.specs
            if s.name not in reported
        ]
        hw_ids = {s.name: s.hw_id for s in run.specs}
        failures = []
        for r in results:
            rec = run.units.setdefault(
                r.name, CameraUnitRecord(name=r.name, hw_id=hw_ids.get(r.name, ""))
            )
            rec.state = r.state
            rec.files = list(dict.fromkeys(rec.files + list(r.files)))
            rec.extra.update(r.extra)
            if not r.ok:
                run.failed.add(r.name)
                failures.append((run.label, r))
                if not rec.error:
                    rec.error, rec.error_code = r.error, r.error_code
            if log_ok or not r.ok:
                log.log(
                    logging.INFO if r.ok else logging.WARNING,
                    "Camera %s/%s %s: %s%s",
                    run.label,
                    r.name,
                    phase,
                    r.state,
                    f": {r.error}" if r.error else "",
                )
        return failures

    def _enforce(self, phase: str, failures: list[tuple[str, UnitResult]]) -> None:
        if not failures:
            return
        if self.policy == POLICY_RAISE:
            self.stop(f"camera {phase} failed")
            raise CameraStartError(phase, failures)
        for run in self._active():
            if not run.any_ok:
                log.warning(
                    "camera group %s has no working camera after %s: stopping it",
                    run.label,
                    phase,
                )
                run.skipped = True
                if run.started and not run.stopped:
                    self._stop_group(run, f"no working camera after {phase}")
        self._write_record()

    def _start_group(
        self, run: _GroupRun, target: CameraTarget
    ) -> list[tuple[str, UnitResult]]:
        backend = self._backend(run)
        Path(target.acqdir).mkdir(parents=True, exist_ok=True)
        run.acqdir, run.basename = target.acqdir, target.basename
        self._register_peer(run, backend)
        run.record.start = ClockAnchor.now()
        run.started = True
        return self._apply(run, self._call(run, "start", target), "start")

    def _stop_group(self, run: _GroupRun, reason: str) -> None:
        run.stopped = True
        results = self._call(run, "stop", reason)
        run.record.stop = ClockAnchor.now()
        self._apply(run, results, "stop")
        self._finalize_peer(run)

    def _register_peer(self, run: _GroupRun, backend: CameraBackend) -> None:
        extra = {
            "cameras": run.camera_names,
            "group": run.group.name,
            "role": run.group.role,
            "backend": run.group.backend,
            "backend_version": str(getattr(backend, "version", "") or ""),
        }
        try:
            append_acquisition_to_session(
                Path(run.acqdir).parent, run.basename, extra=extra
            )
        except Exception:  # noqa: BLE001 - bookkeeping never stops a recording
            log.warning(
                "could not register camera peer %s", run.basename, exc_info=True
            )

    def _finalize_peer(self, run: _GroupRun) -> None:
        try:
            finalize_acquisition_in_session(
                Path(run.acqdir).parent, run.basename, status=run.status
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "could not finalise camera peer %s", run.basename, exc_info=True
            )

    def _write_record(self) -> None:
        if not self._host_folder:
            return
        try:
            write_cameras_record(self._host_folder, self.record())
        except Exception:  # noqa: BLE001
            log.warning("could not write the camera record", exc_info=True)


def run_camera_failure(args_dict: dict[str, Any]) -> str:
    """The run's camera failure policy: the run override, else the setup's, else raise."""
    ctx = args_dict.get("run_context")
    value = getattr(ctx, "camera_failure", "") or args_dict.get("camera_failure") or ""
    if not value:
        setup = args_dict.get("setup_config")
        value = getattr(setup, "camera_failure", "") or POLICY_RAISE
    return str(value)
