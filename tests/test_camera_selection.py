"""Choosing the cameras a task records: names, group:, role:, all, narrowed groups."""

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
    select_units,
)
from murineshiftwork.logic.config.models import CameraGroup, CameraUnit


def write_ensemble(path, descriptions: dict[str, str]):
    """A minimal RCE ensemble YAML: one agent per entry, camera_config.description only."""
    agents = {
        inst: {"camera_config": {"description": desc}}
        for inst, desc in descriptions.items()
    }
    path.write_text(yaml.safe_dump({"agents": agents}))
    return path


def rce_group(cfg, **kw) -> CameraGroup:
    return CameraGroup(name="rce", backend="rce", settings={"config": str(cfg)}, **kw)


@pytest.fixture
def rce(tmp_path):
    cfg = write_ensemble(
        tmp_path / "ens.yaml",
        {"agent-1": "top", "agent-2": "left", "agent-3": "bottom"},
    )
    return rce_group(cfg)


def flir(**settings) -> CameraGroup:
    return CameraGroup(
        name="behaviour",
        backend="flir_bonsai",
        role="body",
        units=[CameraUnit(name="top", index=0), CameraUnit(name="back", index=1)],
        settings=settings,
    )


def pupil(record="always") -> CameraGroup:
    return CameraGroup(
        name="pupil",
        backend="rce",
        role="eye",
        record=record,
        units=[CameraUnit(name="eye")],
    )


# --- parsing record_cameras ---


@pytest.mark.parametrize(
    "value, expected",
    [
        ("top", ["top"]),
        ("top,bottom", ["top", "bottom"]),
        ("[top, 'bottom']", ["top", "bottom"]),
        (["top", "top", "left"], ["top", "left"]),
        ("top,group:side", ["top", "group:side"]),
        (["role:eye"], ["role:eye"]),
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


@pytest.mark.parametrize("value", ["a:b", ["unit:top"]])
def test_unknown_prefix_is_refused(value):
    with pytest.raises(CameraSelectionError, match="prefix"):
        parse_record_cameras(value)


@pytest.mark.parametrize("value", ["group:", "role: "])
def test_empty_prefix_is_refused(value):
    with pytest.raises(CameraSelectionError, match="names nothing"):
        parse_record_cameras(value)


# --- camera names ---


def test_rce_names_come_from_agent_descriptions(rce):
    assert rce_camera_names(rce.settings["config"]) == {
        "agent-1": "top",
        "agent-2": "left",
        "agent-3": "bottom",
    }
    assert camera_names(rce) == ["top", "left", "bottom"]


def test_unit_names_come_first():
    assert camera_names(flir()) == ["top", "back"]
    assert camera_names(pupil()) == ["eye"]


def test_shorthand_without_units_cannot_be_selected_from():
    group = CameraGroup(
        name="flir_bonsai", backend="flir_bonsai", settings={"n_cameras": 2}
    )
    with pytest.raises(CameraSelectionError, match="named units"):
        camera_names(group)


def test_rce_duplicate_names_are_refused(tmp_path):
    cfg = write_ensemble(tmp_path / "e.yaml", {"a": "top", "b": "top"})
    with pytest.raises(CameraSelectionError, match="duplicate"):
        camera_names(rce_group(cfg))


def test_rce_missing_ensemble_is_refused(tmp_path):
    with pytest.raises(CameraSelectionError, match="not found"):
        camera_names(rce_group(tmp_path / "none.yaml"))


def test_rce_names_are_stripped(tmp_path):
    cfg = write_ensemble(tmp_path / "e.yaml", {"a": " top ", "b": "left"})
    assert camera_names(rce_group(cfg)) == ["top", "left"]


# --- narrowing one group ---


def test_select_units_narrows_a_copy():
    group = flir()
    narrowed = select_units(group, ["back"])
    assert narrowed.unit_names == ["back"]
    assert narrowed.units[0].unit_settings == {"index": 1}
    assert group.unit_names == ["top", "back"]  # the setup's group is untouched


def test_select_units_of_rce_without_units_lists_them(rce):
    assert select_units(rce, ["bottom", "top"]).unit_names == ["top", "bottom"]


def test_unknown_name_is_refused(rce):
    with pytest.raises(
        CameraSelectionError, match=r"unknown camera name\(s\) \['front'\]"
    ):
        select_units(rce, ["front"])


def test_flir_multi_camera_workflow_is_refused():
    with pytest.raises(CameraSelectionError, match="-1cam"):
        select_units(flir(workflow="run-flir-flycap-2cam"), ["top"])
    assert select_units(flir(), ["back"]).unit_names == ["back"]


# --- resolving a set value over groups ---


def test_names_narrow_their_group(rce):
    narrowed, recorded = resolve_record_cameras([rce], "left,top")
    assert [g.unit_names for g in narrowed] == [["top", "left"]]
    assert recorded == ["left", "top"]


def test_names_across_groups(tmp_path):
    side = rce_group(write_ensemble(tmp_path / "e.yaml", {"a": "side"}))
    narrowed, recorded = resolve_record_cameras([flir(), side], ["side", "back"])
    assert [(g.name, g.unit_names) for g in narrowed] == [
        ("behaviour", ["back"]),
        ("rce", ["side"]),
    ]
    assert recorded == ["side", "back"]


def test_unchosen_video_group_does_not_start(rce):
    narrowed, recorded = resolve_record_cameras([flir(), rce], ["top"])
    # "top" is the first group's camera; the RCE group is left out
    assert [g.name for g in narrowed] == ["behaviour"]
    assert recorded == ["top"]


def test_group_prefix_keeps_the_whole_group(rce):
    narrowed, recorded = resolve_record_cameras([flir(), rce], "group:rce")
    assert narrowed == [rce]
    assert recorded == ["top", "left", "bottom"]


def test_group_prefix_on_unnamed_group_records_the_token():
    shorthand = CameraGroup(name="flir_bonsai", backend="flir_bonsai")
    narrowed, recorded = resolve_record_cameras([shorthand], "group:flir_bonsai")
    assert narrowed == [shorthand]
    assert recorded == ["group:flir_bonsai"]


def test_role_prefix_picks_groups_by_role(rce):
    narrowed, recorded = resolve_record_cameras([flir(), rce], "role:body")
    assert [g.name for g in narrowed] == ["behaviour"]
    assert recorded == ["top", "back"]


def test_always_groups_are_kept_and_cannot_be_chosen(rce):
    groups = [flir(), pupil()]
    narrowed, recorded = resolve_record_cameras(groups, "back")
    assert [(g.name, g.unit_names) for g in narrowed] == [
        ("behaviour", ["back"]),
        ("pupil", ["eye"]),
    ]
    assert recorded == ["back"]
    for value in ("eye", "group:pupil"):
        with pytest.raises(CameraSelectionError, match="records always"):
            resolve_record_cameras(groups, value)
    with pytest.raises(CameraSelectionError, match="no video group has role"):
        resolve_record_cameras(groups, "role:eye")


@pytest.mark.parametrize(
    "value, match",
    [
        ("group:nope", "unknown camera group"),
        ("role:nope", "no video group has role"),
        ("front", r"unknown camera name\(s\) \['front'\]"),
    ],
)
def test_unknown_values_are_refused(rce, value, match):
    with pytest.raises(CameraSelectionError, match=match):
        resolve_record_cameras([flir(), rce], value)


def test_all_records_every_video_camera_without_narrowing(rce):
    assert resolve_record_cameras([rce], "all") == (None, ["top", "left", "bottom"])
    assert resolve_record_cameras([flir(), pupil()], "all") == (None, ["top", "back"])


def test_all_on_unnamed_cameras_records_them_all():
    shorthand = CameraGroup(name="flir_bonsai", backend="flir_bonsai")
    assert resolve_record_cameras([shorthand], "all") == (None, None)


def test_all_keeps_a_multi_camera_workflow():
    assert resolve_record_cameras([flir(workflow="run-flir-flycap-2cam")], "all") == (
        None,
        ["top", "back"],
    )


def test_record_cameras_without_video_cameras_is_an_error():
    for groups in ([], [pupil()]):
        with pytest.raises(CameraSelectionError, match="no cameras that record"):
            resolve_record_cameras(groups, ["top"])


def test_rce_unnamed_agent_refuses_a_list(tmp_path):
    cfg = write_ensemble(tmp_path / "e.yaml", {"a": "top", "b": ""})
    with pytest.raises(CameraSelectionError, match=r"unnamed: \['b'\]"):
        resolve_record_cameras([rce_group(cfg)], ["top"])


def test_rce_unnamed_agent_drops_all_instead_of_a_partial_list(tmp_path):
    cfg = write_ensemble(tmp_path / "e.yaml", {"a": "top", "b": ""})
    assert resolve_record_cameras([rce_group(cfg)], "all") == (None, None)


# --- evaluate: what the task receives ---


def _args(groups, patched, source="setup:rig.yaml"):
    return {
        "task": "sequence",
        "camera_groups": groups,
        "cameras_config": groups[0] if groups else None,
        "settings.task.patched": patched,
        "settings.task.provenance": {"record_cameras": source},
    }


def test_evaluate_narrows_groups_and_keeps_provenance(rce):
    patched = {"record_cameras": "top"}
    args = _args([rce], patched, source="cli")
    _resolve_record_cameras(args, patched)
    assert [g.unit_names for g in args["camera_groups"]] == [["top"]]
    assert args["cameras_config"].unit_names == ["top"]
    assert patched["record_cameras"] == ["top"]
    assert args["settings.task.provenance"]["record_cameras"] == "cli"


def test_evaluate_all_records_the_names_and_keeps_the_groups(rce):
    patched = {"record_cameras": "all"}
    args = _args([rce], patched)
    _resolve_record_cameras(args, patched)
    assert args["camera_groups"] == [rce]
    assert patched["record_cameras"] == ["top", "left", "bottom"]
    assert args["settings.task.provenance"]["record_cameras"] == "setup:rig.yaml"


def test_evaluate_all_on_unnamed_cameras_is_dropped():
    shorthand = CameraGroup(name="flir_bonsai", backend="flir_bonsai")
    patched = {"record_cameras": "all"}
    args = _args([shorthand], patched)
    _resolve_record_cameras(args, patched)
    assert args["camera_groups"] == [shorthand]
    assert "record_cameras" not in patched
    assert "record_cameras" not in args["settings.task.provenance"]


@pytest.mark.parametrize("patched", [{}, {"record_cameras": None}])
def test_evaluate_unset_records_all_cameras_as_before(rce, patched):
    args = _args([rce], patched)
    _resolve_record_cameras(args, patched)
    assert args["camera_groups"] == [rce]
    assert patched.get("record_cameras") is None


@pytest.mark.parametrize(
    "value, match",
    [(["front"], "unknown camera"), ([], "empty"), ("", "empty"), (3, "camera names")],
)
def test_evaluate_refuses_bad_values_before_hardware(rce, value, match):
    patched = {"record_cameras": value}
    with pytest.raises(CameraSelectionError, match=match):
        _resolve_record_cameras(_args([rce], patched), patched)


@pytest.mark.parametrize("value", [["front"], "all", ["top"]])
def test_evaluate_ignores_record_cameras_without_video(rce, value):
    """With record_video off the choice is dropped unchecked, even an unknown name."""
    patched = {"record_video": False, "record_cameras": value}
    args = _args([rce], patched)
    _resolve_record_cameras(args, patched)
    assert args["camera_groups"] == [rce]
    assert "record_cameras" not in patched
    assert "record_cameras" not in args["settings.task.provenance"]


def test_evaluate_ignores_record_cameras_without_video_or_cameras():
    patched = {"record_video": False, "record_cameras": ["top"]}
    args = {
        "task": "sequence",
        "settings.task.patched": patched,
        "settings.task.provenance": {"record_cameras": "subject:s.yaml"},
    }
    _resolve_record_cameras(args, patched)
    assert "record_cameras" not in patched
