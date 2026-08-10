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

from murineshiftwork.logic.config import ExecutionConfig, SetupConfig, SubjectConfig


class DevicePorts(BaseModel):
    """Resolved serial ports per device - replaces the ``serial_port_{type}`` magic dict keys."""

    bpod: str = ""
    stage: str = ""
    scale: str = ""
    pulsepal: str = ""

    def get(self, device: str) -> str:
        """Port for *device*, or ``""`` if there is no such field.

        Mirrors the old ``args_dict.get(f"serial_port_{device}", "")`` lookup so a device whose
        type has no dedicated port field degrades to empty exactly as before.
        """
        return getattr(self, device) if device in type(self).model_fields else ""


class RunContext(BaseModel):
    """Typed, resolved description of one run (subject + task + setup + ports + settings).

    Built by ``evaluate_args`` from the resolved ``args_dict``. In Phase 1 it is informational
    (dual-carrier); it becomes the carrier as ``execute``/``TaskProcess`` migrate onto it.
    """

    command: str = ""
    subject: str = ""
    task_name: str = ""
    # the setup NAME string (args_dict["setup"]); distinct from the `setup` SetupConfig below
    setup_name: str = ""
    config_dir: str = ""
    out_path: str = ""
    debug: bool = False
    simulate: bool = False
    session_type: str = ""
    # acquisition SYSTEM; "msw" for behaviour tasks (matches evaluate's default)
    acq_type: str = "msw"
    # task.yaml version; None -> callers default to 1
    session_version: int | None = None
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
            setup_name=d.get("setup", "") or "",
            config_dir=d.get("config_dir", "") or "",
            out_path=d.get("out_path", "") or "",
            debug=bool(d.get("debug", False)),
            simulate=bool(d.get("simulate", False)),
            session_type=d.get("session_type", "") or "",
            acq_type=d.get("acq_type") or "msw",
            session_version=d.get("session_version"),
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
            # the boundary/constructor key is "task" (tasks + TaskProcess read args_dict["task"]);
            # "task_name" is derived downstream by TaskProcess, not part of the input contract.
            "task": self.task_name,
            "setup": self.setup_name,
            "config_dir": self.config_dir,
            "out_path": self.out_path,
            "debug": self.debug,
            "simulate": self.simulate,
            "session_type": self.session_type,
            "acq_type": self.acq_type,
            "session_version": self.session_version,
            "serial_port_bpod": self.ports.bpod,
            "serial_port_stage": self.ports.stage,
            "serial_port_scale": self.ports.scale,
            "serial_port_pulsepal": self.ports.pulsepal,
        }

    def to_execution_config(self) -> ExecutionConfig:
        """Derive the ``ExecutionConfig`` bundle from this context.

        ``ExecutionConfig`` is the resolved setup/subject/task bundle that ``TaskProcess`` and the
        ``HookContext`` still consume. RunContext already carries those fields, so it is now the
        single source and ExecutionConfig is a projection of it rather than an independently-built
        object (evaluate_args no longer constructs one directly). It stays until those consumers
        migrate to reading RunContext, then it can be retired.
        """
        return ExecutionConfig(
            setup=self.setup,
            subject=self.subject_config,
            task_name=self.task_name,
            task_settings=self.task_settings,
        )
