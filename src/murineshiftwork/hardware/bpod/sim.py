"""Simulated Bpod: drop-in replacement for BpodFactory that logs all commands.

Enables hardware-free testing and CI coverage of tasks and action drivers without
any USB devices connected.  All state-machine calls return success; manual_override
calls are recorded in self.calls for assertion in tests.

SimBpod.hardware is pre-populated to match a standard 4-port Bpod so that
StateMachine(bpod=sim_bpod) and sma.add_state(...) work without any serial
connection.  The hardware configuration represents:
  outputs: SoftCode, PWM1-4, Valve1-4, BNC1-2, Wire1-2
  inputs:  SoftCode1-15, Port1-4In/Out/Lick, BNC1-2, Wire1-2, Tup

Usage:
    from murineshiftwork.hardware.bpod.sim import SimBpod

    bpod = SimBpod()
    bpod.open()
    # ... use in tasks or BpodActionDriver ...
    assert ("manual_override", ...) in bpod.calls
"""

import logging


def _build_sim_hardware():
    """Return a pre-populated Hardware matching a standard 8-port Bpod.

    Eight behaviour ports cover every bundled task (sequence uses ports up to
    7), so tasks run end to end under --simulate. Populated enough that
    StateMachine(bpod=...) and add_state() with Port/Valve/PWM, BNC, SoftCode,
    and Tup events all work without a serial connection.
    """
    from pybpodapi.bpod.hardware.hardware import Hardware

    h = Hardware()
    h.max_states = 255
    h.n_conditions = 5
    h.n_global_counters = 5
    h.n_global_timers = 5
    h.max_serial_events = 15
    h.firmware_version = 22
    h.machine_type = 2  # 8-port (state machine r2/r2+)
    h.cycle_period = 100
    # 8 behaviour ports (P), USB (X), 2 BNC (B), 2 Wire (W): no UART modules
    h.inputs = ["X", *(["P"] * 8), "B", "B", "W", "W"]
    h.inputs_enabled = [1] * len(h.inputs)
    # USB (X), 8 PWM ports (P), 8 Valves (V), 2 BNC (B), 2 Wire (W)
    h.outputs = ["X", *(["P"] * 8), *(["V"] * 8), "B", "B", "W", "W"]
    h.n_uart_channels = 0
    h.setup(modules=[])
    return h


class _SimTrial:
    """A synthetic pybpodapi trial whose export() mirrors a real trial dict.

    All states in the sent state machine are marked visited with sequential
    [enter, exit] windows starting at ``start``; events are left empty.
    """

    _STATE_DURATION = 0.1

    def __init__(self, sma, start: float = 0.0) -> None:
        self._state_names = list(getattr(sma, "state_names", []) or [])
        self._start = start
        t = start
        self._states: dict[str, list] = {}
        for name in self._state_names:
            self._states[name] = [[round(t, 6), round(t + self._STATE_DURATION, 6)]]
            t += self._STATE_DURATION
        self._end = t

    @property
    def duration(self) -> float:
        return self._end - self._start

    def export(self) -> dict:
        return {
            "Bpod start timestamp": 0.0,
            "Trial start timestamp": round(self._start, 6),
            "Trial end timestamp": round(self._end, 6),
            "States timestamps": self._states,
            "Events timestamps": {},
        }


class _SimSession:
    """Minimal stand-in for pybpodapi's Session: tracks the current trial."""

    def __init__(self) -> None:
        self.trials: list = []
        self.current_trial: _SimTrial | None = None
        self.clock = 0.0

    def add_trial(self, trial: _SimTrial) -> None:
        self.trials.append(trial)
        self.current_trial = trial
        self.clock = trial._end


class SimBpod:
    """Simulated BpodFactory: logs all interactions, returns success.

    Mirrors the BpodFactory public API so it can be injected wherever a real
    BpodFactory is expected (TaskProcess bpod= kwarg, BpodActionDriver, etc.).
    """

    def __init__(self, **kwargs) -> None:
        from pybpodapi.protocol import Bpod

        self.calls: list = []
        self._softcode_handler = None
        self.hardware = _build_sim_hardware()
        self.session = _SimSession()
        # Expose the static channel/event enums that tasks read off the bpod
        # handle (e.g. self.bpod.OutputChannels.BNC1, self.bpod.Events.Tup).
        # These are class-level constants on pybpodapi's Bpod, available
        # without a serial connection; mirroring them keeps tasks
        # hardware-agnostic between SimBpod and the real BpodFactory.
        self.OutputChannels = Bpod.OutputChannels
        self.Events = Bpod.Events
        self.ChannelTypes = Bpod.ChannelTypes
        self.ChannelNames = Bpod.ChannelNames

    # Context manager

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close_safely()

    # Connection lifecycle

    def open(self, **kwargs) -> None:
        logging.debug("[SIM] Bpod.open()")
        self.calls.append(("open",))

    def close_safely(self) -> None:
        logging.debug("[SIM] Bpod.close_safely()")
        self.calls.append(("close_safely",))

    def stop_trial(self) -> None:
        logging.debug("[SIM] Bpod.stop_trial()")
        self.calls.append(("stop_trial",))

    # Softcode handler property (mirrors BpodFactory proxy)

    @property
    def softcode_handler_function(self):
        return self._softcode_handler

    @softcode_handler_function.setter
    def softcode_handler_function(self, value):
        self._softcode_handler = value

    # State machine

    def send_state_machine(self, sma) -> None:
        state_names = getattr(sma, "state_names", "?")
        logging.debug(f"[SIM] Bpod.send_state_machine(states={state_names})")
        self.calls.append(("send_state_machine", sma))

    def run_state_machine(self, sma) -> bool:
        logging.debug("[SIM] Bpod.run_state_machine() -> True")
        self.calls.append(("run_state_machine", sma))
        # Populate session.current_trial so tasks that call
        # self.bpod.session.current_trial.export() get a parseable trial. Every
        # state in the sent state machine is marked visited with a sequential
        # [enter, exit] window; events are left empty. This is behaviourally
        # neutral but structurally faithful, enough to exercise task data
        # pipelines end-to-end without hardware.
        self.session.add_trial(_SimTrial(sma, start=self.session.clock))
        return True

    # Firmware override

    def manual_override(
        self, channel_type, channel_name, channel_number, value
    ) -> None:
        logging.debug(
            f"[SIM] Bpod.manual_override("
            f"type={channel_type}, name={channel_name}, "
            f"ch={channel_number}, val={value})"
        )
        self.calls.append(
            (
                "manual_override",
                channel_type,
                channel_name,
                channel_number,
                value,
            )
        )

    # Helpers for test assertions

    def override_calls(self) -> list:
        """Return only the manual_override call tuples."""
        return [c for c in self.calls if c[0] == "manual_override"]

    def sma_run_count(self) -> int:
        """Number of times run_state_machine was called."""
        return sum(1 for c in self.calls if c[0] == "run_state_machine")
