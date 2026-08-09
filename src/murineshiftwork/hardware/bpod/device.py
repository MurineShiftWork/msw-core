from __future__ import annotations

import logging
from typing import Any

from murineshiftwork.hardware.bpod.factory import BpodFactory
from murineshiftwork.hardware.manager import ManagedDevice

log = logging.getLogger(__name__)


class BpodDevice(ManagedDevice):
    """DeviceProtocol wrapper over ``BpodFactory`` (or ``SimBpod`` when ``simulate=True``).

    ``_open`` creates the factory and calls ``open()`` (with the factory's connect retry);
    ``_close`` calls ``close_safely()`` (idempotent). ``**factory_kwargs`` forwards
    ``connect_retries`` / ``retry_delay_s`` to ``BpodFactory``. With ``simulate=True`` it opens a
    ``SimBpod`` instead and skips the serial-port preflight. Lifecycle comes from
    :class:`ManagedDevice`.
    """

    name = "bpod"

    def __init__(
        self, serial_port: str, *, simulate: bool = False, **factory_kwargs: Any
    ) -> None:
        super().__init__(serial_port)
        self._simulate = simulate
        self._factory_kwargs = factory_kwargs

    def preflight(self) -> None:
        if self._simulate:
            return  # no serial port in simulation
        super().preflight()

    def _open(self) -> Any:
        if self._simulate:
            from murineshiftwork.hardware.bpod.sim import SimBpod

            sim = SimBpod()
            sim.open()
            return sim
        factory = BpodFactory(serial_port=self._serial_port, **self._factory_kwargs)
        factory.open()
        return factory

    def _close(self, handle: Any) -> None:
        handle.close_safely()
