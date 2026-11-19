"""Barcode settings are recorded in session.yaml when a barcode is first sent."""

from __future__ import annotations

import yaml

from murineshiftwork.logic.barcode import (
    BarcodeConfig,
    BarcodeTTL,
    barcode_record,
    set_barcode_sink,
)
from murineshiftwork.logic.task_process import update_session_yaml


def test_each_distinct_setting_is_reported_once():
    seen: list[list[dict]] = []
    set_barcode_sink(seen.append)
    try:
        slow = BarcodeTTL(BarcodeConfig(bit_duration_ms=100, init_duration_ms=100))
        default = BarcodeTTL(BarcodeConfig())
        for ttl in (slow, slow, default, slow):
            ttl.prepare()
    finally:
        set_barcode_sink(None)
    assert len(seen) == 2
    assert [r["bit_duration_ms"] for r in seen[-1]] == [100.0, 35.0]
    assert seen[-1][0]["total_duration_ms"] == 6 * 100 + 37 * 100


def test_no_sink_records_nothing():
    set_barcode_sink(None)
    BarcodeTTL().prepare()  # no error, nothing to write to


def test_failing_sink_does_not_stop_the_barcode():
    def broken(_):
        raise OSError("disk full")

    set_barcode_sink(broken)
    try:
        value, _, timing = BarcodeTTL().prepare()
    finally:
        set_barcode_sink(None)
    assert timing and isinstance(value, int)


def test_written_to_session_yaml(tmp_path):
    base = tmp_path / "s1__20260101_000000"
    set_barcode_sink(lambda sent: update_session_yaml(str(base), barcodes=sent))
    try:
        BarcodeTTL(BarcodeConfig(bit_duration_ms=100, init_duration_ms=100)).prepare()
    finally:
        set_barcode_sink(None)
    (path,) = tmp_path.glob("*session.yaml")
    data = yaml.safe_load(path.read_text())
    assert data["barcodes"] == [
        barcode_record(BarcodeConfig(bit_duration_ms=100, init_duration_ms=100))
    ]
    assert data["barcodes"][0]["bit_order"] == "lsb_first"


def test_recorded_before_the_wall_time_is_taken(monkeypatch):
    from ttl_barcoder.core.barcode_ttl import BarcodeTTL as Base

    order: list[str] = []
    real = Base.prepare

    def base_prepare(self):
        order.append("wall time")
        return real(self)

    monkeypatch.setattr(Base, "prepare", base_prepare)
    set_barcode_sink(lambda _: order.append("recorded"))
    try:
        BarcodeTTL().prepare()
    finally:
        set_barcode_sink(None)
    assert order == ["recorded", "wall time"]
