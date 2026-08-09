"""The swappable trial-data writer and its behaviour-preserving JSONL implementation."""

from __future__ import annotations

import numpy as np
import pytest
from murineshiftwork.io import save_trial_data

from murineshiftwork.logic.trial_writer import JsonlTrialDataWriter, TrialDataWriter

# A trial dict that exercises the codec's tricky bits: float rounding, numpy arrays,
# tuples (which the codec encodes specially), and nested structure.
_TRIALS = [
    {"trial_index": 0, "outcome": "hit", "p": 1.23456789, "arr": np.array([1, 2, 3])},
    {"trial_index": 1, "outcome": "miss", "block": (0.5, 0.5), "info": {"n": 7}},
    {"trial_index": 2, "outcome": "no_response", "liquid": 0.0},
]


def test_jsonl_writer_is_byte_identical_to_save_trial_data(tmp_path):
    """Per-trial emit through the writer must produce the exact bytes of a single batch save."""
    via_writer = tmp_path / "writer.df.jsonl"
    with JsonlTrialDataWriter(via_writer) as w:
        for trial in _TRIALS:
            w.write_trial(trial)

    via_batch = tmp_path / "batch.df.jsonl"
    save_trial_data(_TRIALS, via_batch)

    assert via_writer.read_bytes() == via_batch.read_bytes()


def test_writer_rewrites_whole_file_each_trial(tmp_path):
    """After trial N the file holds exactly the first N trials (whole-file-rewrite semantics)."""
    path = tmp_path / "df.jsonl"
    writer = JsonlTrialDataWriter(path)
    writer.open()
    for n, trial in enumerate(_TRIALS, start=1):
        writer.write_trial(trial)
        snapshot = tmp_path / f"snap{n}.jsonl"
        save_trial_data(_TRIALS[:n], snapshot)
        assert path.read_bytes() == snapshot.read_bytes()
        assert writer.trial_count == n


def test_trial_count_starts_at_zero(tmp_path):
    """An unused writer reports zero trials - the signal the framework uses to skip finalising."""
    writer = JsonlTrialDataWriter(tmp_path / "df.jsonl")
    assert writer.trial_count == 0


def test_open_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "df.jsonl"
    JsonlTrialDataWriter(nested).open()
    assert nested.parent.is_dir()


def test_is_a_trial_data_writer():
    assert issubclass(JsonlTrialDataWriter, TrialDataWriter)
    with pytest.raises(TypeError):
        TrialDataWriter()  # ABC: cannot instantiate directly
