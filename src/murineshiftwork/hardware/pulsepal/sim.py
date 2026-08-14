"""Hardware-free PulsePal stand-in for ``--simulate`` runs.

Covers exactly the surface :class:`murineshiftwork.hardware.stimulation.Stimulation` (and tasks)
touch: every programming/control call is a no-op, and the few *read* attributes it uses
(``channel_configs``, ``nr_output_channels``) return sensible values, so an opto/stim task runs
end to end without a PulsePal attached.
"""

from __future__ import annotations


class _SimChannelConfig:
    """Mutable per-channel config stand-in. Starts with the trigger-link flags Stimulation reads;
    any other pulse parameter it sets via ``setattr`` is simply stored (and ignored)."""

    def __init__(self) -> None:
        self.linkTriggerChannel1 = False
        self.linkTriggerChannel2 = False


class SimPulsePal:
    """No-op PulsePal: accepts all programming calls, holds four channel configs."""

    firmware_version = "sim"
    nr_output_channels = 4

    def __init__(self) -> None:
        self.channel_configs = [
            _SimChannelConfig() for _ in range(self.nr_output_channels)
        ]

    # Programming + control — all no-ops.
    def program_one_param(self, *args, **kwargs) -> None: ...
    def upload_custom_waveform(self, *args, **kwargs) -> None: ...
    def sync_all_params(self, *args, **kwargs) -> None: ...
    def set_continuous(self, *args, **kwargs) -> None: ...
    def program_trigger_channel(self, *args, **kwargs) -> None: ...
    def trigger_selected_channels(self, *args, **kwargs) -> None: ...
    def stop_all_outputs(self, *args, **kwargs) -> None: ...
    def save_settings(self, *args, **kwargs) -> None: ...
    def _pulsepal_set_display(self, *args, **kwargs) -> None: ...
