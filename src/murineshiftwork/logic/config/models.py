from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# Bare Linux serial device names (no /dev/ prefix) accepted in the `port` field.
_BARE_TTY_RE = re.compile(r"tty(ACM|USB|S)\d+")

# ---------------------------------------------------------------------------
# Serial devices


class SerialDevice(BaseModel):
    type: str
    # Set exactly one of:
    #   port         - direct serial port: "COM3" (Windows) or "/dev/ttyACM0"
    #                  (Linux; a bare "ttyACM0"/"ttyUSB0" is also accepted and
    #                  gets the /dev/ prefix). Returned without /dev resolution.
    #   port_by_path - Linux /dev/serial/by-path suffix, resolved at runtime to
    #                  the underlying /dev/ttyXXX. Linux only.
    port: str = ""
    port_by_path: str = ""

    @model_validator(mode="after")
    def _exactly_one_port_source(self) -> SerialDevice:
        if bool(self.port) == bool(self.port_by_path):
            raise ValueError(
                f"{self.type!r} device: set exactly one of 'port' "
                "(e.g. COM3 or /dev/ttyACM0) or 'port_by_path' (Linux by-path "
                "suffix), not both and not neither."
            )
        return self

    def resolve_port(self) -> str:
        """Return the concrete serial port.

        ``port`` is returned verbatim (no filesystem resolution) so Windows COM
        ports work directly; a bare Linux tty name is given its ``/dev/`` prefix.
        ``port_by_path`` is resolved via ``/dev/serial/by-path`` (Linux only);
        on systems without that tree, set ``port`` instead.
        """
        if self.port:
            if _BARE_TTY_RE.fullmatch(self.port):
                return f"/dev/{self.port}"
            return self.port
        p = Path(f"/dev/serial/by-path/{self.port_by_path}")
        if not p.exists():
            raise ValueError(
                f"Serial device not found: /dev/serial/by-path/{self.port_by_path}. "
                "On Windows or other non-Linux systems set 'port' (e.g. COM3) instead."
            )
        return str(p.resolve())


class BpodDevice(SerialDevice):
    type: Literal["bpod"]


class PulsePalDevice(SerialDevice):
    type: Literal["pulsepal"]


class AxisConfig(BaseModel):
    id: int
    position_min: int = 1
    position_max: int = 999
    velocity_max: int = 200
    operating_mode: str = "OP_POSITION"


class StageTowerDevice(SerialDevice):
    type: Literal["stage_tower"]
    baudrate: int = 115200
    timeout: float = 0.1
    axes: dict[str, AxisConfig] = {}
    known_positions: dict[str, dict[str, Any]] = {}


class GenericSerialDevice(SerialDevice):
    type: Literal["serial_generic"]


# Bench scales (and the hx711 bridge) talk 9600 baud; the hardware layer uses the same default.
DEFAULT_SCALE_BAUDRATE = 9600


class ScaleDevice(SerialDevice):
    type: Literal["scale"]
    scale_type: Literal["hx711", "bench"] = "hx711"
    baudrate: int = DEFAULT_SCALE_BAUDRATE
    scale_protocol: int | None = None


DeviceUnion = Annotated[
    BpodDevice | PulsePalDevice | StageTowerDevice | GenericSerialDevice | ScaleDevice,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Calibrations


# Fit-model selector shared by Calibrations and ValveCalibration.
FitModel = Literal["exponential", "linear"]


class ValveCalibration(BaseModel):
    """Bpod valve calibration: list of [open_time_s, delivered_ul] pairs.

    The volume delivered by a solenoid valve as a function of opening time can
    be described by one of two fit models, selected by ``fit_model``:

    - ``"exponential"`` (default): ``volume_ul = a * exp(b * open_time_s) + c``
    - ``"linear"``: ``volume_ul = a * open_time_s + b``

    All lookup methods fit the selected model to the stored points on demand.
    Existing setups that do not set ``fit_model`` keep the exponential model,
    so behaviour is unchanged.
    """

    updated: str = ""
    points: list[list[float]] = []
    fit_model: FitModel = "exponential"

    # Curve fitting lives in logic.calibration.valve_fitting; imported lazily so
    # loading the config models does not pull in scipy.

    def ul_for_s(self, open_s: float) -> float:
        from murineshiftwork.logic.calibration import valve_fitting

        return valve_fitting.ul_for_s(self.points, self.fit_model, open_s)

    def s_for_ul(self, volume_ul: float) -> float:
        from murineshiftwork.logic.calibration import valve_fitting

        return valve_fitting.s_for_ul(self.points, self.fit_model, volume_ul)

    def check_quality(self, r2_threshold: float = 0.95) -> tuple[bool, str]:
        """Return (is_valid, reason); see ``valve_fitting.check_quality``."""
        from murineshiftwork.logic.calibration import valve_fitting

        return valve_fitting.check_quality(self.points, self.fit_model, r2_threshold)


class Calibrations(BaseModel):
    bpod_valve: dict[str, ValveCalibration] = {}
    stale_days: int = 180
    fit_model: FitModel = "exponential"

    @model_validator(mode="after")
    def _propagate_fit_model(self) -> Calibrations:
        """Apply the setup-wide ``fit_model`` to each valve calibration.

        Per-valve ``fit_model`` set explicitly in the YAML is respected; valves
        that did not set it inherit the ``Calibrations.fit_model`` value. Because
        the per-valve default is also ``"exponential"`` this is a no-op for
        existing setups.
        """
        if self.fit_model != "exponential":
            for vc in self.bpod_valve.values():
                if vc.fit_model == "exponential":
                    vc.fit_model = self.fit_model
        return self


# ---------------------------------------------------------------------------
# Camera config
#
# A setup lists camera groups. Each group is driven by one camera backend, a plugin
# registered under the ``msw.camera`` entry point (``flir_bonsai``, ``rce``). Core only
# knows the group's shape; ``settings`` and the extra unit keys belong to the backend,
# which validates them when the session configures it.

#: characters a camera or group name may not hold: ``,`` separates names in
#: ``record_cameras`` and ``:`` marks the ``group:`` and ``role:`` prefixes
_CAMERA_NAME_FORBIDDEN = frozenset(",:")

#: keys of the legacy single camera block that move into the group's ``settings``
_LEGACY_CAMERA_SETTINGS = ("config", "driver", "bonsai_exe", "workflow", "n_cameras")

#: keys only a group has; a mapping without them is the legacy single block
_GROUP_ONLY_KEYS = frozenset({"name", "units", "settings", "role", "record"})

CameraRecordMode = Literal["with_video", "always"]
CameraFailurePolicy = Literal["raise", "continue"]


def _check_camera_name(kind: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError(f"{kind} name is empty")
    bad = sorted(_CAMERA_NAME_FORBIDDEN.intersection(name))
    if bad:
        raise ValueError(f"{kind} name {name!r} may not contain {bad}")
    return name


class CameraUnit(BaseModel):
    """One camera of a group.

    ``name`` is unique across all groups of a setup: it names the camera's files and
    selects it in ``record_cameras``. ``hw_id`` is the hardware identity when the
    backend has one (FLIR serial, Pi hostname). Any other key (such as a FLIR
    ``index``) is passed to the backend unchanged.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    hw_id: str = ""

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _check_camera_name("camera", value)

    @field_validator("hw_id", mode="before")
    @classmethod
    def _hw_id_as_text(cls, value: Any) -> Any:
        # serials are numbers, and YAML reads an unquoted one as an int
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @property
    def unit_settings(self) -> dict[str, Any]:
        """The backend-specific keys of this unit."""
        return dict(self.model_extra or {})


class CameraGroup(BaseModel):
    """Cameras driven together by one backend.

    ``record`` says when the group runs: ``with_video`` groups start when the task
    records video (narrowed by ``record_cameras``), ``always`` groups start in every
    task run. ``role`` is free text naming the view (``eye``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    backend: str
    role: str = ""
    record: CameraRecordMode = "with_video"
    units: list[CameraUnit] = []
    settings: dict[str, Any] = {}
    # The run's camera failure policy, set by the framework on the copy handed to a
    # task (``cameras_config``) for the deprecated camera client. Private so a setup
    # YAML cannot set it; empty means the client's default.
    _failure_policy: str = PrivateAttr(default="")

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _check_camera_name("camera group", value)

    @property
    def failure_policy(self) -> str:
        """The run's camera failure policy carried by this copy, or empty."""
        return self._failure_policy

    def with_failure_policy(self, policy: str) -> CameraGroup:
        """Return a copy carrying the run's camera failure policy."""
        copy = self.model_copy()
        copy._failure_policy = policy
        return copy

    @field_validator("settings", mode="before")
    @classmethod
    def _settings_or_empty(cls, value: Any) -> Any:
        return {} if value is None else value

    @property
    def unit_names(self) -> list[str]:
        return [u.name for u in self.units]


def legacy_camera_group(block: dict[str, Any]) -> dict[str, Any]:
    """Turn the legacy single ``cameras:`` block into one group (as a mapping).

    The group is named after the backend (``rce`` when unset, the old default). The
    flat backend keys move into ``settings`` and the FLIR ``cameras: [{index, name}]``
    list becomes ``units``.

    Unnamed cameras keep their old file names: a FLIR list of only unnamed cameras
    at indices ``0..n-1`` becomes ``n_cameras: n`` with no units, the backend's
    default cameras whose files are labelled ``cam<index>``; an unnamed RCE list,
    which never selected anything, is dropped. Any other unnamed camera is named
    ``cam<index>`` (its files are then labelled ``cam<index>.<index>``).
    """
    backend = str(block.get("backend") or "rce")
    unknown = sorted(
        set(block) - {"backend", "cameras", "selected", *_LEGACY_CAMERA_SETTINGS}
    )
    if unknown:
        raise ValueError(f"cameras: unknown keys {unknown}")
    settings = {k: block[k] for k in _LEGACY_CAMERA_SETTINGS if k in block}
    cameras = [dict(cam or {}) for cam in block.get("cameras") or []]
    if cameras and not any(cam.get("name") for cam in cameras):
        indices = [cam.get("index", i) for i, cam in enumerate(cameras)]
        if backend == "rce":
            cameras = []
        elif backend == "flir_bonsai" and indices == list(range(len(cameras))):
            settings["n_cameras"] = len(cameras)
            cameras = []
    units = []
    for i, cam in enumerate(cameras):
        if not cam.get("name"):
            cam["name"] = f"cam{cam.get('index', i)}"
        units.append(cam)
    return {"name": backend, "backend": backend, "units": units, "settings": settings}


def _check_camera_groups(groups: list[CameraGroup]) -> None:
    def dupes(values: list[str]) -> list[str]:
        return sorted({v for v in values if values.count(v) > 1})

    if bad := dupes([g.name for g in groups]):
        raise ValueError(f"cameras: duplicate group names {bad}")
    if bad := dupes([n for g in groups for n in g.unit_names]):
        raise ValueError(f"cameras: duplicate camera names {bad}")
    # one group per backend: the camera acquisition basename carries the backend's
    # acq_type but no group token, so two groups of one backend would collide
    if bad := dupes([g.backend for g in groups]):
        raise ValueError(f"cameras: more than one group uses backend {bad}")


# ---------------------------------------------------------------------------
# Hook config: lists of dotted import paths for pre/post session hooks


class HooksConfig(BaseModel):
    pre_task: list[str] = []
    post_task: list[str] = []


# ---------------------------------------------------------------------------
# Setup config


class SetupConfig(BaseModel):
    """Hardware configuration for a single behavioural rig.

    Describes the physical devices (Bpod, PulsePal, stage, scale), camera
    backend, valve calibrations, and per-rig hooks for one named setup.
    Loaded from ``{config_dir}/setups/{name}.yaml`` by
    ``load_setup_config``.
    """

    name: str = Field(
        description="Unique rig identifier matching the YAML filename stem."
    )
    devices: dict[str, DeviceUnion] = Field(
        default_factory=dict,
        description=(
            "Named serial devices attached to this rig. "
            "Keys are arbitrary labels (e.g. 'bpod', 'pulsepal'); "
            "values are discriminated-union device models."
        ),
    )
    cameras: list[CameraGroup] = Field(
        default_factory=list,
        description=(
            "Camera groups of this rig, each driven by one camera backend plugin. The "
            "legacy single block (backend, driver, cameras: [{index, name}], config) "
            "loads as one group named after its backend."
        ),
    )
    camera_failure: CameraFailurePolicy = Field(
        default="raise",
        description=(
            "What a camera failing at preflight or start does to the run: 'raise' stops "
            "the cameras and aborts, 'continue' records the failure and runs on. A run "
            "can override it with --camera-failure."
        ),
    )
    calibrations: Calibrations = Calibrations()
    hooks: HooksConfig = HooksConfig()
    open_ephys_url: str = Field(
        default="",
        description=(
            "Base URL of the Open Ephys HTTP server (e.g. 'http://10.0.10.111:37497'). "
            "Leave empty when Open Ephys is not in use on this rig."
        ),
    )
    task_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Mapping of task name to settings overrides applied to every run of that task "
            "on this rig (e.g. record_video for a task only on rigs with cameras for it). "
            "Sits above the task.yaml defaults and the config_dir overlay, below --task-mode, "
            "the subject's task_overrides, -ts and injected values. A task_mode here is "
            "rejected: modes are chosen per run with --task-mode."
        ),
    )

    @field_validator("cameras", mode="before")
    @classmethod
    def _camera_groups(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, dict):
            if _GROUP_ONLY_KEYS.intersection(value):
                return [value]  # one group written without the list
            return [legacy_camera_group(value)]
        return value

    @model_validator(mode="after")
    def _check_cameras(self) -> SetupConfig:
        _check_camera_groups(self.cameras)
        return self

    @field_validator("task_overrides", mode="before")
    @classmethod
    def _check_task_overrides(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(
                "task_overrides must be a mapping of task name to a mapping of settings, "
                f"got {type(value).__name__}"
            )
        for task, overrides in value.items():
            if overrides is None:
                continue
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"task_overrides.{task} must be a mapping of settings, "
                    f"got {type(overrides).__name__}"
                )
            if "task_mode" in overrides:
                raise ValueError(
                    f"task_overrides.{task}.task_mode is not supported: modes are chosen "
                    "per run with --task-mode"
                )
        return {task: dict(overrides or {}) for task, overrides in value.items()}

    def device_port(self, device_name: str) -> str:
        if device_name not in self.devices:
            raise KeyError(f"Device '{device_name}' not in setup '{self.name}'")
        return self.devices[device_name].resolve_port()

    def valve_ul_for_s(self, port: str | int, open_s: float) -> float:
        return self.calibrations.bpod_valve[str(port)].ul_for_s(open_s)

    def valve_s_for_ul(self, port: str | int, volume_ul: float) -> float:
        return self.calibrations.bpod_valve[str(port)].s_for_ul(volume_ul)


# ---------------------------------------------------------------------------
# Subject config

SUBJECT_CONFIG_SCHEMA_VERSION = 2


class SubjectConfig(BaseModel):
    """Per-animal configuration stored as a YAML file under ``subjects/``.

    Captures identity metadata (name, project, experiment) and optional
    per-task parameter overrides that are merged on top of the task's
    default settings at session start.
    """

    schema_version: int = 2
    name: str = Field(
        description="Primary animal identifier; must match the YAML filename stem."
    )
    registered: str = Field(
        default="", description="ISO date (YYYY-MM-DD) when the animal was registered."
    )
    project: str = ""
    experiment: str = ""
    comment: str = ""
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative identifiers for this animal (e.g. cage tag, ear-punch code).",
    )
    task_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Mapping of task name to a flat dict of settings overrides applied "
            "on top of the task defaults for this animal only. Operator intent "
            "(human-owned): forced params, stage position, ... A task_mode here is rejected: "
            "modes are chosen per run with --task-mode."
        ),
    )
    task_state: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Mapping of task name to that task's persisted, machine-written progress "
            "(earned state, distinct from operator overrides). E.g. the sequence task "
            "stores per-(subject, sequence) level here: "
            "task_state['sequence']['sequences'][<sequence_id>] = {'level': N, 'updated': ...}. "
            "Populated by the task's session-end writeback; safe for tooling to rewrite."
        ),
    )


# ---------------------------------------------------------------------------
# Execution config: assembled at session start from setup + subject + task


class ExecutionConfig(BaseModel):
    """Runtime bundle assembled at session start from setup, subject, and task.

    Collects the resolved ``SetupConfig``, ``SubjectConfig``, and merged task
    settings into a single object that is passed through the session pipeline.
    All fields are optional so the object can be built incrementally as each
    config source becomes available.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    setup: SetupConfig | None = None
    subject: SubjectConfig | None = None
    task_name: str = Field(
        default="",
        description="Dotted import path or short name of the task being executed.",
    )
    task_settings: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Fully merged task settings dict: task defaults -> setup overrides "
            "-> task mode -> subject overrides -> CLI overrides, resolved before the "
            "session starts."
        ),
    )


# ---------------------------------------------------------------------------
# Hardware action request: same shape used by Phase 1 CLI and Phase 2 RPC


class ActionRequest(BaseModel):
    """Describes a one-shot hardware action to execute on a named setup device.

    Fields map directly to the Phase 2 FastAPI body so the CLI can slot into
    the same dispatch path without changes when ControllerSession is introduced.
    """

    setup: str
    device: str
    action: str
    params: dict[str, Any] = {}
