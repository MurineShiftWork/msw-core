from __future__ import annotations

import logging
from typing import Any

from murineshiftwork.hardware.bpod.factory import BpodFactory
from murineshiftwork.hardware.manager import ManagedDevice

log = logging.getLogger(__name__)


class BpodDevice(ManagedDevice):
    """DeviceProtocol wrapper over ``BpodFactory``.

    ``_open`` creates the factory and calls ``open()`` (with the factory's connect retry);
    ``_close`` calls ``close_safely()`` (idempotent). ``**factory_kwargs`` forwards
    ``connect_retries`` / ``retry_delay_s`` to ``BpodFactory``. Lifecycle + serial preflight come
    from :class:`ManagedDevice`.
    """

    name = "bpod"

    def __init__(self, serial_port: str, **factory_kwargs: Any) -> None:
        super().__init__(serial_port)
        self._factory_kwargs = factory_kwargs

    def _open(self) -> BpodFactory:
        factory = BpodFactory(serial_port=self._serial_port, **self._factory_kwargs)
        factory.open()
        return factory

    def _close(self, handle: BpodFactory) -> None:
        handle.close_safely()
