"""HookContext: shared state passed to every hook method."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookContext:
    """Shared context passed to every hook method.

    Pre-hooks may write to ``task_settings``: changes are seen by the task.
    Post-hooks may read ``output`` populated by task or other post-hooks.
    Both may stash state in ``output`` for other hooks to consume.
    """

    subject: str
    task_name: str
    task_settings: dict
    session_paths: dict
    execution_config: Any | None = None
    output: dict = field(default_factory=dict)
