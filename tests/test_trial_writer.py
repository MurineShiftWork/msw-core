"""The trial-data writer moved to msw-io; msw-core keeps a backward-compatible re-export.

The classes and their byte-identity behaviour are tested in msw-io. Here we only verify the
`murineshiftwork.logic.trial_writer` shim still re-exports the same objects, so existing importers
(TaskProcess, downstream task tests) keep working.
"""

from __future__ import annotations


def test_logic_shim_reexports_the_io_classes():
    from murineshiftwork import io
    from murineshiftwork.logic import trial_writer

    assert trial_writer.TrialDataWriter is io.TrialDataWriter
    assert trial_writer.JsonlTrialDataWriter is io.JsonlTrialDataWriter
