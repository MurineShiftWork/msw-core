"""Resolved settings of a fixture config tree, compared by value with committed golden files.

``scripts/snapshot_resolved_settings.py`` resolves every task x mode x setup x subject of
``tests/data/settings_snapshot/config`` through ``evaluate_args``; each record must equal its
golden file under ``tests/data/settings_snapshot/golden``. A change to the resolver that changes
any resolved value, provenance label, port, camera group or valve timing fails here.

Regenerate the goldens after an intended change with
``MSW_UPDATE_GOLDENS=1 uv run pytest tests/test_resolved_settings_snapshot.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DATA = Path(__file__).resolve().parent / "data" / "settings_snapshot"
_CONFIG = _DATA / "config"
_GOLDEN = _DATA / "golden"


@pytest.fixture(scope="module")
def snapshot_script():
    path = _ROOT / "scripts" / "snapshot_resolved_settings.py"
    spec = importlib.util.spec_from_file_location("snapshot_resolved_settings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _records(root: Path) -> dict[str, dict]:
    return {
        p.relative_to(root).as_posix(): json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.rglob("*.json"))
    }


@pytest.fixture
def snapshot(tmp_path, fake_tasks, snapshot_script):
    out = tmp_path / "snapshot"
    counts = snapshot_script.snapshot(_CONFIG, out)
    if os.environ.get("MSW_UPDATE_GOLDENS"):
        shutil.rmtree(_GOLDEN, ignore_errors=True)
        shutil.copytree(out, _GOLDEN)
    return counts, _records(out)


def test_every_combination_is_recorded(snapshot):
    counts, records = snapshot
    # 2 setups x 2 subjects x (_test_flush_valves: no mode; demo_sequence: no mode, fast, slow)
    assert len(records) == 16
    assert counts == {"resolved": 14, "error:StickyTaskModeError": 2}
    errors = {k for k, r in records.items() if "error" in r}
    assert errors == {
        "_test_flush_valves/_no_mode/rig_a/subj_b.json",
        "_test_flush_valves/_no_mode/rig_b/subj_b.json",
    }


def test_resolved_settings_match_golden_by_value(snapshot):
    _, records = snapshot
    golden = _records(_GOLDEN)
    assert sorted(records) == sorted(golden)
    for name, record in records.items():
        assert record == golden[name], name


def test_layers_reach_the_record(snapshot):
    """Spot checks that the goldens hold resolved values, not just shapes."""
    _, records = snapshot
    r = records["demo_sequence/fast/rig_b/subj_a.json"]
    # subject beats mode beats overlay; setup sits below the mode
    assert r["settings"]["block"] == {"length": 40, "reversal": True}
    assert r["settings"]["iti"] == 0.75
    assert r["provenance"]["iti"] == "subject:<config_dir>/subjects/subj_a.yaml"
    assert r["settings"]["record_video"] is False
    assert r["run"]["serial_port_bpod"] == "COM3"

    r = records["_test_flush_valves/_no_mode/rig_a/subj_a.json"]
    assert r["settings"]["N_FLUSH_CYCLES"] == 5
    assert r["run"]["serial_port_stage"].endswith("usb-0:0:1.0-port0")
    assert set(r["valve_s_for_ul"]) == {
        f"{port}@{ul}" for port in ("1", "2") for ul in (1.0, 2.0, 5.0)
    }
