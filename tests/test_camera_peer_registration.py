"""Camera clients register their acquisition as a peer in session_manifest.yaml.

initialize_acquisition() appends the camera acquisition to the container's
session manifest; stop_acquisition() finalizes it. Registration happens before
any backend (Conductor / Bonsai) is touched, so these run without the optional
rce/flir packages installed.
"""

from __future__ import annotations

import pytest
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
    # The manifest is registered with the msw-provided ABSOLUTE acq dir + basename (like FLIR),
    # so its container == the framework's session container. The RELATIVE acquisition_path is for
    # the conductor only and must NOT drive the (local) manifest path.
    acqdir = tmp_path / _RCE
    acqdir.mkdir()
    adapter = RceConductorAdapter(
        ensemble_cfg_file="cfg.yaml", output_dir=str(tmp_path)
    )

    adapter.initialize_acquisition(
        acquisition_path="rel/only/for/conductor",  # relative; must be ignored by the manifest
        acquisition_name="conductor_name",
        acqdir=str(acqdir),
        basename=_RCE,
    )
    assert _acquisitions(tmp_path)[_RCE]["status"] == "running"

    adapter.stop_acquisition()  # conductor is None; must still finalize
    assert _acquisitions(tmp_path)[_RCE]["status"] == "complete"


def test_registration_is_skipped_without_basename(tmp_path):
    client = FlirBonsaiClient(config=None, output_dir=str(tmp_path))
    client.initialize_acquisition(acqdir=str(tmp_path / _FLIR), basename="")
    assert not (tmp_path / "session_manifest.yaml").exists()


def test_clients_expose_backend_acq_type():
    # tasks must name the camera acquisition from the backend, not a hardcoded video_flir
    assert (
        RceConductorAdapter(ensemble_cfg_file="c", output_dir="d").acq_type
        == "video_rce"
    )
    assert FlirBonsaiClient(config=None, output_dir="d").acq_type == "video_flir"


class _FailingConductor:
    """Conductor stand-in that raises like EnsembleError (a RuntimeError)."""

    def __init__(self, failing: str = ""):
        self.failing = failing
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if name == self.failing:
            raise RuntimeError(f"{name} failed on cam01")

    def initialize_acquisition(self, **_):
        self._maybe_fail("initialize_acquisition")

    def start_preview(self):
        self._maybe_fail("start_preview")

    def start_recording(self):
        self._maybe_fail("start_recording")

    def stop_acquisition(self):
        self._maybe_fail("stop_acquisition")


def _rce_adapter(tmp_path, conductor):
    (tmp_path / _RCE).mkdir()
    adapter = RceConductorAdapter(ensemble_cfg_file="c", output_dir=str(tmp_path))
    adapter._conductor = conductor
    return adapter


def _run_session(adapter, tmp_path):
    adapter.initialize_acquisition(acqdir=str(tmp_path / _RCE), basename=_RCE)
    adapter.start_preview()
    adapter.start_recording()
    adapter.stop_acquisition()


@pytest.mark.parametrize(
    "failing",
    ["initialize_acquisition", "start_preview", "start_recording", "stop_acquisition"],
)
def test_rce_adapter_marks_peer_failed_and_reraises(tmp_path, failing):
    adapter = _rce_adapter(tmp_path, _FailingConductor(failing))
    with pytest.raises(RuntimeError, match="failed on cam01"):
        _run_session(adapter, tmp_path)
    assert _acquisitions(tmp_path)[_RCE]["status"] == "failed"

    # a later cleanup stop does not overwrite the failure
    adapter._conductor = _FailingConductor()
    adapter.stop_acquisition()
    assert _acquisitions(tmp_path)[_RCE]["status"] == "failed"


def test_rce_adapter_completes_peer_after_a_clean_session(tmp_path):
    conductor = _FailingConductor()
    adapter = _rce_adapter(tmp_path, conductor)
    _run_session(adapter, tmp_path)
    assert conductor.calls[-1] == "stop_acquisition"
    assert _acquisitions(tmp_path)[_RCE]["status"] == "complete"
