"""`-m/--meta` reaches the acquisition manifest; `--setup` is a run argument, not metadata."""

import logging

from murineshiftwork.namespace.manifest import init_acquisition_manifest
from murineshiftwork.readers.session import _read_metadata

from murineshiftwork.cli.evaluate import _evaluate_metadata
from murineshiftwork.cli.parser import parse_args
from murineshiftwork.logic.task_process import _acquisition_metadata


def _metadata(argv):
    return _evaluate_metadata(parse_args(argv))["metadata"]


def test_meta_keys_and_experimenter_are_collected():
    md = _metadata(
        ["run", "-m", "phase=presleep", "cohort=1", "--meta-experimenter", "ab"]
    )
    assert md == {"phase": "presleep", "cohort": "1", "experimenter": "ab"}


def test_setup_is_not_metadata():
    args = parse_args(["run", "--setup", "rig-a", "-m", "phase=task"])
    assert args["setup"] == "rig-a"
    assert _evaluate_metadata(args)["metadata"] == {"phase": "task"}


def test_framework_blocks_win_over_cli_keys(caplog):
    framework = {"task": {"task": "sequence"}, "reward": {"type": "water"}}
    with caplog.at_level(logging.WARNING):
        md = _acquisition_metadata({"phase": "presleep", "task": "x"}, framework)
    assert md == {"phase": "presleep", **framework}
    assert "reserved" in caplog.text


def test_meta_round_trips_through_the_manifest(tmp_path):
    md = _acquisition_metadata({"phase": "presleep"}, {"task": {"task": "sequence"}})
    init_acquisition_manifest(tmp_path, tmp_path.name, metadata=md)
    meta = _read_metadata(tmp_path)
    assert meta["phase"] == "presleep"
    assert meta["task"] == {"task": "sequence"}
