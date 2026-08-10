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
        self._workspace_path: str | None = None
        self._session_name: str | None = None

    def set_workspace(self, workspace_path: str, session_name: str) -> None:
        """Set the pybpod workspace (where the SDK writes its own session files).

        Only known once the session folder exists, so it is set after construction and before
        ``connect()`` (the manager opens the device inside TaskProcess, after the folder is made).
        """
        self._workspace_path = workspace_path
        self._session_name = session_name

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
        factory = BpodFactory(
            serial_port=self._serial_port,
            workspace_path=self._workspace_path,
            session_name=self._session_name,
            **self._factory_kwargs,
        )
        factory.open()
        return factory

    def _close(self, handle: Any) -> None:
        handle.close_safely()
