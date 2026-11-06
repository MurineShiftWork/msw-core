"""Choosing the cameras a task records: names, ``all``, validation, narrowed config."""

from __future__ import annotations

import pytest
import yaml

from murineshiftwork.cli.evaluate import _resolve_record_cameras
from murineshiftwork.hardware.camera.selection import (
    CameraSelectionError,
    camera_names,
    parse_record_cameras,
    rce_camera_names,
    resolve_record_cameras,
    select_cameras,
)
from murineshiftwork.logic.config.models import CameraConfig, CameraUnit, SetupConfig


def write_ensemble(path, descriptions: dict[str, str]):
    """A minimal RCE ensemble YAML: one agent per entry, camera_config.description only."""
    agents = {
        inst: {"camera_config": {"description": desc}}
        for inst, desc in descriptions.items()
    }
    path.write_text(yaml.safe_dump({"agents": agents}))
    return path


@pytest.fixture
def rce(tmp_path):
    cfg = write_ensemble(
        tmp_path / "ens.yaml",
        {"agent-1": "top", "agent-2": "left", "agent-3": "bottom"},
    )
    return CameraConfig(backend="rce", config=str(cfg))


def flir(**kw) -> CameraConfig:
    return CameraConfig(
        backend="flir_bonsai",
        cameras=[CameraUnit(index=0, name="top"), CameraUnit(index=1, name="back")],
        **kw,
    )


# --- parsing record_cameras ---


@pytest.mark.parametrize(
    "value, expected",
    [
        ("top", ["top"]),
        ("top,bottom", ["top", "bottom"]),
        ("[top, 'bottom']", ["top", "bottom"]),
        (["top", "top", "left"], ["top", "left"]),
    ],
)
def test_parse_record_cameras(value, expected):
    assert parse_record_cameras(value) == expected


@pytest.mark.parametrize("value", ["all", " all "])
def test_parse_all(value):
    assert parse_record_cameras(value) == "all"


@pytest.mark.parametrize("value", [[], "", "[]", " , "])
def test_empty_record_cameras_is_an_error(value):
    with pytest.raises(CameraSelectionError, match="record_video: false"):
        parse_record_cameras(value)


@pytest.mark.parametrize("value", [3, [1, 2], {"top": True}])
def test_record_cameras_must_be_names(value):
    with pytest.raises(CameraSelectionError, match="camera names"):
        parse_record_cameras(value)


# --- camera names ---


def test_rce_names_come_from_agent_descriptions(rce):
    assert rce_camera_names(rce.config) == {
        "agent-1": "top",
        "agent-2": "left",
        "agent-3": "bottom",
    }
    assert camera_names(rce) == ["top", "left", "bottom"]


def test_flir_names_come_from_units():
    assert camera_names(flir()) == ["top", "back"]


def test_flir_shorthand_cannot_be_selected_from():
    with pytest.raises(CameraSelectionError, match="give every FLIR camera a name"):
        camera_names(CameraConfig(backend="flir_bonsai", n_cameras=2))


def test_rce_duplicate_names_are_refused(tmp_path):
    cfg = write_ensemble(tmp_path / "e.yaml", {"a": "top", "b": "top"})
    with pytest.raises(CameraSelectionError, match="duplicate"):
        camera_names(CameraConfig(backend="rce", config=str(cfg)))


def test_rce_missing_ensemble_is_refused(tmp_path):
    with pytest.raises(CameraSelectionError, match="not found"):
        camera_names(CameraConfig(backend="rce", config=str(tmp_path / "none.yaml")))


def test_flir_duplicate_names_fail_at_setup_load():
    with pytest.raises(ValueError, match="duplicate camera names"):
        CameraConfig(
            backend="flir_bonsai",
            cameras=[CameraUnit(index=0, name="top"), CameraUnit(index=1, name="top")],
        )


# --- resolving a set value ---


def test_names_narrow_the_config(rce):
    narrowed, recorded = resolve_record_cameras(rce, "left,top")
    assert narrowed is not None and narrowed.selected == ["left", "top"]
    assert recorded == ["left", "top"]


def test_all_records_every_camera_without_narrowing(rce):
    assert resolve_record_cameras(rce, "all") == (None, ["top", "left", "bottom"])
    assert resolve_record_cameras(flir(), "all") == (None, ["top", "back"])


def test_all_on_unnamed_cameras_records_them_all():
    shorthand = CameraConfig(backend="flir_bonsai", n_cameras=2)
    assert resolve_record_cameras(shorthand, "all") == (None, None)


def test_all_keeps_a_multi_camera_workflow():
    assert resolve_record_cameras(flir(workflow="run-flir-flycap-2cam"), "all") == (
        None,
        ["top", "back"],
    )


def test_record_cameras_without_cameras_is_an_error():
    for value in (["top"], "all"):
        with pytest.raises(CameraSelectionError, match="no cameras"):
            resolve_record_cameras(None, value)


# --- narrowed config ---


def test_select_returns_a_narrowed_copy(rce):
    narrowed = select_cameras(rce, ["bottom"])
    assert narrowed.selected == ["bottom"]
    assert narrowed.config == rce.config
    assert rce.selected is None  # the setup's own config is untouched


def test_unknown_name_is_refused(rce):
    with pytest.raises(
        CameraSelectionError, match=r"unknown camera name\(s\) \['front'\]"
    ):
        select_cameras(rce, ["front"])


def test_flir_multi_camera_workflow_is_refused():
    with pytest.raises(CameraSelectionError, match="-1cam"):
        select_cameras(flir(workflow="run-flir-flycap-2cam"), ["top"])
    assert select_cameras(flir(), ["back"]).selected == ["back"]


# --- evaluate: what the task receives ---


def _args(cameras, patched, source="setup:rig.yaml"):
    return {
        "task": "sequence",
        "setup_config": SetupConfig(name="setup-x", cameras=cameras),
        "cameras_config": cameras,
        "settings.task.patched": patched,
        "settings.task.provenance": {"record_cameras": source},
    }


def test_evaluate_narrows_config_and_keeps_provenance(rce):
    patched = {"record_cameras": "top"}
    args = _args(rce, patched, source="cli")
    _resolve_record_cameras(args, patched)
    assert args["cameras_config"].selected == ["top"]
    assert patched["record_cameras"] == ["top"]
    assert args["settings.task.provenance"]["record_cameras"] == "cli"


def test_evaluate_all_records_the_names_and_keeps_the_config(rce):
    patched = {"record_cameras": "all"}
    args = _args(rce, patched)
    _resolve_record_cameras(args, patched)
    assert args["cameras_config"] is rce
    assert patched["record_cameras"] == ["top", "left", "bottom"]
    assert args["settings.task.provenance"]["record_cameras"] == "setup:rig.yaml"


def test_evaluate_all_on_unnamed_cameras_is_dropped():
    shorthand = CameraConfig(backend="flir_bonsai", n_cameras=2)
    patched = {"record_cameras": "all"}
    args = _args(shorthand, patched)
    _resolve_record_cameras(args, patched)
    assert args["cameras_config"] is shorthand
    assert "record_cameras" not in patched
    assert "record_cameras" not in args["settings.task.provenance"]


@pytest.mark.parametrize("patched", [{}, {"record_cameras": None}])
def test_evaluate_unset_records_all_cameras_as_before(rce, patched):
    args = _args(rce, patched)
    _resolve_record_cameras(args, patched)
    assert args["cameras_config"] is rce
    assert patched.get("record_cameras") is None


@pytest.mark.parametrize(
    "value, match",
    [(["front"], "unknown camera"), ([], "empty"), ("", "empty"), (3, "camera names")],
)
def test_evaluate_refuses_bad_values_before_hardware(rce, value, match):
    patched = {"record_cameras": value}
    with pytest.raises(CameraSelectionError, match=match):
        _resolve_record_cameras(_args(rce, patched), patched)
