"""CLI handlers called directly: exit codes, files written and error paths.

The parser tests only check that a command line maps to a handler; these run the handler on a
temporary tree with the hardware and the machine-local config replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from murineshiftwork.cli import post
from murineshiftwork.cli.config import run_config_upgrade
from murineshiftwork.cli.execute import (
    run_action,
    run_calibration,
    run_init,
    run_setup,
)
from murineshiftwork.cli.post import run_post_clean
from murineshiftwork.cli.tasks import run_tasks_defaults, run_tasks_init_configs


@pytest.fixture
def machine_cfg(tmp_path, monkeypatch):
    """Redirect the machine-local config away from the user's home and clear env layers."""
    path = tmp_path / "home" / "msw_machine.yaml"
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._MACHINE_CONFIG_FILE", path
    )
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config._HISTORICAL_DEFAULT",
        tmp_path / "no_such_default",
    )
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    return path


# init


def test_init_builds_tree_and_writes_machine_config(tmp_path, machine_cfg):
    cfg = tmp_path / "cfg"
    data = tmp_path / "data"
    run_init(config_dir=str(cfg), data_dir=str(data))
    for sub in ("setups", "subjects", "tasks", "device_configs/cameras"):
        assert (cfg / sub).is_dir()
    assert data.is_dir()
    written = yaml.safe_load(machine_cfg.read_text())
    assert written["config_dir"] == str(cfg.resolve())
    assert written["data_dir"] == str(data.resolve())


def test_init_into_non_empty_directory_keeps_existing_files(tmp_path, machine_cfg):
    cfg = tmp_path / "cfg"
    (cfg / "setups").mkdir(parents=True)
    (cfg / "setups" / "rig.yaml").write_text("name: rig\n")
    (cfg / "notes.txt").write_text("keep")
    run_init(config_dir=str(cfg))
    assert (cfg / "setups" / "rig.yaml").read_text() == "name: rig\n"
    assert (cfg / "notes.txt").read_text() == "keep"
    assert (cfg / "subjects").is_dir()


def test_init_keeps_other_machine_config_keys(tmp_path, machine_cfg):
    machine_cfg.parent.mkdir(parents=True)
    machine_cfg.write_text("logagent_api_url: http://example.invalid\n")
    run_init(config_dir=str(tmp_path / "cfg"))
    written = yaml.safe_load(machine_cfg.read_text())
    assert written["logagent_api_url"] == "http://example.invalid"
    assert written["config_dir"] == str((tmp_path / "cfg").resolve())


def test_init_onto_a_file_fails(tmp_path, machine_cfg):
    blocker = tmp_path / "cfg"
    blocker.write_text("a file, not a directory")
    with pytest.raises(OSError):
        run_init(config_dir=str(blocker))
    assert not machine_cfg.exists()


# action


class FakeFactory:
    opened = False

    def __init__(self, serial_port="", **kwargs):
        self.serial_port = serial_port
        FakeFactory.instances.append(self)

    instances: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def open(self):
        FakeFactory.opened = True


class FakeDriver:
    requests: list = []

    def __init__(self, bpod):
        self.bpod = bpod

    def dispatch(self, request):
        FakeDriver.requests.append(request)


@pytest.fixture
def fake_bpod(monkeypatch):
    FakeFactory.instances = []
    FakeFactory.opened = False
    FakeDriver.requests = []
    monkeypatch.setattr(
        "murineshiftwork.hardware.bpod.factory.BpodFactory", FakeFactory
    )
    monkeypatch.setattr(
        "murineshiftwork.hardware.bpod.actions.BpodActionDriver", FakeDriver
    )


@pytest.fixture
def setup_cfg_dir(tmp_path):
    run_setup(subcommand="create", setup_name="rigX", config_dir=str(tmp_path))
    return tmp_path


def test_action_dispatches_parsed_params_to_the_bpod_driver(setup_cfg_dir, fake_bpod):
    run_action(
        setup="rigX",
        device="bpod",
        action="pulse",
        params=["duration=0.5", "port=3", "label=a"],
        config_dir=str(setup_cfg_dir),
        serial_port_bpod="PORT_X",
    )
    (req,) = FakeDriver.requests
    assert req.action == "pulse"
    assert req.params == {"duration": 0.5, "port": 3, "label": "a"}
    assert FakeFactory.instances[0].serial_port == "PORT_X"
    assert FakeFactory.opened


def test_action_unknown_setup_raises_before_touching_hardware(tmp_path, fake_bpod):
    with pytest.raises(ValueError, match="not found"):
        run_action(setup="missing", device="bpod", action="x", config_dir=str(tmp_path))
    assert FakeFactory.instances == []


def test_action_unknown_device_lists_available(setup_cfg_dir, fake_bpod):
    with pytest.raises(KeyError, match="Available devices"):
        run_action(
            setup="rigX", device="nope", action="x", config_dir=str(setup_cfg_dir)
        )
    assert FakeFactory.instances == []


def test_action_driver_error_propagates(setup_cfg_dir, fake_bpod, monkeypatch):
    def boom(self, request):
        raise RuntimeError("device refused")

    monkeypatch.setattr(FakeDriver, "dispatch", boom)
    with pytest.raises(RuntimeError, match="device refused"):
        run_action(
            setup="rigX", device="bpod", action="x", config_dir=str(setup_cfg_dir)
        )


# calibration


@pytest.fixture
def calibration_calls(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("murineshiftwork.logic.log.setup_logging", lambda **kw: None)

    def install(saved):
        def fake(**kwargs):
            calls.append(kwargs)
            return saved

        monkeypatch.setattr(
            "murineshiftwork.logic.calibration.save_calibration_pdfs", fake
        )
        return calls

    return install


def test_calibration_prints_each_saved_pdf(tmp_path, calibration_calls, capsys):
    calls = calibration_calls(["a.pdf", "b.pdf"])
    run_calibration(
        config_dir=str(tmp_path), output_dir=str(tmp_path / "out"), setup="r"
    )
    out = capsys.readouterr().out
    assert "Saved: a.pdf" in out and "Saved: b.pdf" in out
    assert calls == [
        {
            "config_dir": str(tmp_path),
            "setup_name": "r",
            "output_dir": str(tmp_path / "out"),
        }
    ]


def test_calibration_without_data_says_so_and_returns(
    tmp_path, calibration_calls, capsys
):
    calls = calibration_calls([])
    run_calibration(config_dir=str(tmp_path))
    assert "No calibration PDFs saved" in capsys.readouterr().out
    assert calls[0]["setup_name"] is None


def test_calibration_writer_error_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr("murineshiftwork.logic.log.setup_logging", lambda **kw: None)

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("murineshiftwork.logic.calibration.save_calibration_pdfs", fail)
    with pytest.raises(OSError, match="disk full"):
        run_calibration(config_dir=str(tmp_path))


# post clean


def _csv(path: Path, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r + "\n" for r in rows))
    return path


def test_post_clean_removes_event_rows_and_backs_up(tmp_path):
    f = _csv(tmp_path / "s1" / "a.msw.csv", ["t,Port1", "t,Port4", "t,Port2"])
    untouched = _csv(tmp_path / "s2" / "b.msw.csv", ["t,Port1"])
    run_post_clean(data_dir=str(tmp_path))
    assert f.read_text() == "t,Port1\nt,Port2\n"
    assert untouched.read_text() == "t,Port1\n"
    backups = list(tmp_path.rglob("*.bak.*"))
    assert len(backups) == 1  # only the modified file is backed up
    assert backups[0].read_text() == "t,Port1\nt,Port4\nt,Port2\n"


def test_post_clean_dry_run_changes_nothing(tmp_path):
    f = _csv(tmp_path / "a.msw.csv", ["t,Port4"])
    run_post_clean(data_dir=str(tmp_path), dry_run=True)
    assert f.read_text() == "t,Port4\n"
    assert list(tmp_path.rglob("*.bak.*")) == []


def test_post_clean_custom_event(tmp_path):
    f = _csv(tmp_path / "a.msw.csv", ["t,Port4", "t,Port7"])
    run_post_clean(data_dir=str(tmp_path), event="Port7")
    assert f.read_text() == "t,Port4\n"


def test_post_clean_missing_data_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="data-dir not found"):
        run_post_clean(data_dir=str(tmp_path / "absent"))


def test_post_clean_ignores_other_files(tmp_path):
    other = _csv(tmp_path / "a.csv", ["t,Port4"])
    run_post_clean(data_dir=str(tmp_path))
    assert other.read_text() == "t,Port4\n"


# post run was removed


def test_post_run_subcommand_is_gone():
    import argparse

    from murineshiftwork.cli.parser import make_subparser_post

    root = argparse.ArgumentParser()
    make_subparser_post(root.add_subparsers())
    with pytest.raises(SystemExit):
        root.parse_args(["post", "run"])
    assert not hasattr(post, "run_post_run")


# tasks defaults / init-configs


def test_tasks_defaults_prints_bundled_yaml(fake_tasks, capsys):
    run_tasks_defaults(task="demo_sequence")
    out = capsys.readouterr().out
    assert out.startswith("# demo_sequence")
    assert "iti: 1.0" in out


def test_tasks_defaults_without_name_exits_1(fake_tasks, capsys):
    with pytest.raises(SystemExit) as info:
        run_tasks_defaults()
    assert info.value.code == 1
    assert "task name required" in capsys.readouterr().err


def test_tasks_defaults_unknown_task_exits_1_and_lists(fake_tasks, capsys):
    with pytest.raises(SystemExit) as info:
        run_tasks_defaults(task="zzz_none")
    assert info.value.code == 1
    assert "demo_sequence" in capsys.readouterr().err


def test_tasks_defaults_task_without_yaml_exits_1(fake_tasks, capsys):
    (fake_tasks["demo_sequence"] / "task.yaml").unlink()
    with pytest.raises(SystemExit) as info:
        run_tasks_defaults(task="demo_sequence")
    assert info.value.code == 1
    assert "no task.yaml" in capsys.readouterr().err


def test_init_configs_writes_overlays_for_all_tasks(fake_tasks, tmp_path, capsys):
    run_tasks_init_configs(config_dir=str(tmp_path))
    for name in fake_tasks:
        assert (tmp_path / "tasks" / name / "task.yaml").is_file()
    assert capsys.readouterr().out.count("wrote") == len(fake_tasks)


def test_init_configs_skips_existing_without_force(fake_tasks, tmp_path, capsys):
    dst = tmp_path / "tasks" / "demo_sequence" / "task.yaml"
    dst.parent.mkdir(parents=True)
    dst.write_text("default:\n  iti: 99\n")
    run_tasks_init_configs(tasks=["demo_sequence"], config_dir=str(tmp_path))
    assert "iti: 99" in dst.read_text()
    assert "use --force" in capsys.readouterr().out


def test_init_configs_force_overwrites(fake_tasks, tmp_path):
    dst = tmp_path / "tasks" / "demo_sequence" / "task.yaml"
    dst.parent.mkdir(parents=True)
    dst.write_text("default:\n  iti: 99\n")
    run_tasks_init_configs(
        tasks=["demo_sequence"], config_dir=str(tmp_path), force=True
    )
    assert yaml.safe_load(dst.read_text())["default"]["iti"] == 1.0


def test_init_configs_unknown_task_is_skipped(fake_tasks, tmp_path, capsys):
    run_tasks_init_configs(tasks=["zzz_none"], config_dir=str(tmp_path))
    assert "not found" in capsys.readouterr().out
    assert not (tmp_path / "tasks").exists()


def test_init_configs_without_config_dir_exits_1(fake_tasks, machine_cfg, capsys):
    with pytest.raises(SystemExit) as info:
        run_tasks_init_configs()
    assert info.value.code == 1
    assert "config_dir not set" in capsys.readouterr().err


# config upgrade


def _overlay(cfg: Path, name: str, content: dict) -> Path:
    p = cfg / "tasks" / name / "task.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(content))
    return p


def test_upgrade_adds_missing_keys_and_keeps_user_values(fake_tasks, tmp_path):
    p = _overlay(
        tmp_path, "demo_sequence", {"default": {}, "mode": {"fast": {"iti": 7}}}
    )
    run_config_upgrade(
        kind="task", name="demo_sequence", config_dir=str(tmp_path), yes=True
    )
    merged = yaml.safe_load(p.read_text())
    assert merged["default"]["iti"] == 1.0
    assert merged["mode"]["fast"]["iti"] == 7
    assert len(list(p.parent.glob("task.yaml.bak.*"))) == 1


def test_upgrade_dry_run_writes_nothing(fake_tasks, tmp_path, capsys):
    p = _overlay(tmp_path, "demo_sequence", {"default": {}})
    before = p.read_text()
    run_config_upgrade(
        kind="task", name="demo_sequence", config_dir=str(tmp_path), dry_run=True
    )
    assert p.read_text() == before
    assert "would update 1 overlay" in capsys.readouterr().out


def test_upgrade_declined_prompt_changes_nothing(
    fake_tasks, tmp_path, monkeypatch, capsys
):
    p = _overlay(tmp_path, "demo_sequence", {"default": {}})
    before = p.read_text()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    run_config_upgrade(kind="task", name="demo_sequence", config_dir=str(tmp_path))
    assert p.read_text() == before
    assert "updated 0 overlay" in capsys.readouterr().out


def test_upgrade_up_to_date_overlay_is_left_alone(fake_tasks, tmp_path, capsys):
    bundled = yaml.safe_load((fake_tasks["demo_sequence"] / "task.yaml").read_text())
    p = _overlay(tmp_path, "demo_sequence", bundled)
    run_config_upgrade(
        kind="task", name="demo_sequence", config_dir=str(tmp_path), yes=True
    )
    assert yaml.safe_load(p.read_text()) == bundled
    assert "up to date" in capsys.readouterr().out
    assert list(p.parent.glob("*.bak.*")) == []


def test_upgrade_all_without_overlays_reports_none(fake_tasks, tmp_path, capsys):
    run_config_upgrade(all=True, config_dir=str(tmp_path))
    assert "No task overlays found" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["setup", "subject"])
def test_upgrade_setup_and_subject_exit_2(tmp_path, capsys, kind):
    with pytest.raises(SystemExit) as info:
        run_config_upgrade(kind=kind, name="x", config_dir=str(tmp_path))
    assert info.value.code == 2
    assert "not implemented" in capsys.readouterr().err


def test_upgrade_without_kind_prints_usage_and_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as info:
        run_config_upgrade(config_dir=str(tmp_path))
    assert info.value.code == 2
    assert "Usage" in capsys.readouterr().err


def test_upgrade_without_config_dir_exits_1(machine_cfg, capsys):
    with pytest.raises(SystemExit) as info:
        run_config_upgrade(kind="task", name="x")
    assert info.value.code == 1
    assert "config_dir not set" in capsys.readouterr().err
