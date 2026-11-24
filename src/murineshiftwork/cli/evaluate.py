import logging
from pathlib import Path

import yaml

from murineshiftwork.cli.defaults import (
    DEFAULT_CALIBRATION_FILE_LIQUID,
    DEFAULT_CALIBRATION_FILE_SOUND,
    DEFAULT_CALIBRATION_FILE_STAGE,
    available_tasks,
    default_config_dir,
    default_out_path,
)
from murineshiftwork.cli.preflight import preflight_hardware_check
from murineshiftwork.cli.tasks import (
    find_task_by_name,
    list_available_tasks,
    load_task_module,
    validate_task_yaml,
)
from murineshiftwork.hardware.camera.selection import (
    RECORD_CAMERAS_KEY,
    resolve_record_cameras,
)
from murineshiftwork.logic.config import (
    deep_merge,
    load_setup_config,
    load_subject_config,
    read_config,
    read_task_modes,
    validate_config_file_path,
)
from murineshiftwork.logic.config.io import subject_config_schema_version
from murineshiftwork.logic.config.models import (
    DEFAULT_SCALE_BAUDRATE,
    SUBJECT_CONFIG_SCHEMA_VERSION,
    CameraGroup,
)
from murineshiftwork.logic.log import setup_logging
from murineshiftwork.logic.machine_config import resolve_config_dir, resolve_data_dir
from murineshiftwork.logic.paths import get_host_ip, get_host_name
from murineshiftwork.logic.run_context import RunContext
from murineshiftwork.logic.task_settings import (
    merge_task_modes,
    resolve_task_settings,
)

# Re-export for anything that imported these from here before the split
__all__ = [
    "evaluate_args",
    "get_task_dir",
    "available_tasks",
    "default_config_dir",
    "default_out_path",
]


def get_task_dir(task=None):
    try:
        mod = load_task_module(task)
        return str(Path(mod.__file__).parent)
    except (ImportError, AttributeError):
        return ""


def _evaluate_metadata(args_dict):
    metadata_dict = {}
    # Key=value pairs from --meta
    metadata_list = args_dict.get("metadata_list", None)
    if metadata_list:
        kv_list = [v.strip().strip("'\"") for v in metadata_list if "=" in v]
        metadata_dict.update(dict(s.split("=", 1) for s in kv_list))
    # Named convenience arg overrides --meta
    if args_dict.get("experimenter"):
        metadata_dict["experimenter"] = args_dict["experimenter"]
    args_dict["metadata"] = metadata_dict
    return args_dict


def _evaluate_log_level(args_dict=None):
    if args_dict["debug"]:
        args_dict["log_level"] = "DEBUG"
    try:
        d_int = int(args_dict["log_level"])
        args_dict["log_int"] = d_int
        args_dict["log_level"] = logging.getLevelName(d_int)
    except ValueError:
        pass
    return args_dict


def _evaluate_task(args_dict=None):
    if args_dict["task"]:
        requested = args_dict["task"]
        args_dict["task"] = find_task_by_name(task_name=requested)
        if args_dict["task"] is None:
            raise ValueError(
                f"Unknown task '{requested}'. Available tasks:\n"
                + "\n".join(f"  {t}" for t in sorted(list_available_tasks()))
            )
        args_dict["task_dir"] = get_task_dir(task=args_dict["task"])
    else:
        raise ValueError(
            "Task name is required. Pass -t <task_name> to the run command."
        )
    return args_dict


def _evaluate_and_load_configs(args_dict=None):
    # Apply priority chain: CLI arg > env var > machine config > defaults
    config_dir = resolve_config_dir(cli_override=args_dict.get("config_dir", ""))
    args_dict["config_dir"] = config_dir
    if not Path(config_dir).exists():
        args_dict["config_dir"] = ""

    args_dict["out_path"] = resolve_data_dir(cli_override=args_dict.get("out_path", ""))

    args_dict["config_file_subjects"] = validate_config_file_path(
        config_file=args_dict.get("config_file_subjects", ""),
        default_dir=args_dict["config_dir"],
    )
    args_dict["config_file_task"] = validate_config_file_path(
        config_file=args_dict.get("config_file_task", "task.yaml"),
        default_dir=args_dict.get("task_dir", ""),
    )
    args_dict["config_file_camera"] = validate_config_file_path(
        config_file=args_dict.get("config_file_camera", ""),
        default_dir=args_dict["config_dir"],
    )
    if not args_dict["task"].startswith("_calibration"):
        args_dict["calibration_file_liquid"] = validate_config_file_path(
            config_file=args_dict.get(
                "calibration_file_liquid", DEFAULT_CALIBRATION_FILE_LIQUID
            ),
            default_dir=args_dict["config_dir"],
        )
        args_dict["calibration_file_sound"] = validate_config_file_path(
            config_file=args_dict.get(
                "calibration_file_sound", DEFAULT_CALIBRATION_FILE_SOUND
            ),
            default_dir=args_dict["config_dir"],
        )

    settings_task_default = (
        read_config(file=args_dict["config_file_task"])
        if args_dict["config_file_task"]
        else {}
    )

    # Config-dir overlay: deep-merge user edits on top of bundled defaults
    _overlay_path = (
        Path(args_dict["config_dir"]) / "tasks" / args_dict["task"] / "task.yaml"
        if args_dict.get("config_dir") and args_dict.get("task")
        else None
    )
    if _overlay_path and _overlay_path.exists():
        overlay_defaults = read_config(file=str(_overlay_path))
        settings_task_default = deep_merge(settings_task_default, overlay_defaults)
        args_dict["config_file_task_overlay"] = str(_overlay_path)
        logging.debug(f"Task config overlay applied from {_overlay_path}")
    else:
        args_dict["config_file_task_overlay"] = ""

    calibration_file_stage = args_dict.get(
        "calibration_file_stage", DEFAULT_CALIBRATION_FILE_STAGE
    )
    args_dict["calibration_file_stage"] = (
        Path(calibration_file_stage).expanduser().as_posix()
    )
    if Path(args_dict["calibration_file_stage"]).exists():
        with Path(args_dict["calibration_file_stage"]).open() as _f:
            args_dict["settings.stage"] = yaml.full_load(_f)
    else:
        args_dict["settings.stage"] = {}

    args_dict["settings.task.default"] = settings_task_default

    # Extract session_type and version from the raw bundled task.yaml.
    # read_config() strips top-level keys (returning only default:), so we
    # re-read the file directly here to get session_type and version.
    _raw_task_yaml: dict = {}
    _task_yaml_file = args_dict.get("config_file_task", "")
    if _task_yaml_file and Path(_task_yaml_file).exists():
        with Path(_task_yaml_file).open() as _f:
            _raw_task_yaml = yaml.safe_load(_f) or {}
    _task_schema = validate_task_yaml(args_dict.get("task", ""), _raw_task_yaml)
    # acq_type (v4.3): the acquisition SYSTEM, not the task. Behaviour tasks are
    # "msw" and carry the task name as a visible task token in the path; a task may
    # pin a typed acq_type (video_flir, pxi, photo, video_rce) for a camera/ephys-
    # primary acquisition, which then carries no task token. The framework injects
    # "msw" rather than reading it from task.yaml.
    from murineshiftwork.namespace.paths import _KNOWN_ACQ_TYPES

    _declared = _task_schema.session_type
    args_dict["acq_type"] = _declared if _declared in _KNOWN_ACQ_TYPES else "msw"
    # session_type: the session-container label. Opt-in only (via --session-type);
    # the container is a bare subject__datetime by default.
    args_dict["session_type"] = args_dict.get("session_type") or ""
    args_dict["session_version"] = _task_schema.version or None

    args_dict["setup_config"] = load_setup_config(
        config_dir=args_dict["config_dir"],
        setup_name=args_dict.get("setup", ""),
    )
    _refuse_outdated_subject_config(
        args_dict["config_dir"], args_dict.get("subject", "")
    )
    args_dict["subject_config"] = load_subject_config(
        config_dir=args_dict["config_dir"],
        subject_name=args_dict.get("subject", ""),
    )

    return args_dict


def _refuse_outdated_subject_config(config_dir: str, subject: str) -> None:
    """Refuse a run whose subject YAML has no or an older ``schema_version``.

    ``load_subject_config`` migrates in memory only, so a run would read a config the
    file does not hold; the operator migrates the file first. Runs before any
    hardware or session folder.
    """
    if not config_dir or not subject:
        return
    version = subject_config_schema_version(config_dir, subject)
    if version is None or version >= SUBJECT_CONFIG_SCHEMA_VERSION:
        return
    path = Path(config_dir) / "subjects" / f"{subject}.yaml"
    raise ValueError(
        f"\n\n\tSubject config {path} has schema_version "
        f"{version or 'missing'}; this software needs "
        f"{SUBJECT_CONFIG_SCHEMA_VERSION}.\n"
        f"\tMigrate it first: msw config migrate-subjects\n"
    )


def _stage_device_to_controller_config(device) -> dict:
    """Convert a StageTowerDevice to the dict format StageController.from_config() expects."""
    return {
        "connection": {
            "baudrate": device.baudrate,
            "timeout": device.timeout,
        },
        "axes": {
            name: {
                "id": ax.id,
                "position_min": ax.position_min,
                "position_max": ax.position_max,
                "velocity_max": ax.velocity_max,
                "operating_mode": ax.operating_mode,
            }
            for name, ax in device.axes.items()
        },
        "known_positions": device.known_positions,
    }


def _task_default_layers(args_dict: dict) -> list[tuple[str, dict]]:
    """The task-default layers (bundled task.yaml, then config_dir overlay), labelled by file."""
    layers: list[tuple[str, dict]] = []
    for key, prefix in (
        ("config_file_task", "task.yaml"),
        ("config_file_task_overlay", "overlay"),
    ):
        path = args_dict.get(key, "")
        if path:
            layers.append((f"{prefix}:{path}", read_config(file=path)))
    return layers


def _subject_label(args_dict: dict) -> str:
    """Provenance label for the subject's task_overrides layer (its YAML path)."""
    subject = args_dict.get("subject", "")
    if args_dict.get("config_dir") and subject:
        return (
            f"subject:{Path(args_dict['config_dir']) / 'subjects' / f'{subject}.yaml'}"
        )
    return f"subject:{subject}"


def _setup_label(args_dict: dict) -> str:
    """Provenance label for the setup's task_overrides layer (its YAML path)."""
    setup = args_dict.get("setup", "")
    if args_dict.get("config_dir") and setup:
        return f"setup:{Path(args_dict['config_dir']) / 'setups' / f'{setup}.yaml'}"
    return f"setup:{setup}"


def _extra_injections_from_args(args_dict: dict) -> dict:
    """Collect fallback-injection keys from args_dict for build_task_settings."""
    injections: dict = {}
    for key in (
        "calibration_file_liquid",
        "calibration_file_sound",
        "calibration_file_stage",
        "serial_port_stage",
        "serial_port_pulsepal",
        "serial_port_scale",
        "scale_type",
        "scale_baudrate",
        "scale_protocol",
        "settings.stage",
    ):
        if key in args_dict:
            injections[key] = args_dict[key]
    if args_dict.get("config_dir"):
        injections["config_dir"] = args_dict["config_dir"]
    return injections


def _inject_valve_calibration(setup_config, patched) -> None:
    """Inject valve_s_for_ul into patched task settings with three-tier fallback.

    Tier 1: setup has calibration for the requested port: use it, warn if stale.
    Tier 2: setup bpod_valve dict is entirely empty: use hardcoded fallback,
              print a loud warning; intended for debug runs only.
    Tier 3: setup has some calibration but the requested port is missing:
              hard fail: partial config is a misconfiguration, not an absence.
    """
    from datetime import datetime, timedelta

    from murineshiftwork.logic.config._defaults import _FALLBACK_VALVE_CALIBRATION

    cal = setup_config.calibrations.bpod_valve

    # Task-time override: `-ts FIT_MODEL=linear` (or exponential) switches the
    # calibration fit model for this session without editing the setup YAML.
    fit_model_override = patched.get("FIT_MODEL")
    if fit_model_override is not None and fit_model_override not in (
        "exponential",
        "linear",
    ):
        logging.warning(
            "Ignoring -ts FIT_MODEL=%r: expected 'exponential' or 'linear'",
            fit_model_override,
        )
        fit_model_override = None
    if fit_model_override is not None:
        for vc in cal.values():
            vc.fit_model = fit_model_override
        logging.info(
            "Valve calibration fit_model overridden to %r via -ts FIT_MODEL",
            fit_model_override,
        )

    if not cal:
        logging.warning(
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "NO VALVE CALIBRATION in setup '%s'.\n"
            "Using built-in fallback (generic approximate values).\n"
            "DO NOT USE THIS DATA FOR EXPERIMENTS: calibrate first.\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            setup_config.name,
        )
        # Copy the module-level fallback so a FIT_MODEL override does not leak
        # into later sessions sharing the same process.
        fallback = _FALLBACK_VALVE_CALIBRATION.model_copy()
        if fit_model_override is not None:
            fallback.fit_model = fit_model_override
        patched["valve_s_for_ul"] = lambda port, volume_ul: fallback.s_for_ul(volume_ul)
        return

    # Build a lookup using the SetupConfig method but also check staleness per port.
    stale_threshold = datetime.now() - timedelta(
        days=setup_config.calibrations.stale_days
    )
    for port, vc in cal.items():
        if vc.updated:
            try:
                updated_dt = datetime.fromisoformat(vc.updated)
                if updated_dt < stale_threshold:
                    age_days = (datetime.now() - updated_dt).days
                    logging.warning(
                        "Valve %s calibration on setup '%s' is %d days old "
                        "(last updated %s): recalibrate before data collection.",
                        port,
                        setup_config.name,
                        age_days,
                        vc.updated[:10],
                    )
            except ValueError:
                pass

    available_ports = sorted(cal.keys())

    def _valve_s_for_ul_checked(port, volume_ul, _cal=cal, _sc=setup_config):
        if str(port) not in _cal:
            raise ValueError(
                f"Valve port {port!r} has no calibration in setup '{_sc.name}'. "
                f"Calibrated ports: {available_ports}. "
                "Calibrate this valve before running a session with reward delivery."
            )
        return _sc.valve_s_for_ul(port, volume_ul)

    patched["valve_s_for_ul"] = _valve_s_for_ul_checked
    logging.debug("Injected valve_s_for_ul from SetupConfig into task settings")


def _resolve_against_config_dir(path: str, config_dir: str) -> str:
    """Resolve a setup-YAML file path: relative paths are relative to ``config_dir``,
    absolute paths (and any path when there is no config dir) are returned unchanged."""
    if not path or not config_dir or Path(path).expanduser().is_absolute():
        return path
    return str(Path(config_dir) / path)


def _apply_device_port(
    setup_config, args_dict, patched, device: str, *, set_patched: bool
) -> ValueError | None:
    """Resolve one device's serial port from SetupConfig into ``args_dict`` (and ``patched``).

    On success sets ``serial_port_{device}`` (and mirrors into ``patched`` when ``set_patched``)
    and returns ``None``. On failure sets nothing (the CLI value is left in place) and returns the
    ``ValueError`` so the caller can emit its device-specific warning and do any cleanup - the
    per-device warning text and fallbacks stay with the caller.
    """
    key = f"serial_port_{device}"
    try:
        resolved = setup_config.device_port(device)
    except ValueError as exc:
        return exc
    args_dict[key] = resolved
    if set_patched:
        patched[key] = resolved
    logging.debug(f"Resolved {device} port from SetupConfig: {resolved}")
    return None


def _resolve_setup_config_ports(args_dict, setup_config, patched):
    """Apply port and camera overrides from SetupConfig into args_dict and patched."""
    if setup_config and "bpod" in setup_config.devices:
        exc = _apply_device_port(
            setup_config, args_dict, patched, "bpod", set_patched=False
        )
        if exc:
            logging.warning(
                f"SetupConfig bpod port resolution failed ({exc}); "
                f"using CLI value {args_dict['serial_port_bpod']!r}"
            )

    if setup_config and "stage" in setup_config.devices:
        stage_dev = setup_config.devices["stage"]
        exc = _apply_device_port(
            setup_config, args_dict, patched, "stage", set_patched=True
        )
        if exc:
            logging.warning(f"SetupConfig stage port resolution failed ({exc})")
        # Always prefer setup config axes over old calibration files
        args_dict["settings.stage"] = _stage_device_to_controller_config(stage_dev)
        patched["settings.stage"] = args_dict["settings.stage"]
        logging.debug("Built settings.stage from SetupConfig stage device")

    if setup_config and "scale" in setup_config.devices:
        scale_dev = setup_config.devices["scale"]
        exc = _apply_device_port(
            setup_config, args_dict, patched, "scale", set_patched=True
        )
        if exc:
            logging.warning(
                f"SetupConfig scale port resolution failed ({exc}); "
                f"using CLI value {args_dict['serial_port_scale']!r}"
            )
        scale_type = getattr(scale_dev, "scale_type", "hx711")
        scale_baudrate = getattr(scale_dev, "baudrate", None) or DEFAULT_SCALE_BAUDRATE
        scale_protocol = getattr(scale_dev, "scale_protocol", None)
        args_dict["scale_type"] = scale_type
        args_dict["scale_baudrate"] = scale_baudrate
        args_dict["scale_protocol"] = scale_protocol
        patched["scale_type"] = scale_type

    if setup_config and "pulsepal" in setup_config.devices:
        exc = _apply_device_port(
            setup_config, args_dict, patched, "pulsepal", set_patched=True
        )
        if exc:
            logging.warning(
                f"SetupConfig pulsepal port resolution failed ({exc}); skipping pulsepal"
            )
            args_dict["serial_port_pulsepal"] = ""
            patched.pop("serial_port_pulsepal", None)
    elif setup_config:
        args_dict["serial_port_pulsepal"] = ""
        patched.pop("serial_port_pulsepal", None)

    _resolve_camera_groups(args_dict, setup_config)

    if setup_config:
        _inject_valve_calibration(setup_config, patched)


def _resolve_camera_groups(args_dict: dict, setup_config) -> None:
    """Resolve the setup's camera groups for the run into ``camera_groups``.

    A relative ``settings.config`` path is resolved against the config dir. An RCE
    group without one takes ``--config-file-camera``, and a run without camera
    groups whose ``--config-file-camera`` exists gets one RCE group from it, as
    before camera groups. An RCE group's ``data_dir`` defaults to the run's
    ``out_path``, where the conductor saves each acquisition's ensemble and
    conductor YAML. ``config_file_camera`` is set to the ensemble YAML of a video
    RCE group for tasks that read it directly.
    """
    if args_dict.get("simulate"):
        # A simulated run never reaches a camera: no groups, no legacy camera config
        # (CameraSession.open refuses real backends as a backstop).
        if (setup_config is not None and setup_config.cameras) or args_dict.get(
            "config_file_camera"
        ):
            logging.info("simulate: cameras not started")
        args_dict["camera_groups"] = []
        args_dict["config_file_camera"] = ""
        args_dict["cameras_config"] = None
        return
    config_dir = args_dict.get("config_dir", "")
    cli_camera = args_dict.get("config_file_camera", "") or ""
    cli_exists = bool(cli_camera) and Path(cli_camera).exists()
    groups = list(setup_config.cameras) if setup_config is not None else []
    if not groups and cli_exists:
        groups = [
            CameraGroup(name="rce", backend="rce", settings={"config": cli_camera})
        ]
    resolved = []
    for group in groups:
        settings = dict(group.settings)
        if isinstance(settings.get("config"), str) and settings["config"]:
            settings["config"] = _resolve_against_config_dir(
                settings["config"], config_dir
            )
        if group.backend == "rce" and not settings.get("config") and cli_exists:
            settings["config"] = cli_camera
        if group.backend == "rce" and args_dict.get("out_path"):
            settings.setdefault("data_dir", args_dict["out_path"])
        if (
            group.backend == "rce"
            and group.record == "with_video"
            and settings.get("config")
            and not cli_exists
        ):
            args_dict["config_file_camera"] = settings["config"]
            logging.debug(
                f"Resolved camera config from SetupConfig: {settings['config']}"
            )
        resolved.append(group.model_copy(update={"settings": settings}))
    args_dict["camera_groups"] = resolved
    # every group before record_video/record_cameras narrow the run: a stale session
    # recovery resets them all (popped by run_task, never reaches a task)
    args_dict["setup_camera_groups"] = list(resolved)
    _set_legacy_cameras_config(args_dict)


def _set_legacy_cameras_config(args_dict: dict) -> None:
    """``cameras_config``: the one group the deprecated ``make_camera_client`` drives
    (the first group recording with video), or None.

    The copy carries the run's camera failure policy, so the shim applies it
    without the task passing it."""
    groups = args_dict.get("camera_groups") or []
    group = next((g for g in groups if g.record == "with_video"), None)
    if group is not None and args_dict.get("camera_failure"):
        group = group.with_failure_policy(args_dict["camera_failure"])
    args_dict["cameras_config"] = group


def _resolve_camera_failure(args_dict: dict, setup_config) -> None:
    """The run's camera failure policy: ``--camera-failure``, else the setup's."""
    value = args_dict.get("camera_failure") or ""
    args_dict["camera_failure"] = value or (
        setup_config.camera_failure if setup_config is not None else "raise"
    )
    _set_legacy_cameras_config(args_dict)


def _resolve_record_cameras(args_dict: dict, patched: dict) -> None:
    """Narrow the run's camera groups to the cameras the task records.

    ``record_cameras`` is an ordinary task setting, so any settings layer can set it
    (the setup's or subject's ``task_overrides``, ``-ts``) and its provenance comes
    from the resolver. Unset, the task records all cameras and nothing changes. The
    resolved list is written back to the task settings so the session YAML records
    it; ``all`` on cameras without usable names is dropped instead, as if unset.
    With ``record_video`` off the setting is dropped unchecked, so a subject-level
    choice does not refuse runs on rigs without those cameras.
    Raises before any hardware starts when a value is unknown or the list is empty.

    ``record_video: false`` also drops the ``with_video`` groups and the legacy
    ``cameras_config``/``config_file_camera``, so a task that drives the camera through
    ``make_camera_client`` without checking ``record_video`` starts no video camera.
    """
    value = patched.get(RECORD_CAMERAS_KEY)
    provenance = args_dict.get("settings.task.provenance", {})
    if "record_video" in patched and not patched["record_video"]:
        _drop_video_cameras(args_dict)
        if value is not None:
            logging.info(
                "record_video is off: ignoring %s=%r (from %s)",
                RECORD_CAMERAS_KEY,
                value,
                provenance.get(RECORD_CAMERAS_KEY) or "unknown",
            )
            patched.pop(RECORD_CAMERAS_KEY, None)
            provenance.pop(RECORD_CAMERAS_KEY, None)
        return
    if value is None:
        return
    narrowed, recorded = resolve_record_cameras(
        args_dict.get("camera_groups") or [], value
    )
    if narrowed is not None:
        args_dict["camera_groups"] = narrowed
        _set_legacy_cameras_config(args_dict)
    if recorded is None:
        # ``all`` on cameras without names: same as unset, so the task never sees it
        patched.pop(RECORD_CAMERAS_KEY, None)
        provenance.pop(RECORD_CAMERAS_KEY, None)
        return
    patched[RECORD_CAMERAS_KEY] = recorded
    source = provenance.get(RECORD_CAMERAS_KEY, "")
    logging.info(
        "Recording cameras %s for task %r (from %s)",
        recorded,
        args_dict.get("task", ""),
        source or "unknown",
    )


def _drop_video_cameras(args_dict: dict) -> None:
    """Drop the ``with_video`` camera groups and the legacy camera config of a run
    that records no video; ``record: always`` groups stay."""
    groups = args_dict.get("camera_groups") or []
    kept = [g for g in groups if g.record != "with_video"]
    if len(kept) != len(groups) or args_dict.get("config_file_camera"):
        logging.info(
            "record_video is off: video cameras not started (%s)",
            ", ".join(g.name for g in groups if g.record == "with_video")
            or "config_file_camera",
        )
    args_dict["camera_groups"] = kept
    args_dict["config_file_camera"] = ""
    args_dict["cameras_config"] = None


def _parse_host_flag(value: str) -> tuple[str, str]:
    """Parse ``TYPE`` or ``TYPE:URL`` from --host flag value."""
    parts = value.strip().split(":", 1)
    return parts[0].strip().lower(), (parts[1].strip() if len(parts) > 1 else "")


def _host_unavailable(
    args_dict: dict, session_type: str, url: str | None, reason: str
) -> None:
    """Abort a ``--host`` run that cannot attach, unless ``--force-standalone``.

    Every way ``--host`` can fail (unknown host type, no URL, attach failed) ends
    here, so none of them records standalone into another folder unasked.
    """
    where = f"{session_type} @ {url}" if url else session_type
    if not args_dict.get("force_standalone"):
        raise RuntimeError(
            f"\n\n  --host {where} could not attach:\n"
            f"  {reason}\n\n"
            f"  Session paths cannot be determined: aborting to avoid saving\n"
            f"  data to the wrong location.\n\n"
            f"  Options:\n"
            f"    1. Fix the issue (run oe-remote record first, check URL)\n"
            f"    2. Use --link-to ACQUISITION_NAME to set the path manually\n"
            f"    3. Pass --force-standalone to intentionally run without a host\n"
        )
    logging.warning(
        "--force-standalone: host session (%s) unavailable (%s): "
        "saving to standalone path",
        where,
        reason,
    )


def _resolve_host_session(args_dict: dict) -> None:
    """Attach to a host acquisition session and populate linked_to.

    Reads ``--host TYPE[:URL]`` from args_dict.  URL is optional: if omitted,
    the backend-specific address is read from setup YAML or machine config.

    No-op when ``--host`` is absent.  Does not overwrite ``--link-to`` if already set.
    """
    host_flag = args_dict.get("host_flag", "")
    if not host_flag:
        return

    session_type, url_override = _parse_host_flag(host_flag)
    is_oe = session_type in ("openephys", "open_ephys", "open-ephys")

    # URL resolution: openephys reads from setup YAML or machine config when not in flag
    url = url_override
    url_source = "--host" if url else ""
    if not url and is_oe:
        setup_config = args_dict.get("setup_config")
        if setup_config is not None:
            url = getattr(setup_config, "open_ephys_url", "") or ""
            url_source = "setup yaml open_ephys_url" if url else ""
        if not url:
            from murineshiftwork.logic.machine_config import (
                get_machine_config_path,
                read_open_ephys_url,
            )

            url = read_open_ephys_url()
            url_source = f"machine config {get_machine_config_path()}" if url else ""
        if not url:
            _host_unavailable(
                args_dict,
                session_type,
                url,
                "no URL: pass it as openephys:HOST or set open_ephys_url in the "
                "setup YAML or the machine config",
            )
            return

    if url:
        logging.info("Host %s URL %s (from %s)", session_type, url, url_source)

    from murineshiftwork.hardware.host_session import make_host_session

    kwargs: dict = {"url": url} if url else {}
    if is_oe:
        # attach only to a GUI that is recording, never to one merely acquiring
        kwargs["require_recording"] = True
    try:
        client = make_host_session(session_type, **kwargs)
    except (ValueError, TypeError) as exc:
        _host_unavailable(args_dict, session_type, url, str(exc))
        return

    subject = args_dict.get("subject", "")
    try:
        # the host refuses a recording of another subject
        info = client.attach(subject=subject) if subject else client.attach()
    except TypeError:
        # compat: a host plugin whose attach() takes no subject keyword; drop once
        # every host plugin accepts it
        info = client.attach()

    if info is None:
        reason = getattr(client, "fail_reason", "") or "unknown reason"
        _host_unavailable(args_dict, session_type, url, reason)
        return

    if args_dict.get("linked_to"):
        logging.debug(
            "linked_to already set to %r: --host result discarded",
            args_dict["linked_to"],
        )
        return

    args_dict["linked_to"] = info.session_name
    args_dict["host_session_info"] = info
    logging.info(
        "Host session attached [%s]: session=%r subject=%r",
        info.backend,
        info.session_name,
        info.subject,
    )


def evaluate_args(args_dict=None):
    """Evaluate parsed arguments."""
    args_dict["host_name"] = get_host_name()
    args_dict["host_ip"] = get_host_ip()
    args_dict["original"] = args_dict.copy()

    args_dict = _evaluate_log_level(args_dict=args_dict)
    args_dict = _evaluate_task(args_dict=args_dict)
    args_dict = _evaluate_metadata(args_dict=args_dict)
    setup_logging(
        level=args_dict["log_level"],
        log_file=args_dict["log_file"],
        task=args_dict.get("task", ""),
        subject=args_dict.get("subject", ""),
        setup=args_dict.get("setup", ""),
    )

    args_dict = _evaluate_and_load_configs(args_dict=args_dict)

    task_modes = merge_task_modes(
        read_task_modes(args_dict.get("config_file_task", "")),
        read_task_modes(args_dict.get("config_file_task_overlay", "")),
    )

    if args_dict["command"] == "run":
        subject = args_dict["subject"]
        subject_config = args_dict.get("subject_config")
        if subject_config is None and subject != "_test_subject":
            if args_dict["debug"]:
                args_dict["subject"] = "_test_subject"
                logging.debug("Overwriting subject to _test_subject for debug mode")
            else:
                raise ValueError(
                    f"\n\n\tUnknown subject '{subject}'. Not found in "
                    f"{args_dict['config_dir']}/subjects/. "
                    f"Register first: murineshiftwork subject add -s {subject}\n"
                )
    else:
        raise ValueError(f"Unknown command: '{args_dict['command']}'")

    task_name = args_dict.get("task", "")
    resolved = resolve_task_settings(
        task_name=task_name,
        default_layers=_task_default_layers(args_dict),
        task_modes=task_modes,
        subject_config=args_dict.get("subject_config"),
        subject_label=_subject_label(args_dict),
        task_mode=args_dict.get("task_mode", ""),
        cli_overrides=args_dict.get("task_settings_overrides", []),
        extra_injections=_extra_injections_from_args(args_dict),
        setup_config=args_dict.get("setup_config"),
        setup_label=_setup_label(args_dict),
    )
    patched = resolved.settings
    args_dict["settings.task.patched"] = patched
    args_dict["settings.task.provenance"] = resolved.provenance

    setup_config = args_dict.get("setup_config")
    _resolve_setup_config_ports(args_dict, setup_config, patched)
    _resolve_camera_failure(args_dict, setup_config)
    _resolve_record_cameras(args_dict, patched)
    _resolve_host_session(args_dict)
    # Typed run context (spine refactor) is the single source for the resolved run: build it
    # from args_dict, then DERIVE the ExecutionConfig bundle from it rather than constructing a
    # second overlapping object. execute/TaskProcess migrate onto RunContext in later phases;
    # ExecutionConfig stays until its consumers (TaskProcess, HookContext) do. See
    # docs/plans/PLAN_msw_core_spine_refactor.md.
    run_context = RunContext.from_args_dict(args_dict)
    args_dict["run_context"] = run_context
    args_dict["execution_config"] = run_context.to_execution_config()

    if args_dict.get("command") == "run":
        preflight_hardware_check(args_dict)

    from murineshiftwork.logic.log import json_dumps_type_safe

    logging.debug(
        "settings.task.patched:\n%s",
        json_dumps_type_safe(args_dict.get("settings.task.patched", {})),
    )

    return args_dict
