"""CLI parser smoke tests: subcommand registration and argument dispatch."""

import pytest

from murineshiftwork.cli.parser import parse_args


def test_version_exits_cleanly():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["init", "/tmp/msw_cfg"],
            {"command": "init", "config_dir": "/tmp/msw_cfg"},
        ),
        (
            ["setup", "list"],
            {"command": "setup", "subcommand": "list"},
        ),
        (
            ["subject", "list"],
            {"command": "subject", "subcommand": "list"},
        ),
        (
            ["run", "--simulate"],
            {"command": "run", "simulate": True},
        ),
        (
            ["calibration", "plot"],
            {"command": "calibration", "action": "plot"},
        ),
        (
            ["tasks", "list"],
            {"command": "tasks", "subcommand": "list"},
        ),
        (
            ["tasks", "defaults", "minimal"],
            {"command": "tasks", "subcommand": "defaults", "task": "minimal"},
        ),
        (
            ["tasks", "modes", "minimal"],
            {"command": "tasks", "subcommand": "modes", "task": "minimal"},
        ),
        (
            ["post", "clean", "--data-dir", "/tmp"],
            {"command": "post", "subcommand": "clean", "data_dir": "/tmp"},
        ),
    ],
)
def test_subcommand_parses(argv, expected):
    args = parse_args(argv)
    for key, val in expected.items():
        assert args[key] == val


def test_tasks_list_func_is_callable():
    from murineshiftwork.cli.tasks import run_tasks_list

    args = parse_args(["tasks", "list"])
    assert callable(args["func"])
    assert args["func"] is run_tasks_list


def test_run_func_is_callable():
    from murineshiftwork.cli.execute import run_task

    args = parse_args(["run", "--simulate"])
    assert args["func"] is run_task


def test_tasks_list_runs_without_error(capsys):
    from murineshiftwork.cli.tasks import run_tasks_list

    run_tasks_list()
    assert isinstance(capsys.readouterr().out, str)
