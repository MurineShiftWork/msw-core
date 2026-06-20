from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import curve_fit as _scipy_curve_fit

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


class ScaleDevice(SerialDevice):
    type: Literal["scale"]
    scale_type: Literal["hx711", "bench"] = "hx711"
    baudrate: int = 9600
    scale_protocol: int | None = None


DeviceUnion = Annotated[
    BpodDevice | PulsePalDevice | StageTowerDevice | GenericSerialDevice | ScaleDevice,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Calibrations


def _exp_model(x: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """a * exp(b * x) + c : the exponential valve volume-vs-time model."""
    return a * np.exp(b * x) + c


def _linear_model(x: np.ndarray, a: float, b: float) -> np.ndarray:
    """a * x + b : the linear valve volume-vs-time model."""
    return a * x + b


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

    # ------------------------------------------------------------------ #
    # Fitting

    def _clean_points(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (open_s, ul) arrays sorted by time with dead-zone points dropped.

        Points with volume <= 0 uL are removed before fitting so they do not
        corrupt the initial-guess calculation or pull the optimizer.
        """
        pts = sorted(self.points, key=lambda p: p[0])
        s = np.array([p[0] for p in pts], dtype=float)
        ul = np.array([p[1] for p in pts], dtype=float)
        mask = ul > 0
        return s[mask], ul[mask]

    def _fit(self) -> tuple[float, ...]:
        """Fit the selected model to stored points.

        Returns (a, b, c) for the exponential model or (a, b) for the linear
        model. At least 3 positive-volume points are required after filtering
        dead-zone measurements.
        """
        s, ul = self._clean_points()

        if len(s) < 3:
            raise ValueError(
                f"Too few positive-volume calibration points ({len(s)}) after "
                "filtering dead-zone measurements: need at least 3."
            )

        if self.fit_model == "linear":
            return self._fit_linear(s, ul)
        return self._fit_exponential(s, ul)

    @staticmethod
    def _fit_exponential(s: np.ndarray, ul: np.ndarray) -> tuple[float, float, float]:
        """Fit a * exp(b * x) + c. Returns (a, b, c).

        Initial guesses are derived from the data so curve_fit converges reliably
        even with sparse calibration sets.
        """
        ul_min, ul_max = float(ul.min()), float(ul.max())
        s_span = float(s.max() - s.min())
        b0 = np.log(ul_max / ul_min) / s_span if s_span > 0 and ul_min > 0 else 5.0
        a0 = ul_min
        c0 = 0.0

        try:
            popt, _ = _scipy_curve_fit(
                _exp_model,
                s,
                ul,
                p0=[a0, b0, c0],
                bounds=([0.0, 0.0, -np.inf], [np.inf, np.inf, np.inf]),
                maxfev=10_000,
            )
            return float(popt[0]), float(popt[1]), float(popt[2])
        except RuntimeError as exc:
            raise ValueError(
                f"Exponential fit failed: check calibration points for valve.\n{exc}"
            ) from exc

    @staticmethod
    def _fit_linear(s: np.ndarray, ul: np.ndarray) -> tuple[float, float]:
        """Fit a * x + b via least squares. Returns (a, b)."""
        a, b = np.polyfit(s, ul, 1)
        return float(a), float(b)

    def _predict(self, s: np.ndarray) -> np.ndarray:
        """Evaluate the fitted model at the given open-time array."""
        params = self._fit()
        if self.fit_model == "linear":
            return _linear_model(s, *params)
        return _exp_model(s, *params)

    def _calibrated_range_ul(self) -> tuple[float, float]:
        """Return (min_ul, max_ul) of the calibrated volume range."""
        pts = sorted(self.points, key=lambda p: p[0])
        ul_endpoints = self._predict(np.array([pts[0][0], pts[-1][0]]))
        return float(ul_endpoints[0]), float(ul_endpoints[-1])

    def ul_for_s(self, open_s: float) -> float:
        pts = sorted(self.points, key=lambda p: p[0])
        s_min, s_max = pts[0][0], pts[-1][0]
        if open_s < s_min or open_s > s_max:
            logging.warning(
                "ValveCalibration.ul_for_s: open_s=%.4f s is outside calibrated "
                "range [%.4f, %.4f] s: extrapolating",
                open_s,
                s_min,
                s_max,
            )
        return float(self._predict(np.array([open_s]))[0])

    def s_for_ul(self, volume_ul: float) -> float:
        """Invert the fitted model numerically via dense sampling.

        Sampling is used rather than an analytical inverse because the analytical
        forms are numerically fragile near degenerate parameters and the same
        code path then works for both the exponential and linear models.

        The sample grid extends 50% beyond the calibrated time range so that
        requests outside the calibrated volume range extrapolate via the fit
        model rather than clamping silently at the boundary.
        """
        pts = sorted(self.points, key=lambda p: p[0])
        s_min, s_max = pts[0][0], pts[-1][0]
        margin = (s_max - s_min) * 0.5
        s_dense = np.linspace(max(0.0, s_min - margin), s_max + margin, 4000)
        ul_dense = self._predict(s_dense)
        ul_lo, ul_hi = float(ul_dense.min()), float(ul_dense.max())
        if volume_ul < ul_lo or volume_ul > ul_hi:
            logging.warning(
                "ValveCalibration.s_for_ul: %.3f uL is outside calibrated "
                "range [%.3f, %.3f] uL: extrapolating",
                volume_ul,
                ul_lo,
                ul_hi,
            )
        return float(np.interp(volume_ul, ul_dense, s_dense))

    def check_quality(self, r2_threshold: float = 0.95) -> tuple[bool, str]:
        """Return (is_valid, reason).

        Checks:
        - At least 3 calibration points
        - All volumes positive
        - Volume monotonically increases with open time
        - Fit R-squared >= r2_threshold
        - Slope is positive (exponential: b > 0; linear: a > 0)
        """
        s, ul = self._clean_points()

        if len(s) < 3:
            return (
                False,
                f"only {len(s)} positive-volume point(s): need at least 3",
            )

        if np.any(np.diff(ul) <= 0):
            bad = int(np.argmax(np.diff(ul) <= 0)) + 1
            return (
                False,
                f"volume not monotonically increasing: point {bad} breaks order",
            )

        try:
            params = self._fit()
        except ValueError as exc:
            return False, str(exc)

        if self.fit_model == "linear":
            slope = params[0]
            if slope <= 0:
                return (
                    False,
                    f"fit slope a = {slope:.4f} <= 0 (curve is not increasing)",
                )
        else:
            b = params[1]
            if b <= 0:
                return (
                    False,
                    f"fit parameter b = {b:.4f} <= 0 (curve is not exponential growth)",
                )

        ul_pred = self._predict(s)
        ss_res = float(np.sum((ul - ul_pred) ** 2))
        ss_tot = float(np.sum((ul - ul.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        if r2 < r2_threshold:
            return (
                False,
                f"R-squared = {r2:.3f} < {r2_threshold} (poor {self.fit_model} fit)",
            )

        params_str = ", ".join(
            f"{name}={val:.5f}" for name, val in zip("abc", params, strict=False)
        )
        return True, f"ok ({self.fit_model}, R-squared = {r2:.3f}, {params_str})"


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

SUBJECT_CONFIG_SCHEMA_VERSION = 1


class SubjectConfig(BaseModel):
    """Per-animal configuration stored as a YAML file under ``subjects/``.

    Captures identity metadata (name, project, experiment) and optional
    per-task parameter overrides that are merged on top of the task's
    default settings at session start.
    """

    schema_version: int = 1
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
            "on top of the task defaults for this animal only."
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
            "Fully merged task settings dict: task defaults -> subject overrides "
            "-> CLI overrides, resolved before the session starts."
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
