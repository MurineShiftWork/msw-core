import logging
import threading
from collections.abc import Callable
from pathlib import Path

from ttl_barcoder.core.barcode_ttl import BarcodeTTL as _BarcodeTTL
from ttl_barcoder.core.config import BarcodeConfig
from ttl_barcoder.hardware.bpod.sender import (
    BARCODE_FIRST_STATE_NAME,
    inject_barcode_states,
)

__all__ = [
    "BarcodeTTL",
    "BarcodeConfig",
    "BARCODE_FIRST_STATE_NAME",
    "inject_barcode_states",
    "barcode_config_from_settings",
    "barcode_record",
    "set_barcode_sink",
    "TTL_IDENTIFIER_SEQUENCES",
    "get_ttl_identifier_sequence",
]

_sink: Callable[[list[dict]], None] | None = None
_sent: list[dict] = []
_lock = threading.Lock()


def barcode_record(config: BarcodeConfig) -> dict:
    """The settings a barcode was sent with, as recorded in session.yaml.

    Everything a decoder needs: value type, bit count and order, init and bit
    lengths, tolerance and the total duration of one barcode.
    """
    return {
        **config.model_dump(mode="json"),
        "bit_order": "lsb_first",
        "framing": "init HIGH-LOW-HIGH, bits, end LOW-HIGH-LOW",
        "total_duration_ms": config.total_duration_ms,
    }


def set_barcode_sink(sink: Callable[[list[dict]], None] | None) -> None:
    """Where the settings of sent barcodes go (the session writer); None to stop.

    The sink gets the list of distinct settings sent so far, each time a new one is
    first used.
    """
    global _sink
    with _lock:
        _sink = sink
        _sent.clear()


def _note_sent(config: BarcodeConfig) -> None:
    record = barcode_record(config)
    with _lock:
        if _sink is None or record in _sent:
            return
        _sent.append(record)
        sink, sent = _sink, list(_sent)
    try:
        sink(sent)
    except Exception:  # noqa: BLE001 - bookkeeping must not stop a barcode
        logging.exception("barcode settings not recorded")


class BarcodeTTL(_BarcodeTTL):
    """``ttl_barcoder.BarcodeTTL`` that records its settings when a barcode is sent.

    ``prepare`` is what tasks call before sending a barcode; the first call with
    each distinct config reports it to the session (``set_barcode_sink``), before
    the wall time is captured.
    """

    def prepare(self) -> tuple[int, float, list[tuple[bool, float]]]:
        """Capture wall time and return (barcode_value, wall_time, timing_sequence).

        The settings are recorded first, so the session file write never sits between
        the captured wall time and the barcode going out.
        """
        _note_sent(self.config)
        return super().prepare()


# Per-task TTL barcode identifier sequences.
# Not all tasks use this; some define sequences in their own task.settings.
TTL_IDENTIFIER_SEQUENCES = {
    "probabilistic_switching": "sssLss",
    "optotagging": "LsLsss",
    "periodic_trigger": "Lsssss",
    "periodic_trigger_with_video": "LLssss",
    "sleep_homecage": "sLsLss",
    "openfield": "sLssss",
    "tests": "ssssss",
}


def get_ttl_identifier_sequence(file=None):
    """Return the TTL barcode identifier sequence for a task file.

    Looks up the task name (derived from the filename stem) in
    ``TTL_IDENTIFIER_SEQUENCES``. Returns ``None`` when the task is not
    registered in that mapping.

    Args:
        file: Path or filename of the task module (e.g. ``"/tasks/probabilistic_switching.py"``).
            Only the stem (name without ``.py``) is used for the lookup.

    Returns:
        A short-form barcode sequence string such as ``"sssLss"``, or ``None``
        if no sequence is registered for this task.
    """
    key = Path(file).name.replace(".py", "")
    return TTL_IDENTIFIER_SEQUENCES.get(key)


def barcode_config_from_settings(settings: dict) -> BarcodeConfig:
    """Build BarcodeConfig from MSW task settings dict, with defaults."""
    return BarcodeConfig(
        barcode_bits=settings.get("barcode_bits", 37),
        bit_duration_ms=settings.get("barcode_bit_duration_ms", 35.0),
        init_duration_ms=settings.get("barcode_init_duration_ms", 10.0),
        tolerance=settings.get("barcode_tolerance", 0.25),
    )
