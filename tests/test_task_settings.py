"""Task-settings resolution: priority chain, per-leaf provenance, dotted CLI keys, no sticky modes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from murineshiftwork.logic.task_settings import (
    LAYER_CLI,
    LAYER_INJECTED,
    StickyTaskModeError,
    build_task_settings,
    merge_task_modes,
    resolve_task_settings,
)

BUNDLED = {
    "iti": 0.4,
    "stop": {"max_trials": {"value": 1500, "action": "stop"}},
    "perturb": {"enabled": False, "distribution": {}},
}
OVERLAY = {"iti": 0.6}
MODES = {"probe": {"perturb": {"enabled": True, "distribution": {"4": 0.1}}}}


def _subject(overrides: dict):
    return SimpleNamespace(task_overrides={"seq": overrides})


def _resolve(**kw):
    args = dict(
        task_name="seq",
        default_layers=[("task.yaml", BUNDLED), ("overlay", OVERLAY)],
        task_modes=MODES,
    )
    args.update(kw)
    return resolve_task_settings(**args)


def test_provenance_attributes_each_leaf_to_its_layer():
    r = _resolve(
        task_mode="probe",
        subject_config=_subject({"stop": {"max_trials": {"value": 300}}}),
        subject_label="subject:M1.yaml",
        cli_overrides=["perturb.enabled=False"],
        extra_injections={"config_dir": "/cfg", "iti": 99},
    )
    assert r.mode == "probe"
    assert r.settings["iti"] == 0.6  # injection never overrides a present key
    assert r.settings["stop"]["max_trials"] == {"value": 300, "action": "stop"}
    assert r.settings["perturb"] == {"enabled": False, "distribution": {"4": 0.1}}
    assert r.provenance == {
        "iti": "overlay",
        "stop.max_trials.value": "subject:M1.yaml",
        "stop.max_trials.action": "task.yaml",
        "perturb.enabled": LAYER_CLI,
        "perturb.distribution.4": "mode:probe",
        "config_dir": LAYER_INJECTED,
    }


def test_replaced_subtree_drops_stale_child_provenance():
    r = _resolve(cli_overrides=["stop={'max_trials': 5}"])
    assert r.settings["stop"] == {"max_trials": 5}
    assert r.provenance["stop.max_trials"] == LAYER_CLI
    assert "stop.max_trials.value" not in r.provenance


def test_dotted_cli_key_sets_nested_leaf_and_literal_dotted_key_is_kept():
    r = _resolve(
        default_layers=[("task.yaml", {**BUNDLED, "settings.stage": {"x": 1}})],
        cli_overrides=["stop.max_trials.value=20", "settings.stage=None"],
    )
    assert r.settings["stop"]["max_trials"] == {"value": 20, "action": "stop"}
    assert r.settings["settings.stage"] is None


def test_sticky_task_mode_in_subject_config_is_rejected():
    with pytest.raises(StickyTaskModeError, match="task_mode = 'probe'"):
        _resolve(subject_config=_subject({"task_mode": "probe"}))


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="not found"):
        _resolve(task_mode="nope")


def test_task_mode_overrides_subject_config_for_the_same_key():
    """A mode is shorthand for a settings group applied at the CLI's position: it beats the
    subject config, the layer beneath it -- unlike a plain CLI -ts key, which never did."""
    r = _resolve(
        task_mode="probe",
        subject_config=_subject({"perturb": {"enabled": False}}),
        subject_label="subject:M1.yaml",
    )
    assert r.settings["perturb"]["enabled"] is True
    assert r.provenance["perturb.enabled"] == "mode:probe"


def test_single_cli_setting_still_overrides_the_task_mode():
    """A single -ts key is more specific than a mode, so it still wins even though both are
    CLI-sourced."""
    r = _resolve(
        task_mode="probe",
        subject_config=_subject({"perturb": {"enabled": False}}),
        cli_overrides=["perturb.enabled=False"],
    )
    assert r.settings["perturb"]["enabled"] is False
    assert r.provenance["perturb.enabled"] == LAYER_CLI


def test_overlay_modes_deep_merge_per_mode():
    merged = merge_task_modes(
        {"a": {"x": {"p": 1, "q": 2}}, "b": {"y": 1}},
        {"a": {"x": {"q": 3}}, "c": {"z": 1}},
    )
    assert merged == {"a": {"x": {"p": 1, "q": 3}}, "b": {"y": 1}, "c": {"z": 1}}


def test_build_task_settings_wrapper_returns_dict():
    out = build_task_settings(
        task_name="seq",
        settings_task_default=BUNDLED,
        task_modes=MODES,
        cli_overrides=["iti=1.0"],
    )
    assert out["iti"] == 1.0


def test_attribute_changes_labels_in_place_edits():
    from murineshiftwork.logic.task_settings import attribute_changes

    r = _resolve()
    before = {
        **r.settings,
        "stop": {"max_trials": dict(r.settings["stop"]["max_trials"])},
    }
    after = {
        **r.settings,
        "stop": {"max_trials": {"value": 7, "action": "stop"}},
        "extra": {"a": 1},
    }
    after.pop("iti")
    changed = attribute_changes(r.provenance, before, after, "hook:Fetch")
    assert sorted(changed) == ["extra", "iti", "stop.max_trials.value"]
    assert r.provenance["stop.max_trials.value"] == "hook:Fetch"
    assert r.provenance["stop.max_trials.action"] == "task.yaml"
    assert r.provenance["extra.a"] == "hook:Fetch"
    assert "iti" not in r.provenance


def test_pre_hook_changes_are_attributed():
    from murineshiftwork.hooks.runner import run_pre_hooks

    class Fetch:
        fatal = False

        def pre_run(self, ctx):
            ctx.task_settings["stop"]["max_trials"]["value"] = 42

    r = _resolve()
    ctx = SimpleNamespace(task_settings=r.settings)
    run_pre_hooks([Fetch()], ctx, provenance=r.provenance)
    assert r.settings["stop"]["max_trials"]["value"] == 42
    assert r.provenance["stop.max_trials.value"] == "hook:Fetch"
