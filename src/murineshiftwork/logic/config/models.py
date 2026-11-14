from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class CameraUnit(BaseModel):
    """Per-camera specification for the FLIR/Bonsai backend."""

    index: int  # SDK enumeration index; run `msw flir list-cameras` to resolve
    name: str = ""  # human label, e.g. "top" or "front"; used in artifact filenames
    # Camera serial number (`msw flir probe-cameras`). When set, the camera is
    # found by serial before Bonsai starts and `index` is only a fallback.
    serial: str = ""

    @field_validator("serial", mode="before")
    @classmethod
    def _serial_as_str(cls, v: Any) -> Any:
        # an unquoted YAML serial loads as int, which pydantic v2 rejects for str
        return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


class CameraConfig(BaseModel):
    backend: str = "rce"  # "rce" | "flir_bonsai"
    config: str = ""  # RCE only: path to ensemble YAML
    # FLIR/Bonsai-specific (ignored when backend="rce")
    driver: str = "flycap"  # "flycap" | "spinnaker"
    bonsai_exe: str = ""  # path to Bonsai.exe; falls back to BONSAI_EXE env var
    workflow: str = ""  # workflow stem; auto-derived as run-flir-{driver}-1cam if empty
    cameras: list[CameraUnit] = []  # preferred; one entry per camera
    n_cameras: int = 1  # flat shorthand when cameras list is empty


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
    cameras: CameraConfig | None = None
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
