"""Camera groups in the setup config: list form, the legacy single block, validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from murineshiftwork.logic.config import load_setup_config
from murineshiftwork.logic.config.models import CameraGroup, SetupConfig

DATA = Path(__file__).parent / "data"


def setup(cameras, **kw) -> SetupConfig:
    return SetupConfig(name="rig", cameras=cameras, **kw)


# --- legacy single block ---


def test_legacy_flir_block_becomes_one_group():
    s = setup(
        {
            "backend": "flir_bonsai",
            "driver": "flycap",
            "bonsai_exe": "C:/Bonsai/Bonsai.exe",
            "workflow": "run-flir-flycap-1cam",
            "cameras": [{"index": 0, "name": "top"}, {"index": 1, "name": "back"}],
        }
    )
    [g] = s.cameras
    assert (g.name, g.backend, g.role, g.record) == (
        "flir_bonsai",
        "flir_bonsai",
        "",
        "with_video",
    )
    assert g.settings == {
        "driver": "flycap",
        "bonsai_exe": "C:/Bonsai/Bonsai.exe",
        "workflow": "run-flir-flycap-1cam",
    }
    assert g.unit_names == ["top", "back"]
    assert [u.unit_settings for u in g.units] == [{"index": 0}, {"index": 1}]


def test_legacy_flir_shorthand_keeps_n_cameras_in_settings():
    [g] = setup({"backend": "flir_bonsai", "n_cameras": 2}).cameras
    assert g.units == [] and g.settings == {"n_cameras": 2}


def test_legacy_unnamed_flir_cameras_are_named_by_index():
    [g] = setup({"backend": "flir_bonsai", "cameras": [{"index": 3}]}).cameras
    assert g.unit_names == ["cam3"]


def test_legacy_rce_block_moves_config_to_settings():
    [g] = setup({"backend": "rce", "config": "cams/rig.yaml"}).cameras
    assert (g.name, g.backend, g.units, g.settings) == (
        "rce",
        "rce",
        [],
        {"config": "cams/rig.yaml"},
    )


def test_legacy_block_without_backend_is_rce():
    [g] = setup({"config": "x.yaml"}).cameras
    assert g.backend == "rce"


def test_legacy_selected_key_is_ignored():
    [g] = setup({"backend": "rce", "config": "x.yaml", "selected": ["top"]}).cameras
    assert "selected" not in g.settings


def test_legacy_unknown_key_is_refused():
    with pytest.raises(ValidationError, match="unknown keys"):
        setup({"backend": "rce", "colour": "red"})


@pytest.mark.parametrize("value", [None, []])
def test_no_cameras(value):
    assert setup(value).cameras == []
    assert SetupConfig(name="rig").cameras == []


# --- list form ---


def test_list_of_groups():
    s = setup(
        [
            {
                "name": "behaviour",
                "backend": "flir_bonsai",
                "units": [
                    {"name": "top", "hw_id": "18012744", "index": 0},
                    {"name": "back", "hw_id": "18107252"},
                ],
                "settings": {"driver": "spinnaker"},
            },
            {
                "name": "pupil",
                "backend": "rce",
                "role": "eye",
                "record": "always",
                "units": [{"name": "pupil"}],
                "settings": {"config": "cameras/pupil.yaml"},
            },
        ]
    )
    behaviour, pupil = s.cameras
    assert behaviour.units[0].hw_id == "18012744"
    assert behaviour.units[0].unit_settings == {"index": 0}
    assert (pupil.role, pupil.record) == ("eye", "always")


def test_one_group_written_without_the_list():
    [g] = setup({"name": "pupil", "backend": "rce", "units": [{"name": "eye"}]}).cameras
    assert g.name == "pupil" and g.unit_names == ["eye"]


# --- validation ---


@pytest.mark.parametrize(
    "groups, match",
    [
        (
            [{"name": "a", "backend": "x"}, {"name": "a", "backend": "y"}],
            "duplicate group names",
        ),
        (
            [
                {"name": "a", "backend": "x", "units": [{"name": "top"}]},
                {"name": "b", "backend": "y", "units": [{"name": "top"}]},
            ],
            "duplicate camera names",
        ),
        (
            [{"name": "a", "backend": "x", "units": [{"name": "u"}, {"name": "u"}]}],
            "duplicate camera names",
        ),
        (
            [{"name": "a", "backend": "x"}, {"name": "b", "backend": "x"}],
            "more than one group uses backend",
        ),
        ([{"name": "a:b", "backend": "x"}], "may not contain"),
        (
            [{"name": "a", "backend": "x", "units": [{"name": "t,b"}]}],
            "may not contain",
        ),
        ([{"name": " ", "backend": "x"}], "is empty"),
        ([{"name": "a", "backend": "x", "record": "sometimes"}], "record"),
        ([{"name": "a", "backend": "x", "colour": "red"}], "colour"),
    ],
)
def test_invalid_groups_are_refused(groups, match):
    with pytest.raises(ValidationError, match=match):
        setup(groups)


def test_camera_failure_defaults_to_raise():
    assert SetupConfig(name="rig").camera_failure == "raise"
    assert SetupConfig(name="rig", camera_failure="continue").camera_failure == (
        "continue"
    )
    with pytest.raises(ValidationError):
        SetupConfig(name="rig", camera_failure="ignore")


# --- real setups ---


def test_fixture_setups_load_as_expected():
    hotel = load_setup_config(DATA, "hotel")
    [g] = hotel.cameras
    assert (g.name, g.backend, g.unit_names) == (
        "flir_bonsai",
        "flir_bonsai",
        ["top", "back"],
    )
    assert set(g.settings) == {"driver", "bonsai_exe"}
    [g] = load_setup_config(DATA, "setup-1").cameras
    assert (g.name, g.backend, g.unit_names) == ("rce", "rce", [])
    assert g.settings == {"config": "device_configs/cameras/setup-1.cameras.yaml"}
    assert load_setup_config(DATA, "golf").cameras == []
    assert load_setup_config(DATA, "alfa").cameras == []


def _configs_dir() -> Path | None:
    """The shared config repository's checkout, when this one sits next to it."""
    env = os.environ.get("MSW_CONFIGS_DIR")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parents[2] / "msw-configs")
    return next((p for p in candidates if (p / "setups").is_dir()), None)


def _expected_group(raw: dict) -> CameraGroup:
    """What a legacy camera block converts to, built by hand from the raw YAML."""
    backend = raw.get("backend") or "rce"
    settings = {
        k: raw[k]
        for k in ("config", "driver", "bonsai_exe", "workflow", "n_cameras")
        if k in raw
    }
    units = [
        {**c, "name": c.get("name") or f"cam{c.get('index', i)}"}
        for i, c in enumerate(raw.get("cameras") or [])
    ]
    return CameraGroup(name=backend, backend=backend, units=units, settings=settings)


def test_every_shared_setup_loads():
    root = _configs_dir()
    if root is None:
        pytest.skip("shared config checkout not found (set MSW_CONFIGS_DIR)")
    paths = sorted((root / "setups").glob("*.yaml"))
    assert paths
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = load_setup_config(root, path.stem)
        assert cfg is not None, path.name
        block = raw.get("cameras")
        if not block:
            assert cfg.cameras == [], path.name
        elif isinstance(block, dict) and "units" not in block:
            assert cfg.cameras == [_expected_group(block)], path.name
        else:
            assert cfg.cameras, path.name
