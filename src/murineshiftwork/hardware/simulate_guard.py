"""Refuse real hardware in a ``--simulate`` run.

Simulation must never reach a device: a task that builds its own connection (rather than
using the injected stand-ins) would otherwise drive the real Bpod, stage or PulsePal on the
rig it runs on. ``run_task`` marks the run as simulated; every place that opens a real device
calls :func:`refuse_real_hardware` first and fails loudly instead.

The mark is an environment variable, not a module flag, so it reaches the task's child
processes (``multiprocessing`` spawns a fresh interpreter on Windows).
"""

from __future__ import annotations

import os

_ENV = "MSW_SIMULATE"


class SimulatedHardwareError(RuntimeError):
    """A real device was about to be opened in a ``--simulate`` run."""


def mark_simulated(simulate: bool) -> None:
    """Mark this process (and the processes it starts) as a simulated run, or clear the mark."""
    if simulate:
        os.environ[_ENV] = "1"
    else:
        os.environ.pop(_ENV, None)


def is_simulated() -> bool:
    """True when the current run is marked as simulated."""
    return os.environ.get(_ENV) == "1"


def refuse_real_hardware(device: str, port: str = "") -> None:
    """Raise if a real ``device`` is about to be opened in a simulated run.

    Raises:
        SimulatedHardwareError: The run is marked as simulated.
    """
    if is_simulated():
        where = f" on {port}" if port else ""
        raise SimulatedHardwareError(
            f"--simulate run tried to open the real {device}{where}; "
            "a simulated run must use the injected stand-in devices"
        )
