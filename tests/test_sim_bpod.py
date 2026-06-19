"""SimBpod fidelity: channel/event enums and a parseable trial export.

These guard the hardware-agnostic surface tasks read off the bpod handle
(OutputChannels/Events) and the session.current_trial.export() shape, so
tasks run end-to-end under --simulate without a serial device.
"""

import math


def _sim():
    from murineshiftwork.hardware.bpod.sim import SimBpod

    b = SimBpod()
    b.open()
    return b


def test_sim_exposes_channel_and_event_enums():
    b = _sim()
    # Tasks read e.g. self.bpod.OutputChannels.BNC1, self.bpod.Events.Tup
    assert b.OutputChannels.BNC1
    assert b.OutputChannels.BNC2
    assert b.Events.Tup
    assert hasattr(b.ChannelTypes, "OUTPUT")
    assert hasattr(b.ChannelNames, "VALVE")


def test_sim_session_export_has_trial_dict_shape():
    from pybpodapi.protocol import Bpod, StateMachine

    b = _sim()
    sma = StateMachine(bpod=b)
    sma.add_state(
        state_name="choice_left",
        state_timer=0.1,
        state_change_conditions={Bpod.Events.Tup: "exit"},
        output_actions=[],
    )
    sma.add_state(
        state_name="choice_right",
        state_timer=0.1,
        state_change_conditions={Bpod.Events.Tup: "exit"},
        output_actions=[],
    )
    b.send_state_machine(sma)
    assert b.run_state_machine(sma) is True

    exported = b.session.current_trial.export()
    for key in (
        "Bpod start timestamp",
        "Trial start timestamp",
        "Trial end timestamp",
        "States timestamps",
        "Events timestamps",
    ):
        assert key in exported
    st = exported["States timestamps"]
    # every state in the sma is keyed, each value an [[enter, exit]] window
    assert set(st) == {"choice_left", "choice_right"}
    enter, exit_ = st["choice_left"][0]
    assert not math.isnan(enter) and exit_ > enter


def test_sim_trial_clock_advances_across_runs():
    from pybpodapi.protocol import Bpod, StateMachine

    b = _sim()
    sma = StateMachine(bpod=b)
    sma.add_state(
        state_name="s",
        state_timer=0.1,
        state_change_conditions={Bpod.Events.Tup: "exit"},
        output_actions=[],
    )
    b.run_state_machine(sma)
    t1 = b.session.current_trial.export()["Trial end timestamp"]
    b.run_state_machine(sma)
    t2 = b.session.current_trial.export()["Trial start timestamp"]
    assert t2 >= t1
