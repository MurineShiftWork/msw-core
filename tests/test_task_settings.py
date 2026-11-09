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
    parse_key_value_list,
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


def test_attribution_error_is_not_a_hook_failure(monkeypatch, caplog):
    from murineshiftwork.hooks import runner

    class Fatal:
        fatal = True

        def pre_run(self, ctx):
            ctx.task_settings["iti"] = 1.0

    def boom(*_a, **_kw):
        raise RuntimeError("bookkeeping")

    monkeypatch.setattr(runner, "attribute_changes", boom)
    r = _resolve()
    ctx = SimpleNamespace(task_settings=r.settings)
    runner.run_pre_hooks(
        [Fatal()], ctx, provenance=r.provenance
    )  # no SessionAbortError
    assert r.settings["iti"] == 1.0
    assert "Could not attribute" in caplog.text


def test_subject_overrides_beat_task_mode():
    """A mode is a preset below the subject config: per-animal values (e.g. a
    pswitching subject's own probabilities) survive --task-mode; -ts still wins."""
    modes = {"stage10": {"reward_amount_ul": 3.0, "probabilities": [[90, 10]]}}
    r = resolve_task_settings(
        task_name="seq",
        default_layers=[("task.yaml", {"reward_amount_ul": 2.0, "iti": 0.4})],
        task_modes=modes,
        subject_config=_subject({"reward_amount_ul": 2.5, "probabilities": [[70, 30]]}),
        subject_label="subject:P1.yaml",
        task_mode="stage10",
        cli_overrides=["iti=0.5"],
    )
    assert r.settings == {
        "reward_amount_ul": 2.5,
        "probabilities": [[70, 30]],
        "iti": 0.5,
    }
    assert r.provenance == {
        "reward_amount_ul": "subject:P1.yaml",
        "probabilities": "subject:P1.yaml",
        "iti": LAYER_CLI,
    }


def _setup(overrides: dict):
    return SimpleNamespace(task_overrides={"seq": overrides})


def test_setup_layer_sits_between_overlay_and_mode():
    """task.yaml < overlay < setup < mode < subject < CLI: each layer beats the one below."""
    r = _resolve(
        default_layers=[
            ("task.yaml", {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}),
            ("overlay", {"b": 2, "c": 2, "d": 2, "e": 2}),
        ],
        task_modes={"m": {"d": 4, "e": 4}},
        setup_config=_setup({"c": 3, "d": 3, "e": 3, "record_video": True}),
        setup_label="setup:rig1.yaml",
        task_mode="m",
        subject_config=_subject({"e": 5}),
        subject_label="subject:M1.yaml",
    )
    assert r.settings == {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "record_video": True}
    assert r.provenance == {
        "a": "task.yaml",
        "b": "overlay",
        "c": "setup:rig1.yaml",
        "d": "mode:m",
        "e": "subject:M1.yaml",
        "record_video": "setup:rig1.yaml",
    }


def test_subject_and_cli_override_setup_without_a_mode():
    r = _resolve(
        setup_config=_setup({"iti": 0.8, "stop": {"max_trials": {"value": 10}}}),
        setup_label="setup:rig1.yaml",
        subject_config=_subject({"iti": 0.9}),
        subject_label="subject:M1.yaml",
        cli_overrides=["stop.max_trials.value=20"],
    )
    assert r.settings["iti"] == 0.9
    assert r.settings["stop"]["max_trials"] == {"value": 20, "action": "stop"}
    assert r.provenance["iti"] == "subject:M1.yaml"
    assert r.provenance["stop.max_trials.value"] == LAYER_CLI
    assert r.provenance["stop.max_trials.action"] == "task.yaml"


def test_setup_nested_override_attributes_only_its_leaves():
    r = _resolve(
        setup_config=_setup({"stop": {"max_trials": {"value": 10}}}),
        setup_label="setup:rig1.yaml",
    )
    assert r.settings["stop"]["max_trials"] == {"value": 10, "action": "stop"}
    assert r.provenance["stop.max_trials.value"] == "setup:rig1.yaml"
    assert r.provenance["stop.max_trials.action"] == "task.yaml"


def test_setup_without_task_overrides_is_unchanged():
    base = _resolve(task_mode="probe")
    for setup in (
        SimpleNamespace(),  # no attribute at all
        SimpleNamespace(task_overrides={}),
        SimpleNamespace(task_overrides={"other_task": {"iti": 9}}),
    ):
        r = _resolve(task_mode="probe", setup_config=setup, setup_label="setup:x")
        assert r.settings == base.settings
        assert r.provenance == base.provenance


def test_build_task_settings_threads_setup_config():
    out = build_task_settings(
        task_name="seq",
        settings_task_default=BUNDLED,
        task_modes=MODES,
        setup_config=_setup({"iti": 0.7}),
    )
    assert out["iti"] == 0.7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("TRUE", True),
        ("null", None),
        ("False", False),
        ("2.5", 2.5),
        ("[1, 2]", [1, 2]),
        ("abc", "abc"),
    ],
)
def test_cli_values_accept_config_file_booleans(raw, expected):
    assert parse_key_value_list([f"record_video={raw}"]) == {"record_video": expected}
