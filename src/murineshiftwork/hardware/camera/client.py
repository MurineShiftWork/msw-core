"""Camera client factory: backend-agnostic interface for session recording.

Both RceConductorAdapter and FlirBonsaiClient expose the same method set so
task code never branches on camera backend:

    conductor = make_camera_client(cameras_config, output_dir)
    if conductor:
        conductor.start()
        conductor.setup_agents()
        # ... after TaskProcess builds session paths:
        conductor.initialize_acquisition(acquisition_path=..., acquisition_name=...)
        conductor.start_preview()
        conductor.start_recording()
        ...
        conductor.stop_acquisition()
        conductor.stop()

Backends
--------
rce         : rpi_camera_ensemble.Conductor: RPi camera colony.  Lazy import.
flir_bonsai : BonsaiCameraRunner/MultiCameraRunner from msw_flir_bonsai.  Lazy import.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RCE adapter


class RceConductorAdapter:
    """Wrap rpi_camera_ensemble.Conductor with the shared camera client API.

    All rpi_camera_ensemble imports are deferred until start() is called so
    that the package is not required on machines that use FLIR cameras only.
    """

    def __init__(self, ensemble_cfg_file: str, output_dir: str) -> None:
        self._ensemble_cfg_file = ensemble_cfg_file
        self._output_dir = output_dir
        self._conductor: Any = None

    def start(self) -> None:
        from rpi_camera_ensemble.conductor.conductor import Conductor
        from rpi_camera_ensemble.config.acquisition import EnsembleAcquisitionConfig
        from rpi_camera_ensemble.config.conductor import ConductorConfig

        from murineshiftwork.logic.log import suppress_third_party_console_handlers

        ensemble_cfg = EnsembleAcquisitionConfig.from_yaml(path=self._ensemble_cfg_file)
        conductor_cfg = ConductorConfig(data_dir=self._output_dir)
        self._conductor = Conductor(config=conductor_cfg, ensemble_config=ensemble_cfg)
        suppress_third_party_console_handlers()
        self._conductor.start()
        suppress_third_party_console_handlers()

    def setup_agents(self) -> None:
        if self._conductor is not None:
            self._conductor.setup_agents()

    def initialize_acquisition(
        self, acquisition_path: str = "", acquisition_name: str = "", **_: Any
    ) -> None:
        if self._conductor is not None:
            self._conductor.initialize_acquisition(
                acquisition_path=acquisition_path,
                acquisition_name=acquisition_name,
            )

    def start_preview(self) -> None:
        if self._conductor is not None:
            self._conductor.start_preview()

    def start_recording(self) -> None:
        if self._conductor is not None:
            self._conductor.start_recording()

    def stop_acquisition(self) -> None:
        if self._conductor is not None:
            self._conductor.stop_acquisition()

    def stop(self) -> None:
        if self._conductor is not None:
            try:
                self._conductor.stop()
            except Exception:
                log.warning(
                    "RceConductorAdapter.stop() did not complete cleanly", exc_info=True
                )
            self._conductor = None


# ---------------------------------------------------------------------------
# FLIR/Bonsai client


def _derive_video_flir_dir(
    output_dir: str, acquisition_path: str, acquisition_name: str
) -> tuple[str, str] | None:
    """Derive a separate video_flir acquisition dir from the MSW acquisition path.

    Returns (container_dir, video_flir_basename) or None if the basename does
    not have the expected __-separated structure with a datetime component.

    The returned video_flir_basename is placed as a sibling of the MSW
    acquisition dir inside the session container:

        output_dir/
          subject/
            subject__dt/                    <- session container
              subject__dt__msw/             <- MSW acquisition (input)
              subject__dt__video_flir/      <- returned sibling dir
    """
    if not acquisition_path or not acquisition_name:
        return None
    parts = acquisition_name.split("__")
    if len(parts) < 2:
        return None
    subject, dt = parts[0], parts[1]
    video_basename = f"{subject}__{dt}__video_flir"
    container_abs = str(Path(output_dir) / Path(acquisition_path).parent)
    return container_abs, video_basename


class FlirBonsaiClient:
    """Camera client for FLIR cameras driven by Bonsai subprocesses.

    Bonsai acquisition is started/stopped as a group of independent subprocesses
    (one per camera) via msw_flir_bonsai.MultiCameraRunner.  All msw_flir_bonsai
    imports are deferred so the package is not required on machines using RCE cameras.

    v4 namespace: video is written to a dedicated ``video_flir`` acquisition
    directory that is a sibling of the MSW acquisition directory inside the
    session container.  The Bonsai workflow Python transform detects the
    pre-built datetime in the session name and skips creating an extra
    timestamp subdirectory.

    Recording lifecycle:
        start()                → no-op (Bonsai starts with recording: no preview mode)
        setup_agents()         → no-op
        initialize_acquisition → buffers output path and derives video_flir dir
        start_preview()        → no-op
        start_recording()      → creates MultiCameraRunner and starts subprocesses
        stop_acquisition()     → stops subprocesses and waits for clean exit
        stop()                 → cleanup; safe to call even if never started
    """

    def __init__(self, config: CameraConfig, output_dir: str) -> None:
        self._config = config
        self._output_dir = output_dir
        self._acq_name: str = "session"
        self._acq_path: str = ""
        self._video_container: str = ""
        self._video_basename: str = ""
        self._runner: Any = None

    def start(self) -> None:
        pass

    def setup_agents(self) -> None:
        pass

    def initialize_acquisition(
        self, acquisition_path: str = "", acquisition_name: str = "", **_: Any
    ) -> None:
        self._acq_path = acquisition_path
        self._acq_name = acquisition_name or (
            acquisition_path.split("/")[-1] if acquisition_path else "session"
        )
        derived = _derive_video_flir_dir(
            self._output_dir, acquisition_path, self._acq_name
        )
        if derived:
            self._video_container, self._video_basename = derived
            log.debug(
                f"FlirBonsaiClient: video_flir dir: "
                f"{self._video_container}/{self._video_basename}"
            )

    def start_preview(self) -> None:
        pass

    def start_recording(self) -> None:
        from msw_flir_bonsai.runner import BonsaiCameraRunner, MultiCameraRunner

        cfg = self._config
        workflow = cfg.workflow or f"run-flir-{cfg.driver}-1cam"
        bonsai_exe = cfg.bonsai_exe or None

        indices = (
            [cam.index for cam in cfg.cameras]
            if cfg.cameras
            else list(range(cfg.n_cameras))
        )

        if self._video_container and self._video_basename:
            bonsai_basepath = self._video_container
            bonsai_session = self._video_basename
        else:
            bonsai_basepath = self._output_dir
            bonsai_session = self._acq_name

        runners = [
            BonsaiCameraRunner(
                workflow=workflow,
                output_dir=bonsai_basepath,
                session=f"{bonsai_session}__cam{idx}",
                cam_index=idx,
                driver=cfg.driver,
                bonsai_exe=bonsai_exe,
            )
            for idx in indices
        ]

        self._runner = MultiCameraRunner(runners)
        log.info(
            f"FlirBonsaiClient: starting {len(indices)} camera(s) "
            f"driver={cfg.driver} indices={indices} session={bonsai_session!r}"
        )
        self._runner.start()
        self._write_flir_meta(indices, cfg)

    def _write_flir_meta(self, indices: list[int], cfg: CameraConfig) -> None:
        """Write .flir.meta.yaml, merging per-camera meta written by Bonsai at startup.

        Bonsai writes {video_dir}/{session}__cam{index}__meta.yaml for each camera.
        This method polls for those files for up to 10 s then writes the combined
        session-level sidecar.
        """
        import time

        import yaml

        if self._video_container and self._video_basename:
            meta_poll_dir = Path(self._video_container) / self._video_basename
            sidecar_name = self._video_basename
        else:
            meta_poll_dir = Path(self._output_dir)
            sidecar_name = self._acq_name

        cam_metas: dict[int, dict[str, Any]] = {}
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(cam_metas) < len(indices):
            for idx in indices:
                if idx not in cam_metas:
                    p = meta_poll_dir / f"{sidecar_name}__cam{idx}__meta.yaml"
                    if p.exists():
                        try:
                            cam_metas[idx] = yaml.safe_load(p.read_text()) or {}
                        except Exception:
                            cam_metas[idx] = {}
            if len(cam_metas) < len(indices):
                time.sleep(0.5)

        if len(cam_metas) < len(indices):
            missing = [i for i in indices if i not in cam_metas]
            log.warning(
                f"FlirBonsaiClient: cam_meta not found for indices {missing} "
                "after 10 s: serial will be absent from sidecar."
            )

        cams_meta = [
            {
                "cam_index": idx,
                "bonsai_session": f"{sidecar_name}__cam{idx}",
                **cam_metas.get(idx, {}),
            }
            for idx in indices
        ]

        meta: dict[str, Any] = {
            "flir_acq_format_version": 1,
            "session": sidecar_name,
            "datetime": datetime.datetime.now().isoformat(timespec="seconds"),
            "driver": cfg.driver,
            "workflow": cfg.workflow or f"run-flir-{cfg.driver}-1cam",
            "bonsai_exe": cfg.bonsai_exe or os.environ.get("BONSAI_EXE", ""),
            "cameras": cams_meta,
        }

        out_dir = meta_poll_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        sidecar = out_dir / f"{sidecar_name}.flir.meta.yaml"

        with sidecar.open("w") as fh:
            yaml.dump(meta, fh, default_flow_style=False, sort_keys=False)

        log.info(f"FlirBonsaiClient: wrote FLIR metadata to {sidecar}")

    def stop_acquisition(self) -> None:
        if self._runner is None:
            return
        self._runner.stop(timeout=5.0)
        for runner in self._runner._runners:
            rc = runner.wait(timeout=10.0)
            if rc is None:
                log.warning(
                    f"FlirBonsaiClient: cam{runner._cam_index} did not exit cleanly"
                )

    def stop(self) -> None:
        if self._runner is not None and self._runner.any_running:
            self._runner.stop(timeout=5.0)
        self._runner = None


# ---------------------------------------------------------------------------
# Factory


def make_camera_client(
    cameras_config: CameraConfig | None,
    config_file_camera: str = "",
    output_dir: str = "",
) -> RceConductorAdapter | FlirBonsaiClient | None:
    """Return a camera client for the given config, or None if no camera is configured.

    Args:
        cameras_config: SetupConfig.cameras object (may be None).
        config_file_camera: Legacy RCE config file path (used when cameras_config is None
            or cameras_config.backend == "rce" without a .config path).
        output_dir: Base data directory passed to the camera backend.
    """
    if cameras_config is None:
        if config_file_camera and Path(config_file_camera).exists():
            log.debug(f"Camera: using RCE config from CLI arg: {config_file_camera}")
            return RceConductorAdapter(config_file_camera, output_dir)
        return None

    if cameras_config.backend == "flir_bonsai":
        log.debug(f"Camera: FLIR/Bonsai backend, {cameras_config.n_cameras} cam(s)")
        return FlirBonsaiClient(cameras_config, output_dir)

    if cameras_config.backend == "rce":
        cfg_file = cameras_config.config or config_file_camera
        if cfg_file and Path(cfg_file).exists():
            log.debug(f"Camera: RCE backend, config={cfg_file}")
            return RceConductorAdapter(cfg_file, output_dir)
        log.info(
            "Camera: RCE backend selected but no config file found: running without video"
        )
        return None

    log.warning(f"Camera: unknown backend {cameras_config.backend!r}: skipping")
    return None
