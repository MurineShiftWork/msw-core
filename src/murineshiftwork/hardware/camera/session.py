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
  finalises their peers and raises ``CameraStartError`` listing the cameras. No
  further group is started once one reports a failure.
- ``continue``: each failed camera is logged as a warning and the run goes on; a
  group left without a working camera is stopped.

An exception escaping a backend counts as every camera of that group failing.

``stop`` stops and closes every group even when one of them raises (a
``KeyboardInterrupt`` included), writes the record, then re-raises the first such
exception.

Each started group is a peer acquisition in ``session_manifest.yaml`` (finalised as
``complete``, ``partial`` or ``failed``) and the behaviour acquisition's
``acquisition_manifest.yaml`` gets a ``cameras:`` block (``CamerasRecord``).

Under ``--simulate`` the run resolves no camera group, and ``from_run`` returns a
simulated session instead: it lists one stand-in ``with_video`` group when the task
asks for video, so a task that requires a camera still runs, but it loads no
backend, starts nothing and writes no record. Video recording is off and the log
says so.
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

#: name and backend of the stand-in group of a simulated session
SIMULATED_GROUP = "simulated"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: what to install for the first-party backends
BACKEND_PACKAGES = {
    "flir_bonsai": "msw-flir-bonsai>=0.8.0 (extra: murineshiftwork[flir])",
    "rce": "rpi-camera-ensemble[msw]>=0.9.0 (extra: murineshiftwork[rce])",
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
        CameraConfigError: The backend is not installed, cannot be imported (a
            missing optional dependency, an old version), or is not a CameraBackend.
    """
    try:
        return load_camera_backend(name)
    except LookupError as exc:
        raise CameraConfigError(
            f"camera backend {name!r} is not installed: {backend_install_hint(name)}"
        ) from exc
    except ImportError as exc:
        raise CameraConfigError(
            f"camera backend {name!r} cannot be imported ({exc}): "
            f"{backend_install_hint(name)}"
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
    # the backend's stop was interrupted (a BaseException); cleared in effect once a
    # later stop succeeds
    stop_interrupted: bool = False
    skipped: bool = False
    started: bool = False
    stopped: bool = False
    closed: bool = False
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
    def broken(self) -> bool:
        """A backend call raised, or the stop was interrupted and never completed."""
        return self.raised or (self.stop_interrupted and not self.stopped)

    @property
    def any_ok(self) -> bool:
        names = self.camera_names
        return not self.broken and (
            not names or any(n not in self.failed for n in names)
        )

    @property
    def status(self) -> str:
        names = self.camera_names
        if self.broken or (names and all(n in self.failed for n in names)):
            return STATUS_FAILED
        return STATUS_PARTIAL if self.failed else STATUS_COMPLETE


class CameraSession:
    """Drive a run's camera groups through their backends.

    Args:
        groups: The groups this run records.
        policy: ``raise`` or ``continue``; see the module docstring.
        backend_loader: Creates a backend from its name (tests replace it).
        simulated: List the groups but drive none of them (``--simulate``): open,
            start and stop do nothing, nothing is recorded or written.
    """

    def __init__(
        self,
        groups: Sequence[CameraGroup] = (),
        policy: str = POLICY_RAISE,
        *,
        backend_loader: Callable[[str], CameraBackend] = load_backend,
        simulated: bool = False,
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(f"camera failure policy {policy!r}; expected {POLICIES}")
        self.policy = policy
        self._load = backend_loader
        self._runs = [_GroupRun(group=g, specs=unit_specs(g)) for g in groups]
        self._opened = False
        self._preflight_failures: list[tuple[str, UnitResult]] = []
        self._policy_applied = False  # open applied the policy to the preflight
        self._stopping = False  # stop was called: start nothing more
        self._closed = False  # every group stopped and closed
        self._host_folder: str = ""
        self.simulated = simulated
        #: the session was asked for video (it holds a ``with_video`` group)
        self.requests_video = any(g.record == "with_video" for g in groups)
        if self._runs and not simulated:
            _RUN_SESSIONS.append(self)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_run(
        cls, args_dict: dict[str, Any], record_video: bool | None = None
    ) -> CameraSession:
        """The session of a resolved run.

        Groups with ``record: always`` start in every task run; ``with_video`` groups
        (already narrowed by ``record_cameras``) start when the task records video.

        Under ``--simulate`` (``simulate`` in ``args_dict``) the session is simulated
        and holds one stand-in group when the task records video.

        Args:
            args_dict: The resolved run (``camera_groups``, ``camera_failure``).
            record_video: Whether the task records video; defaults to the task
                setting ``record_video``.
        """
        if record_video is None:
            patched = args_dict.get("settings.task.patched") or {}
            record_video = bool(patched.get("record_video", False))
        if args_dict.get("simulate"):
            return cls._simulated(record_video)
        groups = [
            g
            for g in args_dict.get("camera_groups") or []
            if g.record == "always" or record_video
        ]
        return cls(groups, policy=run_camera_failure(args_dict))

    @classmethod
    def _simulated(cls, record_video: bool) -> CameraSession:
        from murineshiftwork.logic.config.models import CameraGroup

        groups = []
        if record_video:
            groups = [CameraGroup(name=SIMULATED_GROUP, backend=SIMULATED_GROUP)]
            log.info("simulate: video recording off, no camera started")
        return cls(groups, simulated=True)

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
        if self._policy_applied or not self._runs or self.simulated:
            return
        failures = self.preflight()
        self._policy_applied = True
        self._enforce("preflight", failures)

    def preflight(self) -> list[tuple[str, UnitResult]]:
        """Load and configure every backend, then preflight every camera, without
        applying the failure policy.

        ``record()`` then holds every camera's preflight state. Nothing records;
        ``stop`` closes the backends. A second call (or ``open`` after this one)
        reuses the first call's results without touching a camera.

        Returns:
            ``(group, result)`` for every camera not ok at preflight.

        Raises:
            CameraConfigError: A backend is missing or rejects its settings (the
                backends are closed first).
        """
        if self._opened or not self._runs or self.simulated:
            return list(self._preflight_failures)
        if self._load is load_backend:
            from murineshiftwork.hardware.simulate_guard import refuse_real_hardware

            # evaluate starts no camera under --simulate; a task that builds its own
            # groups still never reaches a real camera
            refuse_real_hardware(
                "camera group(s) " + ", ".join(r.group.name for r in self._runs)
            )
        self._opened = True
        try:
            for run in self._runs:
                self._backend(run).configure(dict(run.group.settings), run.specs)
        except BaseException:
            self.stop("camera configuration failed")
            raise
        failures: list[tuple[str, UnitResult]] = []
        try:
            for run in self._runs:
                results = self._call(run, "preflight")
                failures += self._apply(run, results, "preflight", log_ok=False)
        except BaseException:
            self.stop("camera preflight failed")
            raise
        self._preflight_failures = failures
        return list(failures)

    def start(self, session_paths: dict[str, Any]) -> None:
        """Start every group into its own camera acquisition next to the behaviour one.

        ``session_paths`` is the behaviour acquisition's ``TaskProcess.session_paths``.

        Groups already started are left alone, so a second call starts nothing.

        Raises:
            CameraStartError: A camera failed to start under the ``raise`` policy.
        """
        if not self._runs or self.simulated:
            return
        from murineshiftwork.namespace.paths import generate_session_paths

        self.open()
        sp = session_paths
        targets: dict[str, CameraTarget] = {}
        try:
            for run in self._active():
                if run.started:
                    continue
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

        Groups already started are skipped. Under ``raise``, no further group is
        started once one reports a failed camera.

        Args:
            targets: Target per group name.
            host_folder: Behaviour acquisition folder that receives the camera
                record; empty to write none.
        """
        if self._stopping or self.simulated:
            return
        self._host_folder = host_folder
        failures: list[tuple[str, UnitResult]] = []
        try:
            for run in self._active():
                if run.started or run.group.name not in targets:
                    continue
                failures += self._start_group(run, targets[run.group.name])
                if failures and self.policy == POLICY_RAISE:
                    break
        except BaseException:
            self.stop("camera start failed")
            raise
        self._write_record()
        self._enforce("start", failures)

    def stop(self, reason: str = "completed") -> None:
        """Stop every started group, finalise the peers, close the backends and
        write the camera record. Safe to call repeatedly.

        Every group is stopped and closed even when another raised; the first
        exception that escaped (``KeyboardInterrupt`` included) is re-raised once
        the record is written. A group whose stop or close was interrupted is
        tried again by the next call.
        """
        if self._closed or not self._runs or self.simulated:
            return
        self._stopping = True
        first: BaseException | None = None
        for run in self._runs:
            try:
                if run.started and not run.stopped:
                    self._stop_group(run, reason)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                log.warning("camera group %s: stop failed", run.label, exc_info=True)
                first = first or exc
            finally:
                try:
                    self._close_group(run)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    first = first or exc
        self._closed = all(
            r.closed and (r.stopped or not r.started) for r in self._runs
        )
        try:
            self._write_record()
        finally:
            if first is not None:
                raise first

    def _camera_outcomes(self) -> tuple[list[str], list[str]]:
        """``(recorded, lost)`` as ``group/camera``: the one split of the session's
        cameras the exit code comes from. A camera recorded when its group started,
        did not raise and the camera did not fail (preflight, start or stop); every
        other camera of the session is lost, a group that never started included."""
        recorded: list[str] = []
        lost: list[str] = []
        for run in self._runs:
            ok_group = run.started and not run.broken
            for name in run.camera_names:
                full = f"{run.label}/{name}"
                (recorded if ok_group and name not in run.failed else lost).append(full)
        return recorded, lost

    def recorded_cameras(self) -> list[str]:
        """``group/camera`` of every camera that started and did not fail."""
        return self._camera_outcomes()[0]

    def lost_cameras(self) -> list[str]:
        """``group/camera`` of every requested camera that did not record."""
        return self._camera_outcomes()[1]

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

    def _close_group(self, run: _GroupRun) -> None:
        if run.closed:
            return
        if run.backend is not None:
            try:
                run.backend.close()
            except Exception:  # noqa: BLE001 - closing must not mask the stop
                log.warning("camera group %s: close failed", run.label, exc_info=True)
        run.closed = True

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
        result counts as failed).

        A unit's ``files`` are those of the latest phase that reported any, and
        an empty ``hw_id`` is taken from the ``host`` a remote backend reports.
        """
        results = [r for r in results or [] if r is not None]
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
            # a camera that failed in an earlier phase keeps that state: a later
            # "stopped" (a backend reporting every unit) does not undo it
            if not (r.ok and r.name in run.failed):
                rec.state = r.state
            if r.files:
                rec.files = list(dict.fromkeys(r.files))
            rec.extra.update(r.extra or {})
            if not rec.hw_id and rec.extra.get("host"):
                rec.hw_id = str(rec.extra["host"])
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
        results = self._call(run, "start", target)
        run.record.started = ClockAnchor.now()
        return self._apply(run, results, "start")

    def _stop_group(self, run: _GroupRun, reason: str) -> None:
        """Stop one group; its peer is finalised whatever happens.

        An exception escaping the backend's ``stop`` (only a ``BaseException``
        does, ``_call`` turns the rest into results) leaves the group not stopped
        and failed, so the next ``stop`` tries again; once that one succeeds, the
        group's status comes from its cameras' results.
        """
        try:
            try:
                results = self._call(run, "stop", reason)
            except BaseException:
                run.stop_interrupted = True
                raise
            finally:
                run.record.stop = ClockAnchor.now()
            run.stopped = True
            self._apply(run, results, "stop")
        finally:
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
        """Finalise the group's session-manifest peer as ``complete``, ``partial``
        (some cameras failed, the others recorded) or ``failed``.

        ``partial`` is a camera peer status only; behaviour acquisitions end
        ``complete`` or ``failed``.
        """
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


#: Camera sessions with groups created in this process since the last
#: ``reset_run_sessions``; the CLI reads them for the run's exit code.
_RUN_SESSIONS: list[CameraSession] = []


def reset_run_sessions() -> None:
    """Forget the camera sessions of an earlier run in this process."""
    _RUN_SESSIONS.clear()


def run_recorded_cameras() -> list[str]:
    """``group/camera`` of every camera recorded by this run's camera sessions."""
    return [name for s in _RUN_SESSIONS for name in s.recorded_cameras()]


def run_lost_cameras() -> list[str]:
    """``group/camera`` of every camera of this run's camera sessions that did not
    record (failed at preflight, at start or during the session)."""
    return [name for s in _RUN_SESSIONS for name in s.lost_cameras()]


def run_requested_video() -> bool:
    """Whether a camera session of this run was asked for video, whatever the task
    setting that decided it."""
    return any(s.requests_video for s in _RUN_SESSIONS)


def run_camera_failure(args_dict: dict[str, Any]) -> str:
    """The run's camera failure policy: the run override, else the setup's, else raise."""
    ctx = args_dict.get("run_context")
    value = getattr(ctx, "camera_failure", "") or args_dict.get("camera_failure") or ""
    if not value:
        setup = args_dict.get("setup_config")
        value = getattr(setup, "camera_failure", "") or POLICY_RAISE
    return str(value)
