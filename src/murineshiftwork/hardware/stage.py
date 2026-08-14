"""DeviceProtocol wrapper for the one-axis stage tower (``one_axis_stage.StageController``).

Wraps the existing driver; the ``name``/``preflight``/``connect``/``disconnect``/``handle``
lifecycle comes from :class:`murineshiftwork.hardware.manager.ManagedDevice`, so this is only the
construction logic: ``_open`` builds the controller via ``StageController.from_config`` (the
driver's recommended entry point), injecting the resolved serial port into the config's
``connection`` block - matching how the task constructs it today.
"""

from __future__ import annotations

import logging
from typing import Any

from murineshiftwork.hardware.manager import ManagedDevice

log = logging.getLogger(__name__)


class SimStage:
    """Hardware-free stand-in for ``StageController``: moves are no-ops and known positions are
    tracked in memory, so a task that homes/moves the stage runs under ``--simulate`` unchanged."""

    def __init__(self) -> None:
        self.known_positions: dict = {}
        self.small_increment = 0
        self.large_increment = 0

    def save_as_known_position(self, name: str) -> None:
        self.known_positions[name] = {"_sim": True}

    def move_to_known_position(self, name: str) -> None:
        log.debug("SimStage: move_to_known_position(%r) [no-op]", name)

    def __repr__(self) -> str:
        return f"SimStage(known_positions={list(self.known_positions)})"


class StageDevice(ManagedDevice):
    """DeviceProtocol implementation wrapping ``one_axis_stage.StageController`` (or
    :class:`SimStage` when ``simulate=True``)."""

    name = "stage"

    def __init__(
        self,
        serial_port: str,
        controller_config: dict | None = None,
        *,
        simulate: bool = False,
    ) -> None:
        super().__init__(serial_port)
        self._config = controller_config or {}
        self._simulate = simulate

    def preflight(self) -> None:
        if self._simulate:
            return  # no serial port to check for a simulated stage
        super().preflight()

    def _open(self) -> Any:
        if self._simulate:
            log.info("Stage: simulated (no hardware)")
            return SimStage()
        from one_axis_stage.controller import StageController

        cfg = dict(self._config)
        cfg["connection"] = {
            **cfg.get("connection", {}),
            "serial_port": self._serial_port,
        }
        stage = StageController.from_config(cfg)
        log.info("Stage: connected on %s", self._serial_port)
        return stage

    def _close(self, handle: Any) -> None:
        if self._simulate:
            return
        handle.api.connection.disconnect()
