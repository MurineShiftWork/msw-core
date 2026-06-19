from __future__ import annotations

import logging
from typing import Any

from murineshiftwork.logic.misc import serial_port_present

log = logging.getLogger(__name__)


class PulsePalDevice:
    """DeviceProtocol implementation wrapping pypulsepal.PulsePal.

    Satisfies hardware.manager.DeviceProtocol structurally (no inheritance).
    HardwareManager calls preflight → connect → [task] → disconnect.

    preflight: checks the serial port is present (cross-platform).
    connect:   creates PulsePal (which auto-connects in __init__).
    disconnect: stops all outputs and closes serial.
    handle:    returns the PulsePal instance for injection into tasks.
    """

    name = "pulsepal"

    def __init__(self, serial_port: str) -> None:
        self._serial_port = serial_port
        self._pulsepal: Any | None = None

    def preflight(self) -> None:
        log.debug("PulsePal preflight: checking port %s", self._serial_port)
        if not serial_port_present(self._serial_port):
            raise ValueError(
                f"PulsePal serial port not accessible: {self._serial_port!r}"
            )
        log.debug("PulsePal preflight: port present")

    def connect(self) -> None:
        from pypulsepal import PulsePal as _PulsePal

        self._pulsepal = _PulsePal(serial_port=self._serial_port)
        log.info(
            "PulsePal: connected on %s (firmware v%s)",
            self._serial_port,
            self._pulsepal.firmware_version,
        )

    def disconnect(self) -> None:
        log.debug("PulsePal disconnect: stopping outputs")
        if self._pulsepal is not None:
            try:
                self._pulsepal.stop_all_outputs()
                self._pulsepal._arcom.serial_object.close()
            except Exception:
                pass
            self._pulsepal = None

    @property
    def handle(self) -> Any:
        if self._pulsepal is None:
            raise RuntimeError(
                "PulsePalDevice not connected: call connect() before accessing handle"
            )
        return self._pulsepal
