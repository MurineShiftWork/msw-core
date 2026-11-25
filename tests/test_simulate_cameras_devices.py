"""--simulate: no camera starts, and every required device has a stand-in."""

from __future__ import annotations

import pytest
from msw_plugin_api.camera import UNIT_READY, UnitResult

from murineshiftwork.cli.evaluate import _resolve_camera_groups
from murineshiftwork.cli.execute import _build_sim_device_list
from murineshiftwork.hardware.camera.session import CameraSession
from murineshiftwork.hardware.pulsepal.sim import SimPulsePal
from murineshiftwork.hardware.simulate_guard import (
    SimulatedHardwareError,
    mark_simulated,
)
from murineshiftwork.logic.config.models import (
    BpodDevice,
    CameraGroup,
    SetupConfig,
)
from murineshiftwork.logic.run_context import RunContext


def _setup(**devices) -> SetupConfig:
    return SetupConfig(
        name="rig",
        devices=devices or {"bpod": BpodDevice(type="bpod", port="COM1")},
        cameras=[CameraGroup(name="top", backend="rce", settings={"config": "e.yaml"})],
    )


def test_simulate_resolves_no_camera_groups(caplog):
    args = {"simulate": True, "config_file_camera": "ens.yaml"}
    with caplog.at_level("INFO"):
        _resolve_camera_groups(args, _setup())
    assert args["camera_groups"] == []
    assert args["cameras_config"] is None
    assert args["config_file_camera"] == ""
    assert "simulate: cameras not started" in caplog.text


class _Backend:
    acq_type = "video_fake"

    def configure(self, settings, units):
        pass

    def preflight(self):
        return [UnitResult(name="top", ok=True, state=UNIT_READY)]

    def close(self):
        pass


def test_camera_session_refuses_real_backends_when_simulated():
    mark_simulated(True)
    with pytest.raises(SimulatedHardwareError, match="camera group"):
        CameraSession([CameraGroup(name="top", backend="rce")]).open()


def test_camera_session_with_an_injected_backend_runs_when_simulated():
    mark_simulated(True)
    session = CameraSession(
        [CameraGroup(name="top", backend="fake")], backend_loader=lambda _: _Backend()
    )
    session.open()
    session.stop()


def test_simulated_session_stands_in_for_a_video_task(caplog, tmp_path):
    from murineshiftwork.hardware.camera import session as camera_session

    mark_simulated(True)
    camera_session.reset_run_sessions()
    args = {"simulate": True, "camera_groups": []}
    with caplog.at_level("INFO"):
        cams = CameraSession.from_run(args, record_video=True)
    # a task that requires a camera group finds one, and nothing is driven
    assert cams
    assert [g.record for g in cams.groups] == ["with_video"]
    cams.open()
    cams.start({"session_folder": str(tmp_path)})
    cams.stop("completed")
    assert cams.recorded_cameras() == []
    assert camera_session.run_requested_video() is False
    assert list(tmp_path.iterdir()) == []
    assert "simulate: video recording off" in caplog.text


def test_simulated_session_without_video_is_empty():
    assert not CameraSession.from_run(
        {"simulate": True, "settings.task.patched": {"record_video": False}}
    )


def _names(devices) -> list[str]:
    return [d.name for d in devices]


def test_sim_builds_required_devices_the_setup_lacks():
    ctx = RunContext(
        setup=_setup(),
        setup_name="rig",
        task_settings={"required_devices": ["bpod", "pulsepal", "stage", "scale"]},
    )
    assert _names(_build_sim_device_list(ctx)) == ["bpod", "pulsepal", "stage", "scale"]


def test_sim_without_setup_builds_the_required_devices():
    ctx = RunContext(task_settings={"required_devices": ["bpod", "pulsepal"]})
    assert _names(_build_sim_device_list(ctx)) == ["bpod", "pulsepal"]


def test_sim_skips_a_device_type_without_stand_in():
    ctx = RunContext(setup=_setup(), task_settings={"required_devices": ["laser"]})
    assert _build_sim_device_list(ctx) == []


def test_stimulation_connects_a_sim_pulsepal_when_simulated():
    from murineshiftwork.hardware.stimulation import Stimulation

    mark_simulated(True)
    stim = Stimulation(port="COM9", in_dict={})
    stim.connect()
    assert isinstance(stim.pulsepal, SimPulsePal)
    stim.disconnect()
    assert stim.pulsepal is None
