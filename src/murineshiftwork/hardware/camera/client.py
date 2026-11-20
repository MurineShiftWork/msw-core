"""Deprecated camera client: the old per-task method set over a ``CameraSession``.

Tasks written before ``CameraSession`` drive the camera themselves::

    conductor = make_camera_client(args_dict.get("cameras_config"), ...)
    conductor.start()
    conductor.setup_agents()
    conductor.initialize_acquisition(
        acquisition_path=..., acquisition_name=..., acqdir=..., basename=...
    )
    conductor.start_preview()
    conductor.start_recording()
    ...
    conductor.stop_acquisition()
    conductor.stop()

``make_camera_client`` keeps that working on one camera group (``cameras_config``,
the first group recording with video) through a ``CameraSession`` under the run's
camera failure policy (carried on ``cameras_config``, ``raise`` by default). Groups
with ``record: always`` and further video groups are not started: move tasks to
``CameraSession.from_run``, which starts every group.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from msw_plugin_api.camera import CameraConfigError, CameraTarget

from murineshiftwork.hardware.camera.session import POLICY_RAISE, CameraSession

if TYPE_CHECKING:
    from murineshiftwork.logic.config.models import CameraGroup

log = logging.getLogger(__name__)


class LegacyCameraClient:
    """The old camera client method set, driving one group through a session."""

    def __init__(self, group: CameraGroup, policy: str = POLICY_RAISE) -> None:
        self.group = group
        self.session = CameraSession([group], policy=policy)
        self._target: CameraTarget | None = None
        self._host_folder = ""

    @property
    def acq_type(self) -> str:
        """Acquisition type of the group's backend (``video_flir``, ``video_rce``)."""
        return self.session.acq_type(self.group.name)

    def start(self) -> None:
        self.session.open()

    def setup_agents(self) -> None:
        """Agents are reached in ``start`` (the backend's preflight)."""

    def initialize_acquisition(
        self,
        acquisition_path: str = "",
        acquisition_name: str = "",
        acqdir: str = "",
        basename: str = "",
        **_: Any,
    ) -> None:
        self._target = CameraTarget(
            acqdir=acqdir,
            basename=basename,
            host_relpath=acquisition_path,
            host_name=acquisition_name,
        )
        # the behaviour acquisition sits next to the camera one in the session container
        if acqdir and acquisition_name:
            host = Path(acqdir).parent / acquisition_name
            self._host_folder = str(host) if host.is_dir() else ""

    def start_preview(self) -> None:
        """Backends preview as part of ``start``."""

    def start_recording(self) -> None:
        if self._target is None:
            raise RuntimeError(
                "initialize_acquisition must come before start_recording"
            )
        self.session.start_targets(
            {self.group.name: self._target}, host_folder=self._host_folder
        )

    def stop_acquisition(self) -> None:
        self.session.stop("stopped")

    def stop(self) -> None:
        self.session.stop("stopped")


def make_camera_client(
    cameras_config: CameraGroup | list[CameraGroup] | None,
    config_file_camera: str = "",
    output_dir: str = "",
    camera_failure: str = "",
) -> LegacyCameraClient | None:
    """Return a client for one camera group, or None when there is none.

    Deprecated: use ``CameraSession.from_run``.

    Args:
        cameras_config: The run's ``cameras_config`` (one group), or a list of
            groups of which the first is used.
        config_file_camera: RCE ensemble YAML used when there is no group, or the
            RCE group has no ``settings.config``.
        output_dir: Default ``data_dir`` of an RCE group: where the conductor
            saves each acquisition's ensemble and conductor YAML.
        camera_failure: ``raise`` or ``continue``; defaults to the policy carried
            by ``cameras_config``, else ``raise``.

    Raises:
        CameraConfigError: An RCE group's ensemble YAML does not exist.
    """
    warnings.warn(
        "make_camera_client is deprecated; use "
        "murineshiftwork.hardware.camera.CameraSession.from_run",
        DeprecationWarning,
        stacklevel=2,
    )
    from murineshiftwork.logic.config.models import CameraGroup

    group: CameraGroup | None
    if isinstance(cameras_config, list | tuple):
        if len(cameras_config) > 1:
            log.warning(
                "Camera: make_camera_client drives only group %r of %s",
                cameras_config[0].name,
                [g.name for g in cameras_config],
            )
        group = cameras_config[0] if cameras_config else None
    else:
        group = cameras_config
    cli_exists = bool(config_file_camera) and Path(config_file_camera).exists()
    if group is None:
        if not cli_exists:
            return None
        log.debug(f"Camera: using RCE config from CLI arg: {config_file_camera}")
        group = CameraGroup(
            name="rce", backend="rce", settings={"config": config_file_camera}
        )
    if group.backend == "rce":
        settings = dict(group.settings)
        cfg_file = str(settings.get("config") or "")
        if not (cfg_file and Path(cfg_file).exists()):
            if not cli_exists:
                raise CameraConfigError(
                    f"camera group {group.name!r}: RCE ensemble config not found: "
                    f"{cfg_file or config_file_camera or '(none set)'!r}"
                )
            settings["config"] = config_file_camera
        if output_dir:
            settings.setdefault("data_dir", output_dir)
        group = group.model_copy(update={"settings": settings})
    policy = camera_failure or group.failure_policy or POLICY_RAISE
    return LegacyCameraClient(group, policy=policy)
