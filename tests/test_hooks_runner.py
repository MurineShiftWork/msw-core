"""Hook discovery and execution: load, collect order, pre/post run, failure policy."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from murineshiftwork.hooks.base import SessionAbortError, TaskHook
from murineshiftwork.hooks.context import HookContext
from murineshiftwork.hooks.runner import (
    collect_hooks,
    load_hooks,
    run_post_hooks,
    run_pre_hooks,
)
from murineshiftwork.logic.task_settings import attribute_changes

HOOK_MODULE = """
from murineshiftwork.hooks.base import TaskHook

class Plain(TaskHook):
    pass

class Other(TaskHook):
    pass

class NeedsArgs(TaskHook):
    def __init__(self, required):
        pass

NOT_A_CLASS = "text"
"""


@pytest.fixture
def hook_module(tmp_path, monkeypatch):
    (tmp_path / "msw_test_hooks_mod.py").write_text(HOOK_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "msw_test_hooks_mod"


def make_ctx(**settings) -> HookContext:
    return HookContext(
        subject="s1", task_name="t", task_settings=dict(settings), session_paths={}
    )


class Recorder(TaskHook):
    """Appends '<name>.<phase>' to a shared log; optionally raises in a phase."""

    def __init__(self, name, log, fail_in=(), fatal=False):
        self.name = name
        self.log = log
        self.fail_in = fail_in
        self.fatal = fatal

    def _run(self, phase):
        self.log.append(f"{self.name}.{phase}")
        if phase in self.fail_in:
            raise ValueError(f"{self.name} {phase} boom")

    def pre_run(self, ctx):
        self._run("pre")

    def post_run(self, ctx):
        self._run("post")


# load_hooks


def test_load_hooks_instantiates_in_order(hook_module):
    hooks = load_hooks([f"{hook_module}.Other", f"{hook_module}.Plain"])
    assert [type(h).__name__ for h in hooks] == ["Other", "Plain"]


def test_load_hooks_skips_empty_paths():
    assert load_hooks(["", ""]) == []


@pytest.mark.parametrize(
    "path",
    [
        "no_such_module_xyz.Hook",
        "NoDots",
        "{mod}.Missing",
        "{mod}.NOT_A_CLASS",
        "{mod}.NeedsArgs",
    ],
)
def test_load_hooks_bad_path_warns_and_keeps_good_ones(hook_module, caplog, path):
    bad = path.format(mod=hook_module)
    with caplog.at_level(logging.WARNING):
        hooks = load_hooks([bad, f"{hook_module}.Plain"])
    assert [type(h).__name__ for h in hooks] == ["Plain"]
    assert f"Could not load hook '{bad}'" in caplog.text


# collect_hooks


def test_collect_hooks_global_before_task_specific(hook_module):
    setup = SimpleNamespace(
        hooks=SimpleNamespace(
            pre_task=[f"{hook_module}.Plain"], post_task=[f"{hook_module}.Plain"]
        )
    )
    settings = {
        "HOOKS_PRE_TASK": [f"{hook_module}.Other"],
        "HOOKS_POST_TASK": [f"{hook_module}.Other"],
    }
    pre, post = collect_hooks(setup, settings)
    assert [type(h).__name__ for h in pre] == ["Plain", "Other"]
    assert [type(h).__name__ for h in post] == ["Plain", "Other"]


@pytest.mark.parametrize(
    "setup", [None, SimpleNamespace(), SimpleNamespace(hooks=None)]
)
def test_collect_hooks_without_setup_hooks(hook_module, setup):
    pre, post = collect_hooks(setup, {"HOOKS_PRE_TASK": [f"{hook_module}.Plain"]})
    assert [type(h).__name__ for h in pre] == ["Plain"]
    assert post == []


def test_collect_hooks_none_task_settings_values():
    assert collect_hooks(None, {"HOOKS_PRE_TASK": None, "HOOKS_POST_TASK": None}) == (
        [],
        [],
    )


# run_pre_hooks


def test_pre_hooks_run_in_order():
    log: list[str] = []
    hooks = [Recorder("a", log), Recorder("b", log), Recorder("c", log)]
    run_pre_hooks(hooks, make_ctx())
    assert log == ["a.pre", "b.pre", "c.pre"]


def test_pre_hooks_empty_list_is_a_noop():
    run_pre_hooks([], make_ctx())


def test_pre_hook_changes_seen_through_context():
    class Setter(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["x"] = 2
            ctx.output["note"] = "kept"

    ctx = make_ctx(x=1)
    run_pre_hooks([Setter()], ctx)
    assert ctx.task_settings["x"] == 2
    assert ctx.output["note"] == "kept"


def test_nonfatal_pre_hook_failure_is_logged_and_skipped(caplog):
    log: list[str] = []
    hooks = [Recorder("a", log, fail_in=("pre",)), Recorder("b", log)]
    with caplog.at_level(logging.WARNING):
        run_pre_hooks(hooks, make_ctx())
    assert log == ["a.pre", "b.pre"]
    assert "Pre-hook Recorder raised (skipped): a pre boom" in caplog.text


def test_fatal_pre_hook_aborts_and_stops_remaining_hooks():
    log: list[str] = []
    hooks = [
        Recorder("a", log, fail_in=("pre",), fatal=True),
        Recorder("b", log),
    ]
    with pytest.raises(SessionAbortError, match="Fatal pre-hook Recorder") as info:
        run_pre_hooks(hooks, make_ctx())
    assert isinstance(info.value.__cause__, ValueError)
    assert log == ["a.pre"]


def test_pre_hook_raising_session_abort_propagates_even_if_nonfatal():
    class Aborter(TaskHook):
        def pre_run(self, ctx):
            raise SessionAbortError("stop now")

    with pytest.raises(SessionAbortError, match="^stop now$"):
        run_pre_hooks([Aborter()], make_ctx())


def test_pre_hook_failure_keeps_changes_made_before_the_raise():
    class Half(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["x"] = 5
            raise RuntimeError("late")

    ctx = make_ctx(x=1)
    prov: dict = {}
    run_pre_hooks([Half()], ctx, provenance=prov)
    assert ctx.task_settings["x"] == 5
    assert prov["x"] == "hook:Half"


# change attribution


def test_attribution_names_each_hook_and_only_changed_keys():
    class A(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["a"] = 10

    class B(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["nested"]["b"] = 20

    ctx = make_ctx(a=1, keep=0, nested={"b": 1, "c": 1})
    prov = {"keep": "default", "a": "default"}
    run_pre_hooks([A(), B()], ctx, provenance=prov)
    assert prov["a"] == "hook:A"
    assert prov["nested.b"] == "hook:B"
    assert prov["keep"] == "default"
    assert "nested.c" not in prov


def test_attribution_covers_new_and_removed_keys():
    class Edit(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["new"] = 1
            del ctx.task_settings["gone"]

    ctx = make_ctx(gone=1)
    prov = {"gone": "default"}
    run_pre_hooks([Edit()], ctx, provenance=prov)
    assert prov["new"] == "hook:Edit"
    assert "gone" not in prov


def test_unchanged_settings_are_not_attributed():
    ctx = make_ctx(a=1)
    prov = {"a": "default"}
    run_pre_hooks([Recorder("n", [])], ctx, provenance=prov)
    assert prov == {"a": "default"}


def test_no_provenance_means_no_snapshot(monkeypatch):
    from murineshiftwork.hooks import runner

    def boom(settings):
        raise AssertionError("snapshot taken without provenance")

    monkeypatch.setattr(runner, "_snapshot", boom)
    run_pre_hooks([Recorder("n", [])], make_ctx())


def test_unsnapshottable_settings_skip_attribution_not_the_hook():
    class Unpicklable:
        def __deepcopy__(self, memo):
            raise TypeError("no copy")

    class Setter(TaskHook):
        def pre_run(self, ctx):
            ctx.task_settings["x"] = 2

    ctx = make_ctx(x=1, handle=Unpicklable())
    prov: dict = {}
    run_pre_hooks([Setter()], ctx, provenance=prov)
    assert ctx.task_settings["x"] == 2
    assert prov == {}


def test_attribute_changes_reports_changed_dotted_keys():
    prov: dict = {}
    changed = attribute_changes(prov, {"a": {"b": 1}}, {"a": {"b": 2}}, "hook:H")
    assert changed == ["a.b"]


# run_post_hooks


def test_post_hooks_run_in_order_and_share_output():
    class Writer(TaskHook):
        def post_run(self, ctx):
            ctx.output["n"] = 1

    class Reader(TaskHook):
        def post_run(self, ctx):
            ctx.output["seen"] = ctx.output.get("n")

    ctx = make_ctx()
    run_post_hooks([Writer(), Reader()], ctx)
    assert ctx.output == {"n": 1, "seen": 1}


def test_post_hooks_empty_list_is_a_noop():
    run_post_hooks([], make_ctx())


def test_nonfatal_post_hook_failure_is_logged_and_does_not_abort(caplog):
    log: list[str] = []
    hooks = [Recorder("a", log, fail_in=("post",)), Recorder("b", log)]
    with caplog.at_level(logging.WARNING):
        run_post_hooks(hooks, make_ctx())
    assert log == ["a.post", "b.post"]
    assert "Post-hook Recorder raised (skipped): a post boom" in caplog.text


def test_fatal_post_hook_runs_remaining_hooks_then_raises():
    log: list[str] = []
    hooks = [
        Recorder("a", log, fail_in=("post",), fatal=True),
        Recorder("b", log),
    ]
    with pytest.raises(SessionAbortError, match="Fatal post-hook Recorder") as info:
        run_post_hooks(hooks, make_ctx())
    assert log == ["a.post", "b.post"]
    assert isinstance(info.value.__cause__, ValueError)


def test_first_fatal_post_hook_is_the_one_reported():
    log: list[str] = []
    hooks = [
        Recorder("a", log, fail_in=("post",), fatal=True),
        Recorder("b", log, fail_in=("post",), fatal=True),
    ]
    with pytest.raises(SessionAbortError, match="a post boom"):
        run_post_hooks(hooks, make_ctx())
    assert log == ["a.post", "b.post"]


def test_post_hook_raising_session_abort_propagates_immediately():
    log: list[str] = []

    class Aborter(TaskHook):
        def post_run(self, ctx):
            raise SessionAbortError("stop now")

    with pytest.raises(SessionAbortError, match="^stop now$"):
        run_post_hooks([Aborter(), Recorder("b", log)], make_ctx())
    assert log == []


def test_pre_then_post_ordering_with_one_instance():
    log: list[str] = []
    hook = Recorder("a", log)
    ctx = make_ctx()
    run_pre_hooks([hook], ctx)
    run_post_hooks([hook], ctx)
    assert log == ["a.pre", "a.post"]
