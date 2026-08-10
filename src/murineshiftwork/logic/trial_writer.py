"""Backward-compatible re-export: the trial-data writer now lives in msw-io beside the codec.

Kept so existing importers (``from murineshiftwork.logic.trial_writer import ...``) keep working.
New code should import from ``murineshiftwork.io``.
"""

from murineshiftwork.io import JsonlTrialDataWriter, TrialDataWriter

__all__ = ["JsonlTrialDataWriter", "TrialDataWriter"]
