from __future__ import annotations

import logging
from typing import Any

from murineshiftwork.hardware.manager import ManagedDevice

log = logging.getLogger(__name__)


class PulsePalDevice(ManagedDevice):
    """DeviceProtocol wrapper over ``pypulsepal.PulsePal``.

    ``_open`` creates the PulsePal (which auto-connects in ``__init__``); ``_close`` stops all
    outputs and closes the serial. Lifecycle + serial preflight come from :class:`ManagedDevice`.
    """

    name = "pulsepal"

    def _open(self) -> Any:
        from pypulsepal import PulsePal as _PulsePal

        pulsepal = _PulsePal(serial_port=self._serial_port)
        log.info(
            "PulsePal: connected on %s (firmware v%s)",
            self._serial_port,
            pulsepal.firmware_version,
        )
        return pulsepal

    def _close(self, handle: Any) -> None:
        handle.stop_all_outputs()
        handle._arcom.serial_object.close()
