"""Camera clients register their acquisition as a peer in session_manifest.yaml.

initialize_acquisition() appends the camera acquisition to the container's
session manifest; stop_acquisition() finalizes it. Registration happens before
any backend (Conductor / Bonsai) is touched, so these run without the optional
rce/flir packages installed.
"""

from __future__ import annotations

import yaml

from murineshiftwork.hardware.camera.client import (
    FlirBonsaiClient,
    RceConductorAdapter,
)

_FLIR = "s1__20260620_120000_000000__video_flir__v1"
_RCE = "s1__20260620_120000_000000__video_rce"


def _acquisitions(container):
    manifest = container / "session_manifest.yaml"
    assert manifest.exists(), "session_manifest.yaml was not created"
    data = yaml.safe_load(manifest.read_text()) or {}
    return {a["basename"]: a for a in data.get("acquisitions", [])}


def test_flir_client_registers_and_finalizes_peer(tmp_path):
    acqdir = tmp_path / _FLIR
    acqdir.mkdir()
    client = FlirBonsaiClient(config=None, output_dir=str(tmp_path))

    client.initialize_acquisition(acqdir=str(acqdir), basename=_FLIR)
    entry = _acquisitions(tmp_path)[_FLIR]
    assert entry["status"] == "running"

    client.stop_acquisition()  # no runner started; must still finalize
    assert _acquisitions(tmp_path)[_FLIR]["status"] == "complete"


def test_rce_adapter_registers_and_finalizes_peer(tmp_path):
    acqdir = tmp_path / _RCE
    acqdir.mkdir()
    adapter = RceConductorAdapter(
        ensemble_cfg_file="cfg.yaml", output_dir=str(tmp_path)
    )

    adapter.initialize_acquisition(acquisition_path=str(acqdir), acquisition_name=_RCE)
    assert _acquisitions(tmp_path)[_RCE]["status"] == "running"

    adapter.stop_acquisition()  # conductor is None; must still finalize
    assert _acquisitions(tmp_path)[_RCE]["status"] == "complete"


def test_rce_adapter_resolves_relative_path_against_output_dir(tmp_path):
    # The real caller passes session_folder_relative (relative to the data
    # root), so registration must resolve it against output_dir rather than the
    # process CWD: otherwise the manifest write lands on a path that does not
    # exist and raises FileNotFoundError.
    rel = f"s1/s1__20260620_120000_000000/{_RCE}"
    container = tmp_path / "s1" / "s1__20260620_120000_000000"
    container.mkdir(parents=True)
    adapter = RceConductorAdapter(
        ensemble_cfg_file="cfg.yaml", output_dir=str(tmp_path)
    )

    adapter.initialize_acquisition(acquisition_path=rel, acquisition_name=_RCE)
    assert (container / "session_manifest.yaml").exists()
    assert _acquisitions(container)[_RCE]["status"] == "running"

    adapter.stop_acquisition()  # conductor is None; must still finalize
    assert _acquisitions(container)[_RCE]["status"] == "complete"


def test_registration_is_skipped_without_basename(tmp_path):
    client = FlirBonsaiClient(config=None, output_dir=str(tmp_path))
    client.initialize_acquisition(acqdir=str(tmp_path / _FLIR), basename="")
    assert not (tmp_path / "session_manifest.yaml").exists()
