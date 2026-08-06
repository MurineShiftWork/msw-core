"""Typed run context - the object that will replace the untyped ``args_dict`` bag.

The CLI spine currently threads one mutable ``dict`` through parse -> evaluate -> execute ->
TaskProcess (~18 functions, ~96 read-sites), with magic string keys and no type safety. This
module introduces a typed ``RunContext`` that carries the resolved run as an object.

Phase 1 (proof-of-concept) is **dual-carrier**: ``evaluate_args`` builds a ``RunContext`` from the
already-populated ``args_dict`` and stashes it alongside the dict - nothing downstream reads it
yet. Later phases migrate ``execute``/``TaskProcess`` to read the typed fields and finally retire
the dict, keeping the msw-tasks-lab task boundary frozen via ``to_task_kwargs()``. See
``docs/plans/PLAN_msw_core_spine_refactor.md``.

Unlike ``ExecutionConfig`` (which it will absorb), this needs no ``arbitrary_types_allowed``: its
only non-scalar fields are pydantic models (``SetupConfig``/``SubjectConfig``) and a plain
``task_settings`` dict, so it validates cleanly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from murineshiftwork.logic.config import SetupConfig, SubjectConfig


class DevicePorts(BaseModel):
    """Resolved serial ports per device - replaces the ``serial_port_{type}`` magic dict keys."""

    bpod: str = ""
    stage: str = ""
    scale: str = ""
    pulsepal: str = ""


class RunContext(BaseModel):
    """Typed, resolved description of one run (subject + task + setup + ports + settings).

    Built by ``evaluate_args`` from the resolved ``args_dict``. In Phase 1 it is informational
    (dual-carrier); it becomes the carrier as ``execute``/``TaskProcess`` migrate onto it.
    """

    command: str = ""
    subject: str = ""
    task_name: str = ""
    config_dir: str = ""
    out_path: str = ""
    debug: bool = False
    simulate: bool = False
    ports: DevicePorts = Field(default_factory=DevicePorts)
    setup: SetupConfig | None = None
    subject_config: SubjectConfig | None = None
    task_settings: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_args_dict(cls, d: dict) -> RunContext:
        """Build a RunContext from a resolved ``args_dict`` (the parse->evaluate boundary).

        Reads the well-known keys; missing keys fall back to the field defaults so this never
        raises on a partially-built dict.
        """
        return cls(
            command=d.get("command", "") or "",
            subject=d.get("subject", "") or "",
            task_name=d.get("task_name") or d.get("task", "") or "",
            config_dir=d.get("config_dir", "") or "",
            out_path=d.get("out_path", "") or "",
            debug=bool(d.get("debug", False)),
            simulate=bool(d.get("simulate", False)),
            ports=DevicePorts(
                bpod=d.get("serial_port_bpod", "") or "",
                stage=d.get("serial_port_stage", "") or "",
                scale=d.get("serial_port_scale", "") or "",
                pulsepal=d.get("serial_port_pulsepal", "") or "",
            ),
            setup=d.get("setup_config"),
            subject_config=d.get("subject_config"),
            task_settings=d.get("settings.task.patched") or {},
        )

    def to_task_kwargs(self) -> dict:
        """Project the fields this context owns back to their ``args_dict`` keys.

        The frozen boundary shape for the migrated subset - as later phases move fields onto
        RunContext, they are emitted here so the msw-tasks-lab task contract never changes.
        """
        return {
            "command": self.command,
            "subject": self.subject,
            "task_name": self.task_name,
            "config_dir": self.config_dir,
            "out_path": self.out_path,
            "debug": self.debug,
            "simulate": self.simulate,
            "serial_port_bpod": self.ports.bpod,
            "serial_port_stage": self.ports.stage,
            "serial_port_scale": self.ports.scale,
            "serial_port_pulsepal": self.ports.pulsepal,
        }
