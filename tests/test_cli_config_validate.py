"""``msw config validate`` over tmp config dirs."""

from __future__ import annotations

import json
from datetime import date

import pytest
import yaml

from murineshiftwork.cli.config_validate import config_validate
from murineshiftwork.cli.parser import parse_args
from murineshiftwork.logic.config import SUBJECT_CONFIG_SCHEMA_VERSION


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)


def _good_setup(name="rig") -> dict:
    return {
        "name": name,
        "devices": {"bpod": {"type": "bpod", "port": "COM3"}},
        "calibrations": {
            "bpod_valve": {
                "1": {
                    "updated": date.today().isoformat(),
                    "points": [[0.01, 1.0], [0.05, 5.0]],
                }
            }
        },
    }


@pytest.fixture
def cfg(tmp_path):
    _write(tmp_path / "setups" / "rig.yaml", _good_setup())
    return tmp_path


def _run(cfg, capsys, **kw):
    code = config_validate(config_dir=str(cfg), as_json=True, **kw)
    return code, json.loads(capsys.readouterr().out)


def _findings(report, name):
    f = next(f for f in report["files"] if f["name"] == name)
    return [(x["level"], x["message"]) for x in f["findings"]]


def test_valid_setup_exits_0(cfg, capsys):
    code, report = _run(cfg, capsys, setup="rig")
    assert code == 0 and report["ok"]
    assert _findings(report, "rig") == []


def test_model_error_exits_1(cfg, capsys):
    _write(
        cfg / "setups" / "bad.yaml", {"name": "bad", "devices": {"b": {"type": "bpod"}}}
    )
    code, report = _run(cfg, capsys)
    assert code == 1
    [(level, msg)] = _findings(report, "bad")
    assert level == "error" and "exactly one of 'port'" in msg
    assert _findings(report, "rig") == []


def test_unreadable_yaml_is_an_error(cfg, capsys):
    _write(cfg / "setups" / "broken.yaml", "name: [unclosed\n")
    code, report = _run(cfg, capsys)
    assert code == 1
    assert _findings(report, "broken")[0][0] == "error"


def test_setup_warnings_pass_unless_strict(cfg, capsys):
    setup = _good_setup("other")  # name differs from the file name
    setup["calibrations"] = {}
    _write(cfg / "setups" / "rig.yaml", setup)
    code, report = _run(cfg, capsys, setup="rig")
    assert code == 0
    messages = [m for _, m in _findings(report, "rig")]
    assert any("does not match the file name" in m for m in messages)
    assert any("no calibrations.bpod_valve" in m for m in messages)
    code, _ = _run(cfg, capsys, setup="rig", strict=True)
    assert code == 1


def test_stale_calibration_is_a_warning(cfg, capsys):
    setup = _good_setup()
    setup["calibrations"]["bpod_valve"]["1"]["updated"] = "2000-01-01"
    _write(cfg / "setups" / "rig.yaml", setup)
    _, report = _run(cfg, capsys, setup="rig")
    [(level, msg)] = _findings(report, "rig")
    assert level == "warning" and "older than stale_days" in msg


def test_unknown_task_override(cfg, capsys, fake_tasks):
    setup = _good_setup()
    setup["task_overrides"] = {"demo_sequence": {"iti": 2}, "typo_task": {"x": 1}}
    _write(cfg / "setups" / "rig.yaml", setup)
    _, report = _run(cfg, capsys, setup="rig")
    assert _findings(report, "rig") == [
        ("warning", "task_overrides.typo_task: no such task installed here")
    ]


def test_check_ports(cfg, capsys):
    setup = _good_setup()
    setup["devices"] = {
        "bpod": {"type": "bpod", "port_by_path": "pci-nowhere"},
        "pulsepal": {"type": "pulsepal", "port": "/dev/ttyNOPE99"},
        "scale": {"type": "scale", "port": "COM7"},  # COM ports cannot be checked
    }
    _write(cfg / "setups" / "rig.yaml", setup)
    code, report = _run(cfg, capsys, setup="rig")
    assert (
        code == 0 and "device ports not checked (pass --check-ports)" in report["notes"]
    )
    code, report = _run(cfg, capsys, setup="rig", check_ports=True)
    assert code == 1
    messages = [m for _, m in _findings(report, "rig")]
    assert len(messages) == 2
    assert any(m.startswith("devices.bpod: Serial device not found") for m in messages)
    assert any(
        "devices.pulsepal: port '/dev/ttyNOPE99' not present" in m for m in messages
    )


def test_check_ports_only_for_the_given_setup(cfg, capsys):
    # another machine's setup in the shared config dir: its port is absent here
    other = _good_setup("other")
    other["devices"] = {"bpod": {"type": "bpod", "port": "/dev/ttyNOPE99"}}
    _write(cfg / "setups" / "other.yaml", other)
    code, report = _run(cfg, capsys, check_ports=True)
    assert code == 0
    assert _findings(report, "other") == []
    assert any("pass --setup <setup> --check-ports" in n for n in report["notes"])
    code, report = _run(cfg, capsys, setup="other", check_ports=True)
    assert code == 1


def test_subjects(cfg, capsys):
    current = SUBJECT_CONFIG_SCHEMA_VERSION
    subjects = cfg / "subjects"
    _write(subjects / "ok.yaml", {"schema_version": current, "name": "ok"})
    _write(subjects / "legacy.yaml", {"name": "legacy"})
    _write(subjects / "old.yaml", {"schema_version": 1, "name": "old"})
    _write(subjects / "future.yaml", {"schema_version": current + 1, "name": "future"})
    _write(subjects / "noname.yaml", {"schema_version": current})
    code, report = _run(cfg, capsys, subjects=True)
    assert code == 1
    assert _findings(report, "ok") == []
    assert "schema_version missing" in _findings(report, "legacy")[0][1]
    assert _findings(report, "old")[0][0] == "warning"
    assert "newer than this software" in _findings(report, "future")[-1][1]
    assert _findings(report, "noname") == [("error", "name: Field required")]


def test_subjects_skipped_without_flag(cfg, capsys):
    _write(cfg / "subjects" / "x.yaml", {"name": "x"})
    _, report = _run(cfg, capsys)
    assert [f["kind"] for f in report["files"]] == ["setup"]


@pytest.mark.parametrize("kw", [{"setup": "nope"}, {"config_dir": "/no/such/dir"}])
def test_usage_errors_exit_2(cfg, capsys, kw):
    kw.setdefault("config_dir", str(cfg))
    assert config_validate(**kw) == 2
    assert "config validate:" in capsys.readouterr().err


def test_empty_config_dir_exits_2(tmp_path):
    assert config_validate(config_dir=str(tmp_path)) == 2


def test_text_output(cfg, capsys):
    _write(
        cfg / "setups" / "bad.yaml", {"name": "bad", "devices": {"b": {"type": "x"}}}
    )
    assert config_validate(config_dir=str(cfg)) == 1
    out = capsys.readouterr().out
    assert "error    setup    bad" in out and "ok       setup    rig" in out
    assert "2 file(s): 1 with errors, 0 with warnings only" in out


def test_parser():
    args = parse_args(["config", "validate", "--setup", "rig", "--subjects", "--json"])
    assert args["setup"] == "rig" and args["subjects"] and args["as_json"]
    assert not args["check_ports"] and not args["strict"]
    with pytest.raises(SystemExit):
        parse_args(["config", "validate", "--setup", "rig", "--all"])
