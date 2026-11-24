"""``msw doctor`` over a tmp config dir, fake serial ports and a fake camera backend."""

from __future__ import annotations

import json
import urllib.error

import pytest
import yaml
from msw_plugin_api.camera import UNIT_READY, UnitResult

from murineshiftwork.cli import doctor as doctor_mod
from murineshiftwork.cli.doctor import _oe_status_url, doctor
from murineshiftwork.cli.parser import parse_args
from murineshiftwork.logic import machine_config


class FakeBackend:
    def __init__(self) -> None:
        self.acq_type, self.version = "video_flir", "1.0"
        self.units: list = []

    def configure(self, settings, units):
        self.units = list(units)

    def preflight(self):
        return [UnitResult(name=u.name, ok=True, state=UNIT_READY) for u in self.units]

    def start(self, target):
        return []

    def stop(self, reason):
        return []

    def close(self):
        pass


class _EntryPoint:
    name, value = "flir_bonsai", "fake:flir_bonsai"

    def load(self):
        return FakeBackend


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        machine_config, "_MACHINE_CONFIG_FILE", tmp_path / "msw_machine.yaml"
    )
    monkeypatch.setenv("MSW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MSW_CONFIG_DIR", raising=False)
    monkeypatch.delenv("BONSAI_EXE", raising=False)
    monkeypatch.setattr(
        "msw_plugin_api.camera.entry_points",
        lambda group: [_EntryPoint()] if group == "msw.camera" else [],
    )
    monkeypatch.setattr(
        "murineshiftwork.cli.preflight.test_serial_port_is_accessible",
        lambda port: port == "COM3",
    )


@pytest.fixture
def cfg(tmp_path):
    setup = {
        "name": "rig",
        "devices": {"bpod": {"type": "bpod", "port": "COM3"}},
        "cameras": [
            {"name": "behaviour", "backend": "flir_bonsai", "units": [{"name": "top"}]}
        ],
        "calibrations": {
            "bpod_valve": {"1": {"updated": "", "points": [[0.01, 1.0], [0.05, 5.0]]}}
        },
    }
    (tmp_path / "cfg" / "setups").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "cfg" / "setups" / "rig.yaml").write_text(yaml.safe_dump(setup))
    (tmp_path / "msw_machine.yaml").write_text(yaml.safe_dump({"data_dir": "x"}))
    return tmp_path / "cfg"


def _report(cfg, capsys, **kw):
    code = doctor("rig", config_dir=str(cfg), as_json=True, **kw)
    data = json.loads(capsys.readouterr().out)
    return code, {c["name"]: c for c in data["checks"]}, data


def test_all_ok(cfg, capsys):
    code, checks, data = _report(cfg, capsys)
    assert code == 0 and data["ok"]
    assert checks["config validate"]["status"] == "ok"
    assert checks["devices"]["message"].startswith("opened bpod")
    assert checks["cameras"]["message"] == "1/1 camera(s) ready"
    assert checks["open ephys"]["status"] == "skip"
    assert "msw-core" in checks["packages"]["message"]
    assert checks["machine config"]["status"] == "ok"


def test_skip_hardware(cfg, capsys):
    code, checks, _ = _report(cfg, capsys, skip_hardware=True)
    assert code == 0
    for name in ("devices", "cameras", "open ephys"):
        assert checks[name]["status"] == "skip"


def test_device_not_accessible_fails(cfg, capsys, monkeypatch):
    monkeypatch.setattr(
        "murineshiftwork.cli.preflight.test_serial_port_is_accessible",
        lambda port: False,
    )
    code, checks, _ = _report(cfg, capsys)
    assert code == 1
    assert checks["devices"]["status"] == "fail"
    assert "bpod serial port not accessible: 'COM3'" in checks["devices"]["message"]


def test_invalid_setup_fails_and_skips_hardware(cfg, capsys):
    (cfg / "setups" / "rig.yaml").write_text("name: rig\ndevices: {b: {type: bpod}}\n")
    code, checks, _ = _report(cfg, capsys)
    assert code == 1
    assert checks["config validate"]["status"] == "fail"
    assert checks["devices"] == {
        "name": "devices",
        "status": "skip",
        "message": "setup config invalid",
    }


def test_unknown_setup_exits_2(cfg, capsys):
    assert doctor("nope", config_dir=str(cfg)) == 2
    out = capsys.readouterr().out
    assert "fail  config validate" in out and "setup not found" in out


def test_broken_machine_config_fails(cfg, capsys, tmp_path):
    (tmp_path / "msw_machine.yaml").write_text("- a list\n")
    code, checks, _ = _report(cfg, capsys, skip_hardware=True)
    assert code == 1 and checks["machine config"]["status"] == "fail"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, n):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup_oe(cfg, url):
    path = cfg / "setups" / "rig.yaml"
    data = yaml.safe_load(path.read_text())
    data["open_ephys_url"] = url
    path.write_text(yaml.safe_dump(data))


def test_open_ephys_reachable(cfg, capsys, monkeypatch):
    _setup_oe(cfg, "oe-host")
    seen = {}

    def fake_urlopen(url, timeout):
        seen.update(url=url, timeout=timeout)
        return _Resp(b'{"mode": "IDLE"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    code, checks, _ = _report(cfg, capsys)
    assert code == 0
    assert seen == {"url": "http://oe-host:37497/api/status", "timeout": 3.0}
    assert checks["open ephys"]["message"].endswith("reachable, mode IDLE")


@pytest.mark.parametrize("from_setup, status", [(True, "fail"), (False, "warn")])
def test_open_ephys_unreachable(cfg, capsys, monkeypatch, tmp_path, from_setup, status):
    if from_setup:
        _setup_oe(cfg, "http://oe-host:1234")
    else:
        (tmp_path / "msw_machine.yaml").write_text("openephys_api_url: oe-host\n")

    def refuse(url, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    code, checks, _ = _report(cfg, capsys)
    assert checks["open ephys"]["status"] == status
    assert code == (1 if from_setup else 0)


def test_oe_status_url():
    assert _oe_status_url("h") == "http://h:37497/api/status"
    assert _oe_status_url("http://h:1/") == "http://h:1/api/status"


def test_text_output(cfg, capsys):
    assert doctor("rig", config_dir=str(cfg), skip_hardware=True) == 0
    out = capsys.readouterr().out
    assert "ok    python" in out and "skip  cameras" in out
    assert out.rstrip().endswith("setup 'rig': 0 fail, 0 warn")


def test_parser():
    args = parse_args(["doctor", "--setup", "rig", "--skip-hardware", "--json"])
    assert args["func"] is doctor_mod.run_doctor
    assert args["setup"] == "rig" and args["skip_hardware"] and args["as_json"]
