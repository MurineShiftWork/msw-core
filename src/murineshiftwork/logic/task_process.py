import contextlib
import logging
import time
import uuid
from datetime import UTC
from pathlib import Path
from threading import Thread
from typing import Any

import yaml
from murineshiftwork.io import JsonlTrialDataWriter
from murineshiftwork.namespace import msw_file
from murineshiftwork.namespace.manifest import (
    append_acquisition_to_session,
    finalize_acquisition_in_session,
    init_acquisition_manifest,
    init_session_manifest,
)
from murineshiftwork.namespace.paths import generate_session_paths

from murineshiftwork.hardware.bpod import BpodConnectionError
from murineshiftwork.hooks import (
    HookContext,
    SessionAbortError,
    collect_hooks,
    run_post_hooks,
    run_pre_hooks,
)
from murineshiftwork.logic.host_info import (
    _get_git_commit,
    _get_host_info,
    _resolve_msw_version,
)
from murineshiftwork.logic.log import (
    add_session_log_handler,
    patch_logging_levels,
)
from murineshiftwork.logic.misc import (
    print_box,
)
from murineshiftwork.logic.paths import test_path_is_writable
from murineshiftwork.logic.reward_metadata import build_reward_metadata


def _resolve_hook_setup(run_context, execution_config):
    """The resolved ``SetupConfig`` for hook collection.

    Prefers the typed ``RunContext`` (spine refactor); falls back to the legacy
    ``execution_config`` bundle when a caller has not threaded the context through. Both hold the
    same ``SetupConfig`` instance, so the two paths are equivalent. Returns ``None`` if neither is
    present.
    """
    if run_context is not None:
        return run_context.setup
    if execution_config is not None:
        return execution_config.setup
    return None


def _resolve_run_identifiers(input_kwargs: dict) -> tuple:
    """``(debug, session_type, acq_type, session_version)`` from the RunContext, else the kwargs.

    Prefers the typed ``run_context`` (spine refactor, Cycle B); falls back to the loose keys when
    a caller has not threaded the context through. Returns the raw values - the caller applies the
    same ``or None`` / ``or "msw"`` / ``or 1`` defaults as before. ``task_settings`` is deliberately
    NOT resolved here: it stays on the shared dict because pre-hooks mutate it for the task to see.
    """
    ctx = input_kwargs.get("run_context")
    if ctx is not None:
        return ctx.debug, ctx.session_type, ctx.acq_type, ctx.session_version
    return (
        input_kwargs.get("debug", False),
        input_kwargs.get("session_type"),
        input_kwargs.get("acq_type"),
        input_kwargs.get("session_version"),
    )


def _ctx_field(input_kwargs: dict, field: str, key: str, default: str = "") -> str:
    """A RunContext ``field`` if the context is threaded through, else ``input_kwargs[key]``.

    The single-field form of the Cycle B/C reads: prefer ``run_context.<field>``, fall back to the
    loose kwargs key. Used for the resolved string identifiers (setup name, session_type).
    """
    ctx = input_kwargs.get("run_context")
    if ctx is not None:
        return getattr(ctx, field)
    return input_kwargs.get(key, default)


def _strip_unserializable(obj):
    """Recursively remove callables and other non-YAML-safe objects from dicts/lists."""
    if isinstance(obj, dict):
        return {k: _strip_unserializable(v) for k, v in obj.items() if not callable(v)}
    if isinstance(obj, list):
        return [_strip_unserializable(v) for v in obj if not callable(v)]
    return obj


def update_session_yaml(session_file_path, **sections):
    """Add or update top-level sections in .msw.session.yaml.

    Creates the file with msw_format_version: 2 if it does not exist yet.
    Typical callers: task_objects writing task_settings or stage after init.
    """
    yaml_path = str(msw_file(session_file_path, "session.yaml"))
    p = Path(yaml_path)
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    else:
        data = {"msw_format_version": 2}
    data.update(_strip_unserializable(sections))
    with Path(yaml_path).open("w") as f:
        yaml.safe_dump(
            data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


class TaskRunner(Thread):
    """Base class for task threads.

    Subclass and override ``run()``.  Check ``self.continue_task`` in the run
    loop so ``stop()`` can interrupt gracefully.  No Qt dependency: GUI layers
    can wrap this in a QThread adapter if needed.
    """

    bpod: Any = None
    input_kwargs: dict = {}
    continue_task = True

    def __init__(self, bpod=None, **kwargs):
        super().__init__(daemon=True)
        self.bpod = bpod
        self.input_kwargs = kwargs
        self.prepare()

    def prepare(self):
        """Override for setup work before the task starts (load settings, open video, etc.)."""
        logging.debug("No 'TaskRunner.prepare()' implementation.")

    def run(self) -> None:
        """Override with task loop logic.

        trial_index = 0
        max_trials = 1500
        while self.continue_task and trial_index < max_trials:
            ...
        """
        raise NotImplementedError(
            "This function has to get re-implemented in child classes."
        )

    def stop(self):
        self.continue_task = False

    def emit_trial(self, trial: dict) -> None:
        """Persist one scored trial through the framework's trial-data writer.

        A migrated task calls this once per scored trial instead of writing the file itself: the
        durable write goes through the injected ``TrialDataWriter`` (``input_kwargs['trial_writer']``)
        so the framework owns the destination path and on-disk format. A task that has not migrated
        never calls this, and the writer stays empty.
        """
        writer = self.input_kwargs.get("trial_writer")
        if writer is not None:
            writer.write_trial(trial)

    def get_path(self, artifact: str) -> Path:
        """Return the session file path for *artifact* (e.g. 'df.jsonl', 'log')."""
        return msw_file(
            self.input_kwargs["session_paths"]["session_file_path"], artifact
        )


class TaskProcess:
    """Manages one session: paths, bpod connection, task thread lifecycle.

    Bpod injection: pass a pre-opened ``RobustBpodSession`` via ``bpod=`` to let
    the caller (controller/hardware manager) own the hardware connection.  When
    ``bpod`` is None (default), TaskProcess opens the connection itself using
    ``serial_port_bpod``.
    """

    # Input
    serial_port = None
    task_in = None
    task_name = None
    subject = None
    input_kwargs: dict = {}
    # Run task
    session_paths: Any = None
    bpod: Any = None
    bpod_baudrate = 115200
    serial_is_open = False
    task_runner = None
    # Hooks
    _pre_hooks: list = []
    _post_hooks: list = []
    _hook_ctx: Any = None
    # LogAgent relay
    _relay_queue: Any = None
    _relay_proc: Any = None
    # Framework-owned trial-data writer (inert unless a task emits)
    _trial_writer: Any = None
    # HardwareManager owning this run's device collection (None when a bpod/collection is injected)
    _hw_manager: Any = None
    session_uuid: str = ""
    # Misc
    exiting = False
    debug = False

    def __init__(
        self,
        serial_port_bpod=None,
        out_path=None,
        subject=None,
        task=None,
        bpod=None,
        devices: dict | None = None,
        auto_init=True,
        auto_start=True,
        linked_to=None,
        require_bpod=True,
        simulate=False,
        **kwargs,
    ):
        super().__init__()
        self.serial_port = str(serial_port_bpod) if serial_port_bpod else ""
        self.out_path = str(out_path)
        self.subject = str(subject)
        self.task_in = str(task)
        self.input_kwargs = kwargs
        self.input_kwargs["subject"] = self.subject
        self.input_kwargs["serial_port_bpod"] = self.serial_port
        if devices:
            self.input_kwargs["devices"] = devices
        # Resolved run identifiers come from the typed RunContext (spine refactor, Cycle B),
        # falling back to the loose kwargs. task_settings is NOT among them (see helper docstring).
        _debug, _session_type, _acq_type, _session_version = _resolve_run_identifiers(
            self.input_kwargs
        )
        self.debug = _debug
        self.simulate = simulate
        self.session_uuid = str(uuid.uuid4())

        self.task_name = self.task_in
        _session_type = _session_type or None
        # v4.3: behaviour acquisitions are the "msw" acq system, with the task as
        # a visible token in the path (acq_type=msw, task=<name>). Typed
        # acquisitions (video_flir, pxi, ...) pass their own acq_type explicitly
        # and carry no task token.
        _acq_type = _acq_type or "msw"
        # task.yaml version is recorded in the session YAML (task_version), not as
        # a __vN path suffix -- v4.3 drops __vN on new writes.
        self.task_version = _session_version or 1
        self.session_paths = generate_session_paths(
            basepath=Path(self.out_path),
            subject=self.subject,
            task=self.task_name,
            acq_type=_acq_type,
            linked_to=linked_to,
            session_type=_session_type,
            acq_version=None,
        )
        self.input_kwargs["task_name"] = self.task_name
        self.input_kwargs["session_paths"] = self.session_paths

        if not self.task_name and not self.debug:
            raise ValueError(
                f"Task to run '{self.task_in}' not found or not specific enough."
            )

        Path(self.session_paths["session_folder"]).mkdir(parents=True, exist_ok=False)
        target_file = Path(self.session_paths["session_folder"]) / ".write_test"
        if not test_path_is_writable(target_file) and not self.debug:
            raise PermissionError(f"Session files not writable at {str(target_file)}")

        _container = Path(self.session_paths["session_folder"]).parent
        init_session_manifest(_container, self.session_paths["host_session_name"])
        append_acquisition_to_session(
            _container, self.session_paths["session_basename"]
        )
        # Acquisition-level provenance so a loaded acquisition is self-describing:
        # the task schema version (which evaluator produced its trials) and the reward
        # config. Both follow the acquisition, so they live on the acquisition manifest.
        _ts = self.input_kwargs.get("settings.task.patched", {})
        acq_metadata: dict = {
            "task": {
                "task": self.task_name,
                "task_schema_version": self.task_version,
            }
        }
        if _ts.get("scoring_metric"):
            acq_metadata["task"]["scoring_metric"] = _ts["scoring_metric"]
        reward_md = build_reward_metadata(_ts)
        if reward_md:
            acq_metadata["reward"] = reward_md
        init_acquisition_manifest(
            self.session_paths["session_folder"],
            self.session_paths["session_basename"],
            metadata=acq_metadata,
        )

        patch_logging_levels()
        add_session_log_handler(self.session_paths["session_file_path"])
        logging.info(
            "Session: task=%s subject=%s setup=%s",
            self.task_name,
            self.subject,
            _ctx_field(self.input_kwargs, "setup_name", "setup"),
        )
        _host = _get_host_info()
        logging.info(
            "Host: %s %s [%s] (%s) msw=%s commit=%s",
            _host.get("hostname", ""),
            _host.get("ip", ""),
            _host.get("mac", ""),
            _host.get("platform", ""),
            _resolve_msw_version(),
            _get_git_commit(),
        )
        logging.info("Session folder: %s", self.session_paths.get("session_folder", ""))
        # Framework-owned trial-data writer: a migrated task calls emit_trial() and the write lands
        # here instead of the task calling save_trial_data. Injected for every run but inert unless
        # the task emits -- exit_safely only finalises it when it has trials, so a task that still
        # writes its own file is untouched. Created after the session folder exists (its open()
        # would otherwise pre-create the folder and collide with the exclusive mkdir above).
        self._trial_writer = JsonlTrialDataWriter(
            msw_file(self.session_paths["session_file_path"], "df.jsonl")
        )
        self._trial_writer.open()
        self.input_kwargs["trial_writer"] = self._trial_writer
        self.persist_settings()
        self._start_relay()

        # Acquire the run's devices. A caller may inject a `bpod` handle or a pre-opened `devices`
        # collection and keep ownership; otherwise open the declared collection HERE - now that the
        # session folder exists, which bpod needs for its pybpod workspace. This is the single bpod
        # path (real, simulated, and the bare-serial-port fallback all go through the device wrapper
        # + HardwareManager); it replaces the old connect_bpod method.
        if bpod is None and devices is None:
            devices = self._open_devices(require_bpod)
        if bpod is None and devices is not None:
            bpod = devices.get("bpod")
        if devices is not None:
            self.input_kwargs["devices"] = devices

        if bpod is not None:
            logging.info(
                "Bpod: using %s handle",
                "device-collection" if self._hw_manager is not None else "injected",
            )
            self.bpod = bpod
            self.serial_is_open = True
        elif require_bpod:
            raise BpodConnectionError(
                "Bpod required but none available: no injected handle, no device list, "
                f"and no serial port (serial_port_bpod={self.serial_port!r})."
            )

        # Build hook context and load hooks (after bpod is connected)
        _task_settings = self.input_kwargs.get("settings.task.patched", {})
        _execution_config = self.input_kwargs.get("execution_config")
        # Read the resolved setup from the typed RunContext (spine refactor), falling back to the
        # legacy execution_config bundle. The HookContext still receives execution_config for hooks
        # that read it (migrated in a later phase).
        _setup_config = _resolve_hook_setup(
            self.input_kwargs.get("run_context"), _execution_config
        )
        self._hook_ctx = HookContext(
            subject=self.subject,
            task_name=self.task_name,
            task_settings=_task_settings,
            session_paths=self.session_paths,
            execution_config=_execution_config,
        )
        self._pre_hooks, self._post_hooks = collect_hooks(_setup_config, _task_settings)

        if auto_init:
            try:
                run_pre_hooks(self._pre_hooks, self._hook_ctx)
            except SessionAbortError:
                self.exit_safely()
                raise
            logging.info("Task init: %s", self.task_name)
            self.init_task()
        if auto_start:
            logging.info("Task start: %s", self.task_name)
            self.run_task()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        post_exc = None
        if self._hook_ctx is not None:
            try:
                run_post_hooks(self._post_hooks, self._hook_ctx)
            except SessionAbortError as exc:
                post_exc = exc
        status = "aborted" if exc_type is not None else "complete"
        if self.session_paths:
            _container = Path(self.session_paths["session_folder"]).parent
            finalize_acquisition_in_session(
                _container, self.session_paths["session_basename"], status=status
            )
        self.exit_safely()
        if post_exc is not None:
            raise post_exc

    def _start_relay(self) -> None:
        from murineshiftwork.logic.machine_config import read_log_config

        log_cfg = read_log_config()
        log_url = log_cfg["log_url"]
        if not log_url:
            return

        import multiprocessing
        from datetime import datetime

        try:
            from murineshiftwork.logagent.logagent import LogAgent
        except ImportError:
            logging.debug("msw-agent not installed: relay disabled")
            return

        bearer_token = log_cfg["log_bearer_token"]
        self._relay_queue = multiprocessing.Queue(maxsize=500)
        setup = _ctx_field(self.input_kwargs, "setup_name", "setup") or (
            self.input_kwargs.get("metadata", {}).get("setup", "")
        )
        session_start_payload = {
            "subject": self.subject,
            "task": self.task_name,
            "setup": setup,
            "session_uuid": self.session_uuid,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "session_paths": {k: str(v) for k, v in (self.session_paths or {}).items()},
        }
        self._relay_proc = LogAgent(
            self._relay_queue,
            log_url,
            setup=setup,
            session_start_payload=session_start_payload,
            session_uuid=self.session_uuid,
            bearer_token=bearer_token,
        )
        self._relay_proc.start()
        self.input_kwargs["relay_queue"] = self._relay_queue
        logging.debug("LogAgent started -> %s (setup=%r)", log_url, setup)

    def exit_safely(self):
        self.exiting = True
        # Finalise the trial-data write only if the task actually emitted (trial_count > 0). A task
        # that still writes its own file never touched the writer, so we leave its output alone.
        if self._trial_writer is not None:
            if self._trial_writer.trial_count > 0:
                with contextlib.suppress(Exception):
                    self._trial_writer.close()
            self._trial_writer = None
        if self._hw_manager is not None:
            # We opened the device collection: tear down every device (bpod included) in one place.
            self._hw_manager.close()
            self._hw_manager = None
            self.serial_is_open = False
        elif self.serial_is_open and self.bpod is not None:
            # Injected/legacy bpod handle we do not own a manager for.
            self.bpod.close_safely()
            self.serial_is_open = False
        if self._relay_queue is not None:
            with contextlib.suppress(Exception):
                self._relay_queue.put_nowait(None)
            self._relay_queue = None

    def _open_devices(self, require_bpod: bool):
        """Open this run's declared devices, now that the session folder exists.

        Device descriptors come from ``input_kwargs['device_list']`` (built by the CLI from the
        setup + ports, or the sim stand-ins under ``--simulate``). As a fallback for a
        ``require_bpod`` run with no declared list, a lone bpod device is built from the serial port
        (or a ``SimBpod`` under simulate) - this is what replaced the old ``connect_bpod`` path.
        Bpod receives the session folder as its pybpod workspace. Returns the opened
        ``DeviceCollection``, or ``None`` when there is nothing to open.
        """
        from murineshiftwork.hardware.bpod.device import BpodDevice
        from murineshiftwork.hardware.manager import HardwareManager

        device_list = list(self.input_kwargs.get("device_list") or [])
        if not device_list and (require_bpod or self.simulate):
            if self.simulate:
                device_list = [BpodDevice("", simulate=True)]
            elif self.serial_port:
                device_list = [BpodDevice(self.serial_port)]
        if not device_list:
            return None

        for dev in device_list:
            if getattr(dev, "name", "") == "bpod" and hasattr(dev, "set_workspace"):
                dev.set_workspace(
                    self.session_paths["session_folder"],
                    self.session_paths["session_basename"] + ".msw",
                )

        try:
            self._hw_manager = HardwareManager(device_list)
            return self._hw_manager.open()
        except RuntimeError as exc:
            print_box(f"\n{exc}\n")
            # Typed error instead of sys.exit so a GUI/RPC caller is not killed; the CLI catches it
            # at the top level and exits 1 (see cli.run_cli).
            raise BpodConnectionError(str(exc)) from exc

    def persist_settings(self):
        data = {
            "msw_format_version": 2,
            "process": {
                "msw_version": _resolve_msw_version(),
                "git_commit": _get_git_commit(),
                "host": _get_host_info(),
                "session_uuid": self.session_uuid,
                "task": self.task_name,
                "subject": self.subject,
                "setup": _ctx_field(self.input_kwargs, "setup_name", "setup"),
                "serial_port": self.serial_port,
                "out_path": self.out_path,
                "session_folder": str(self.session_paths.get("session_folder", "")),
                "session_basename": self.session_paths.get("session_basename", ""),
                "datetime": self.session_paths.get("datetime", ""),
                # Namespace identity, written so the file is self-describing
                # without depending on its directory name surviving intact.
                "namespace_version": self.session_paths.get(
                    "namespace_spec_version", ""
                ),
                "acq_type": self.session_paths.get("acq_type", ""),
                "acq_version": self.session_paths.get("acq_version"),
                "task_version": self.task_version,
                "session_type": _ctx_field(
                    self.input_kwargs, "session_type", "session_type"
                )
                or "",
                "host_session_name": self.session_paths.get("host_session_name", ""),
            },
        }
        ps_info = self.input_kwargs.get("host_session_info")
        if ps_info is not None:
            data["host_session"] = {
                "backend": ps_info.backend,
                "session_name": ps_info.session_name,
                "subject": ps_info.subject,
                "parent_directory": ps_info.parent_directory,
                **ps_info.extra,
            }
        yaml_path = str(
            msw_file(self.session_paths["session_file_path"], "session.yaml")
        )
        with Path(yaml_path).open("w") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def init_task(self):
        """Import specific Task and make self.task_runner Thread."""
        import shutil

        from murineshiftwork.cli.tasks import load_task_module

        try:
            mod = load_task_module(self.task_name)
            task_class = mod.Task
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                f"Cannot import 'Task' from task '{self.task_name}': {exc}"
            ) from exc

        plot_spec_src = Path(mod.__file__).parent / "plot_spec.yaml"
        if plot_spec_src.exists():
            dest = msw_file(self.session_paths["session_file_path"], "plot_spec.yaml")
            shutil.copy2(plot_spec_src, dest)
            logging.debug("plot_spec copied: %s", dest.name)

        self.task_runner = task_class(bpod=self.bpod, **self.input_kwargs)

    def run_task(self):
        """Run the Task thread."""
        self.task_runner.start()
        time.sleep(0.1)

    def is_running(self):
        return self.task_runner.is_alive()

    def stop_task(self):
        if self.task_runner is not None and self.is_running():
            self.task_runner.stop()
            if self.bpod is not None:
                with contextlib.suppress(Exception):
                    self.bpod.stop_trial()
            logging.debug("Task stopped.")
