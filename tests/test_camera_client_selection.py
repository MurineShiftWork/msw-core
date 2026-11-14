"""Camera clients start only the selected cameras and record their names.

The rce and flir backends are optional packages, so they are replaced here by
small fakes installed in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from murineshiftwork.hardware.camera import client as client_mod
from murineshiftwork.hardware.camera.client import (
    FlirBonsaiClient,
    RceConductorAdapter,
    make_camera_client,
)
from murineshiftwork.logic.config.models import CameraConfig, CameraUnit

_FLIR = "s1__20260620_120000_000000__video_flir__v1"
_RCE = "s1__20260620_120000_000000__video_rce"


def _install(monkeypatch, name: str, **attrs: Any) -> None:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        mod_name = ".".join(parts[:i])
        if mod_name not in sys.modules or i == len(parts):
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


@pytest.fixture
def fake_rce(monkeypatch):
    seen: dict[str, Any] = {}

    class EnsembleAcquisitionConfig(BaseModel):
        agents: dict[str, Any]

        @classmethod
        def from_yaml(cls, path):
            raw = yaml.safe_load(Path(path).read_text())["agents"]
            return cls(
                agents={
                    inst: SimpleNamespace(
                        camera_config=SimpleNamespace(**a["camera_config"])
                    )
                    for inst, a in raw.items()
                }
            )

    class Conductor:
        def __init__(self, config, ensemble_config):
            seen["agents"] = sorted(ensemble_config.agents)

        def start(self):
            pass

        def initialize_acquisition(self, **kw):
            pass

    monkeypatch.setitem(sys.modules, "rpi_camera_ensemble", types.ModuleType("r"))
    _install(
        monkeypatch, "rpi_camera_ensemble.conductor.conductor", Conductor=Conductor
    )
    _install(
        monkeypatch,
        "rpi_camera_ensemble.config.acquisition",
        EnsembleAcquisitionConfig=EnsembleAcquisitionConfig,
    )
    _install(
        monkeypatch,
        "rpi_camera_ensemble.config.conductor",
        ConductorConfig=lambda data_dir: SimpleNamespace(data_dir=data_dir),
    )
    return seen


@pytest.fixture
def ensemble(tmp_path):
    agents = {
        "agent-1": {"camera_config": {"description": "top"}},
        "agent-2": {"camera_config": {"description": "left"}},
        "agent-4": {"camera_config": {"description": "bottom"}},
    }
    p = tmp_path / "ens.yaml"
    p.write_text(yaml.safe_dump({"agents": agents}))
    return p


@pytest.fixture
def fake_flir(monkeypatch):
    started: list[dict] = []

    class BonsaiCameraRunner:
        def __init__(self, **kw):
            started.append(kw)

    class MultiCameraRunner(list):
        def start(self):
            pass

    monkeypatch.setitem(sys.modules, "msw_flir_bonsai", types.ModuleType("m"))
    _install(
        monkeypatch,
        "msw_flir_bonsai.runner",
        BonsaiCameraRunner=BonsaiCameraRunner,
        MultiCameraRunner=MultiCameraRunner,
    )
    return started


@pytest.fixture
def peers(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_append(container, basename, started_at=None, extra=None):
        calls.append((basename, extra))

    monkeypatch.setattr(client_mod, "append_acquisition_to_session", fake_append)
    return calls


def test_rce_only_selected_agents_reach_the_conductor(fake_rce, ensemble, tmp_path):
    adapter = make_camera_client(
        CameraConfig(backend="rce", config=str(ensemble), selected=["bottom"]),
        output_dir=str(tmp_path),
    )
    assert isinstance(adapter, RceConductorAdapter)
    adapter.start()
    assert fake_rce["agents"] == ["agent-4"]


def test_rce_without_selection_keeps_all_agents(fake_rce, ensemble, tmp_path):
    RceConductorAdapter(str(ensemble), str(tmp_path)).start()
    assert fake_rce["agents"] == ["agent-1", "agent-2", "agent-4"]


def test_rce_peer_entry_lists_recorded_cameras(fake_rce, ensemble, tmp_path, peers):
    adapter = RceConductorAdapter(str(ensemble), str(tmp_path), selected=["top"])
    adapter.start()
    adapter.initialize_acquisition(acqdir=str(tmp_path / _RCE), basename=_RCE)
    assert peers == [(_RCE, {"cameras": ["top"]})]


def test_flir_starts_runners_for_selected_units_only(fake_flir, tmp_path, peers):
    cfg = CameraConfig(
        backend="flir_bonsai",
        cameras=[CameraUnit(index=0, name="top"), CameraUnit(index=1, name="back")],
        selected=["back"],
    )
    client = FlirBonsaiClient(cfg, str(tmp_path))
    client.initialize_acquisition(acqdir=str(tmp_path / _FLIR), basename=_FLIR)
    client.start_recording()
    assert [r["cam_index"] for r in fake_flir] == [1]
    assert peers == [(_FLIR, {"cameras": ["back"]})]


def test_flir_refuses_selection_with_multi_camera_workflow(fake_flir, tmp_path):
    cfg = CameraConfig(
        backend="flir_bonsai",
        workflow="run-flir-flycap-2cam",
        cameras=[CameraUnit(index=0, name="top")],
        selected=["top"],
    )
    with pytest.raises(RuntimeError, match="-1cam"):
        FlirBonsaiClient(cfg, str(tmp_path)).start_recording()
    assert fake_flir == []


def test_peer_registers_without_names_on_old_msw_io(monkeypatch, tmp_path):
    calls: list[str] = []

    def old_append(container, basename, started_at=None):
        calls.append(basename)

    monkeypatch.setattr(client_mod, "append_acquisition_to_session", old_append)
    client_mod._register_camera_peer(str(tmp_path / _RCE), _RCE, ["top"])
    assert calls == [_RCE]
