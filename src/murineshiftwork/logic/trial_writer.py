"""Swappable trial-data writer: the framework owns the durable write, the task emits dicts.

``TrialDataWriter`` is the seam the task-isolation work writes through - a task will call
``emit_trial(dict)`` and the framework persists it via the injected writer, instead of the task
calling ``save_trial_data`` itself. ``JsonlTrialDataWriter`` is the first, behaviour-preserving
implementation: it keeps the accumulated trials and rewrites the JSONL through the unchanged msw-io
``save_trial_data`` on every write, so the bytes on disk are identical to today's per-trial
``save()`` loop. A future append-mode writer can be swapped in without touching tasks.

(The ABC belongs in msw-io alongside the codec long term; it is kept here while the ``emit_trial``
wiring that consumes it lands, to avoid a cross-package release for an as-yet-unused class.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TrialDataWriter(ABC):
    """Owns the durable on-disk write of a session's trial data.

    One instance per acquisition. The framework constructs it from the acquisition's trial-data
    path and feeds it scored trial dicts; the task never touches the file. Implementations decide
    the on-disk format. Usable as a context manager (``with writer:``).
    """

    @abstractmethod
    def open(self) -> None:
        """Prepare the destination (create dirs, write any header)."""

    @abstractmethod
    def write_trial(self, trial: dict) -> None:
        """Persist one scored trial dict. Must be durable on return."""

    @abstractmethod
    def write_all(self, trials: list[dict]) -> None:
        """Persist the given full trial list, replacing any prior contents.

        The whole-list counterpart to ``write_trial`` for a task that keeps its own accumulated
        list and rewrites it each trial. Durable on return.
        """

    @abstractmethod
    def close(self) -> None:
        """Flush and finalise. Idempotent."""

    def __enter__(self) -> TrialDataWriter:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class JsonlTrialDataWriter(TrialDataWriter):
    """JSONL writer that matches ``save_trial_data`` byte-for-byte.

    Preserves today's whole-file-rewrite semantics: it keeps an in-memory list of accepted trials
    and rewrites the file on each ``write_trial`` via the unchanged ``save_trial_data``, so the
    on-disk output is identical to the current per-trial ``save()`` loop.
    """

    def __init__(self, filepath: str | Path) -> None:
        self._filepath = Path(filepath)
        self._trials: list[dict] = []

    @property
    def trial_count(self) -> int:
        """How many trials have been written (0 => the writer was never used)."""
        return len(self._trials)

    def open(self) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)

    def write_trial(self, trial: dict) -> None:
        from murineshiftwork.io import save_trial_data

        self._trials.append(trial)
        save_trial_data(self._trials, self._filepath)  # whole-file rewrite

    def write_all(self, trials: list[dict]) -> None:
        from murineshiftwork.io import save_trial_data

        self._trials = list(trials)
        save_trial_data(self._trials, self._filepath)  # whole-file rewrite

    def close(self) -> None:
        from murineshiftwork.io import save_trial_data

        save_trial_data(self._trials, self._filepath)
