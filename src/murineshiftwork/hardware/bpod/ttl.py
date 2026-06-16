import logging

from pybpodapi.bpod import Bpod


def add_trial_onset_ttl(
    sma=None,
    state_name_tuple=("ttl_on", "ttl_off"),
    ttl_pulse_duration=None,
    bnc_channel=Bpod.OutputChannels.BNC2,
    next_state=None,
):
    if not isinstance(bnc_channel, list) or not isinstance(bnc_channel, tuple):
        bnc_channel = [bnc_channel]

    if not isinstance(bnc_channel[0], str):
        raise ValueError(
            f"bnc_channel variable can only be list, tuple or str, but is {bnc_channel}"
        )

    logging.debug(f"Sending trial onset TTL: {ttl_pulse_duration}s on {bnc_channel}")

    sma.add_state(
        state_name=state_name_tuple[0],
        state_timer=ttl_pulse_duration,
        state_change_conditions={Bpod.Events.Tup: state_name_tuple[1]},
        output_actions=[(ch, 1) for ch in bnc_channel],
    )
    sma.add_state(
        state_name=state_name_tuple[1],
        state_timer=0,
        state_change_conditions={Bpod.Events.Tup: next_state},
        output_actions=[(ch, 0) for ch in bnc_channel],
    )
    return sma
