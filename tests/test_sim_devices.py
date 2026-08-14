"""Simulation device stand-ins (Cycle D Step 2): the device collection must be present under
``--simulate`` for every required device type, so a migrated task runs hardware-free.
"""

from __future__ import annotations

from murineshiftwork.cli.execute import _SIM_DEVICE_REGISTRY
from murineshiftwork.hardware.pulsepal.device import PulsePalDevice
from murineshiftwork.hardware.pulsepal.sim import SimPulsePal
from murineshiftwork.hardware.stage import SimStage, StageDevice


def test_sim_registry_covers_pulsepal_and_stage():
    # every real device type with a hardware-free stand-in is registered for --simulate
    assert set(_SIM_DEVICE_REGISTRY) >= {"bpod", "scale", "pulsepal", "stage_tower"}


def test_sim_stage_device_preflight_noop_and_homes():
    dev = StageDevice(serial_port="", simulate=True)
    dev.preflight()  # must not raise despite an empty serial port
    dev.connect()
    stage = dev.handle
    assert isinstance(stage, SimStage)
    # the task's homing sequence works: save 'front', then it is a non-empty known position
    stage.save_as_known_position("front")
    assert stage.known_positions["front"]
    stage.small_increment = 20
    stage.move_to_known_position("back")  # no-op, no hardware
    dev.disconnect()  # no-op close, no raise


def test_sim_pulsepal_device_preflight_noop_and_programs():
    dev = PulsePalDevice(serial_port="", simulate=True)
    dev.preflight()  # must not raise despite an empty serial port
    dev.connect()
    pp = dev.handle
    assert isinstance(pp, SimPulsePal)
    assert pp.nr_output_channels == 4 and len(pp.channel_configs) == 4
    pp.program_one_param(channel=0, param_name="linkTriggerChannel1", param_value=1)
    pp.sync_all_params()
    pp.stop_all_outputs()
    dev.disconnect()  # no-op close, no raise


def test_stimulation_runs_against_sim_pulsepal():
    """The real consumer (Stimulation) must connect + configure through the sim handle without
    touching hardware - this is what lets optotagging run under --simulate."""
    from murineshiftwork.hardware.stimulation import Stimulation

    stim = Stimulation(in_dict={})
    stim.connect(
        handle=SimPulsePal()
    )  # exercises _sync_channel_configs + sync_all_params + off
    assert stim.is_open() is False  # sim handle has no serial connection
    stim.off()
