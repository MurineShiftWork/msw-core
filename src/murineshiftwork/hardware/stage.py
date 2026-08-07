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


class StageDevice(ManagedDevice):
    """DeviceProtocol implementation wrapping ``one_axis_stage.StageController``."""

    name = "stage"

    def __init__(self, serial_port: str, controller_config: dict | None = None) -> None:
        super().__init__(serial_port)
        self._config = controller_config or {}

    def _open(self) -> Any:
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
        handle.api.connection.disconnect()
