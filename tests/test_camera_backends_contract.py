"""CameraSession driving the real first-party camera backends.

The backends run against fakes at their lowest seams: a fake RCE conductor and fake
Bonsai runners. Each test is skipped when its backend package is not installed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from murineshiftwork.namespace import msw_file
from murineshiftwork.namespace.manifest import (
    append_acquisition_to_session,
    init_acquisition_manifest,
    init_session_manifest,
)
from murineshiftwork.namespace.paths import generate_session_paths

from murineshiftwork.cli import evaluate as ev
from murineshiftwork.hardware.camera.records import read_cameras_record
from murineshiftwork.hardware.camera.session import CameraSession
from murineshiftwork.logic.config.models import SetupConfig


@pytest.fixture
def session_paths(tmp_path):
    sp = generate_session_paths(
        subject="m1", task="seq", basepath=tmp_path / "data", printout=False
    )
    folder = Path(sp["session_folder"])
    folder.mkdir(parents=True)
    init_session_manifest(folder.parent, sp["host_session_name"])
    append_acquisition_to_session(folder.parent, sp["session_basename"])
    init_acquisition_manifest(folder, sp["session_basename"])
    return sp


def _peer_statuses(sp) -> dict[str, str]:
    data = yaml.safe_load(
        (Path(sp["session_folder"]).parent / "session_manifest.yaml").read_text()
    )
    return {a["group"]: a["status"] for a in data["acquisitions"] if "group" in a}


# --- rce ---


class FakeConductor:
    """The conductor surface ``RceBackend`` uses; every agent answers."""

    instances: list[FakeConductor] = []

    def __init__(self, config, ensemble_config) -> None:
        from rpi_camera_ensemble.conductor.models import (
            AgentResult,
            AgentStatusResponse,
            EnsembleResult,
        )

        self._models = (AgentResult, AgentStatusResponse, EnsembleResult)
        self.config = config
        self.ensemble_config = ensemble_config
        self.agents: dict[str, object] = {}
        self._stop_sent = False
        FakeConductor.instances.append(self)

    def start(self) -> None:
        pass

    def setup_agents(self, raise_on_failure: bool = True):
        self.agents = {name: object() for name in self.ensemble_config.agents}
        return self._ok()

    def agent_status(self):
        _, status, _ = self._models
        return [
            status(
                agent_name=n, ok=True, status={"status": {}}, timestamp=datetime.now()
            )
            for n in self.agents
        ]

    def _ok(self):
        result, _, ensemble = self._models
        out = ensemble()
        for name in self.agents:
            out[name] = result(agent_name=name, success=True)
        return out

    def initialize_acquisition(self, acquisition_path, acquisition_name, **_):
        self.ensemble_config.initialize_session(acquisition_path, acquisition_name)
        return self._ok()

    def start_preview(self, **_):
        return self._ok()

    def start_recording(self, **_):
        return self._ok()

    @property
    def stop_pending(self) -> bool:
        return self.ensemble_config.session is not None and not self._stop_sent

    def stop_acquisition(self, **_):
        self._stop_sent = True
        return self._ok()

    def stop(self) -> None:
        pass


def test_rce_backend_gets_the_run_data_dir(tmp_path, session_paths):
    msw = pytest.importorskip("rpi_camera_ensemble.integrations.msw")
    ensemble = tmp_path / "pupil.yaml"
    ensemble.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "pi_eye": {
                        "api_url": "http://pi-eye.example:8876",
                        "camera_config": {"description": "eye"},
                    }
                }
            }
        )
    )
    setup = SetupConfig(name="rig", cameras={"backend": "rce", "config": str(ensemble)})
    out_path = str(session_paths["basepath"])
    args: dict[str, Any] = {"out_path": out_path, "config_dir": ""}
    ev._resolve_camera_groups(args, setup)
    [group] = args["camera_groups"]

    FakeConductor.instances = []
    backend = msw.RceBackend(conductor_factory=FakeConductor)
    with CameraSession([group], backend_loader=lambda name: backend) as cams:
        cams.start(session_paths)

    [conductor] = FakeConductor.instances
    assert conductor.config.data_dir == Path(out_path)
    assert _peer_statuses(session_paths) == {"rce": "complete"}
    [record] = read_cameras_record(session_paths["session_folder"]).groups
    assert (record.backend_version, record.acq_type) == (msw.__version__, "video_rce")
    [unit] = record.units
    assert (unit.name, unit.hw_id, unit.state) == ("eye", "pi-eye.example", "stopped")
    assert unit.extra["acquisition_name"] == session_paths["session_basename"]


# --- flir_bonsai ---


class FakeRunner:
    """A Bonsai camera process that records a frame and a timestamp."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.root = Path(kwargs["acqdir"])
        self.stem = kwargs["cam_basename"]
        self.running = False
        self.pid = 1000 + kwargs["cam_index"]
        self.exited_unexpectedly = False

    def start(self) -> None:
        self.running = True

    def check_started(self) -> None:
        (self.root / f"{self.stem}.avi").write_bytes(b"frame")

    def stop(self, timeout: float = 5.0) -> None:
        if self.running:
            (self.root / f"{self.stem}.timestamps.csv").write_text("1\n")
        self.running = False

    def wait(self, timeout: float | None = None) -> int | None:
        return 0

    @property
    def is_running(self) -> bool:
        return self.running


@pytest.mark.parametrize(
    "cameras, labels",
    [
        # unnamed legacy cameras keep the file names they had before camera groups
        ([{"index": 0}, {"index": 1}], ["cam0", "cam1"]),
        (
            [{"index": 0, "name": "top"}, {"index": 1, "name": "back"}],
            ["top.0", "back.1"],
        ),
    ],
)
def test_flir_backend_names_files_as_before(tmp_path, session_paths, cameras, labels):
    flir = pytest.importorskip("msw_flir_bonsai.msw")
    from msw_flir_bonsai.probe import ProbeResult

    exe = tmp_path / "Bonsai.exe"
    exe.touch()
    setup = SetupConfig(
        name="rig",
        cameras={"backend": "flir_bonsai", "bonsai_exe": str(exe), "cameras": cameras},
    )
    runners: list[FakeRunner] = []

    def runner_factory(**kwargs):
        runners.append(FakeRunner(**kwargs))
        return runners[-1]

    backend = flir.FlirBonsaiBackend(
        probe=lambda driver: ProbeResult(driver), runner_factory=runner_factory
    )
    with CameraSession(setup.cameras, backend_loader=lambda name: backend) as cams:
        cams.start(session_paths)

    [record] = read_cameras_record(session_paths["session_folder"]).groups
    basename = record.basename
    assert [r.kwargs["cam_basename"] for r in runners] == [
        msw_file(basename, label).name for label in labels
    ]
    assert record.status == "complete"
    assert [sorted(u.files) for u in record.units] == [
        sorted(
            [
                f"{msw_file(basename, lb).name}.avi",
                f"{msw_file(basename, lb).name}.timestamps.csv",
            ]
        )
        for lb in labels
    ]
