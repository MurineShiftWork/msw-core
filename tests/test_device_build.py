"""Device collection + the build that selects/ports the required devices for a run."""

from __future__ import annotations

from types import SimpleNamespace

from murineshiftwork.cli import execute
from murineshiftwork.hardware.manager import DeviceCollection
from murineshiftwork.logic.run_context import DevicePorts

# --------------------------------------------------------------------------- #
# DeviceCollection: a dict of {name: handle}, plus .device() for the descriptor


def test_device_collection_is_dict_of_handles_plus_descriptor():
    dev_a = SimpleNamespace(name="bpod", handle="BPOD_HANDLE")
    dev_b = SimpleNamespace(name="scale", handle="SCALE_HANDLE")
    col = DeviceCollection([dev_a, dev_b])

    # backward-compatible: it IS a dict, so tasks' isinstance(devices, dict) guard passes
    assert isinstance(col, dict)
    assert col["bpod"] == "BPOD_HANDLE"
    assert col.get("scale") == "SCALE_HANDLE"
    assert col.get("missing") is None
    assert "bpod" in col
    assert set(col) == {"bpod", "scale"}
    # and the descriptor is reachable for the framework
    assert col.device("bpod") is dev_a
    assert col.device("missing") is None


# --------------------------------------------------------------------------- #
# _build_device_list: which devices, with which ports


class _Cfg:
    def __init__(self, type_):
        self.type = type_


class _Setup:
    def __init__(self, devices):
        self.devices = devices

    def device_port(self, name):
        return f"/cfg/{name}"


def _ctx(setup, required, ports):
    return SimpleNamespace(
        setup=setup, task_settings={"required_devices": required}, ports=ports
    )


def test_build_selects_required_and_resolves_ports(monkeypatch):
    recorded: list = []

    def fake_factory(cfg, port):
        recorded.append((cfg.type, port))
        return ("wrapper", cfg.type, port)

    monkeypatch.setattr(
        execute,
        "_DEVICE_REGISTRY",
        {"bpod": fake_factory, "pulsepal": fake_factory, "stage_tower": fake_factory},
    )
    setup = _Setup(
        {
            "bpod": _Cfg("bpod"),
            "pulsepal": _Cfg("pulsepal"),
            "stage": _Cfg("stage_tower"),
            "widget": _Cfg("widget_type"),  # no factory -> skipped
        }
    )
    # ports carries bpod/pulsepal/stage; stage resolves by NAME (type 'stage_tower' has no field)
    ports = DevicePorts(bpod="/b", pulsepal="/p", stage="/s")
    ctx = _ctx(setup, ["bpod", "pulsepal", "stage", "widget"], ports)

    out = execute._build_device_list(ctx)

    assert recorded == [("bpod", "/b"), ("pulsepal", "/p"), ("stage_tower", "/s")]
    assert len(out) == 3  # widget skipped (no factory)


def test_build_skips_devices_not_required(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(
        execute,
        "_DEVICE_REGISTRY",
        {"bpod": lambda c, p: recorded.append(("bpod", p))},
    )
    setup = _Setup({"bpod": _Cfg("bpod"), "pulsepal": _Cfg("pulsepal")})
    ctx = _ctx(setup, ["bpod"], DevicePorts(bpod="/b", pulsepal="/p"))
    execute._build_device_list(ctx)
    assert recorded == [("bpod", "/b")]  # pulsepal not in required


def test_build_port_falls_back_to_config(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        execute, "_DEVICE_REGISTRY", {"bpod": lambda c, p: seen.append(p)}
    )
    setup = _Setup({"bpod": _Cfg("bpod")})
    # ports empty -> falls back to setup_config.device_port("bpod")
    ctx = _ctx(setup, ["bpod"], DevicePorts())
    execute._build_device_list(ctx)
    assert seen == ["/cfg/bpod"]


def test_build_returns_empty_without_setup():
    ctx = _ctx(None, ["bpod"], DevicePorts(bpod="/b"))
    assert execute._build_device_list(ctx) == []


# --------------------------------------------------------------------------- #
# simulation collection


def test_build_sim_list_selects_devices_with_a_sim_factory():
    setup = _Setup(
        {
            "bpod": _Cfg("bpod"),
            "scale": _Cfg("scale"),
            "stage": _Cfg("stage_tower"),  # no sim factory -> omitted
        }
    )
    ctx = _ctx(setup, ["bpod", "scale", "stage"], DevicePorts())
    sims = execute._build_sim_device_list(ctx)
    assert sorted(d.name for d in sims) == ["bpod", "scale"]


def test_build_sim_list_defaults_to_bpod_without_setup():
    ctx = _ctx(None, [], DevicePorts())
    sims = execute._build_sim_device_list(ctx)
    assert [d.name for d in sims] == ["bpod"]


def test_sim_bpod_factory_needs_no_port():
    dev = execute._make_sim_bpod(None)
    assert dev.name == "bpod" and dev._simulate is True
    dev.preflight()  # must not raise despite having no serial port


# --------------------------------------------------------------------------- #
# real factories produce the right wrapper with the resolved port


def test_factories_build_wrappers_with_port():
    from murineshiftwork.hardware.bpod.device import BpodDevice
    from murineshiftwork.hardware.pulsepal.device import PulsePalDevice
    from murineshiftwork.hardware.scale import ScaleDevice
    from murineshiftwork.logic.config.models import ScaleDevice as ScaleCfg

    bpod = execute._make_bpod(None, "/dev/b")
    assert isinstance(bpod, BpodDevice) and bpod._serial_port == "/dev/b"

    pp = execute._make_pulsepal(None, "/dev/p")
    assert isinstance(pp, PulsePalDevice) and pp._serial_port == "/dev/p"

    scale = execute._make_scale(
        ScaleCfg(type="scale", port="/dev/s", scale_type="bench", baudrate=4800),
        "/dev/s",
    )
    assert isinstance(scale, ScaleDevice)
    assert scale._serial_port == "/dev/s" and scale._scale_type == "bench"
