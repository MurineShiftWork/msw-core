"""make_host_session: entry-point dispatch over the ``msw.host`` plugin group."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from msw_plugin_api import HostSessionProtocol

from murineshiftwork.hardware.host_session import make_host_session
from murineshiftwork.logic.config.models import SetupConfig


class _FakeHostSession:
    """Minimal structural HostSessionProtocol implementation; records its kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def attach(self, **kwargs):
        return None

    def start(self):
        pass

    def stop(self):
        pass


class _NotAHostSession:
    def __init__(self, **kwargs):
        pass


def _register(monkeypatch, name, cls):
    ep = MagicMock()
    ep.name = name
    ep.load.return_value = cls
    monkeypatch.setattr(
        "murineshiftwork.hardware.host_session.entry_points",
        lambda group: [ep] if group == "msw.host" else [],
    )


def test_dispatches_to_registered_plugin_with_kwargs(monkeypatch):
    _register(monkeypatch, "openephys", _FakeHostSession)
    client = make_host_session("openephys", url="127.0.0.1")
    assert isinstance(client, _FakeHostSession)
    assert isinstance(client, HostSessionProtocol)
    assert client.kwargs == {"url": "127.0.0.1"}


@pytest.mark.parametrize("requested", ["open-ephys", "open_ephys", "OpenEphys"])
def test_name_matching_ignores_case_and_separators(monkeypatch, requested):
    _register(monkeypatch, "openephys", _FakeHostSession)
    assert isinstance(make_host_session(requested), _FakeHostSession)


def test_unknown_type_raises(monkeypatch):
    _register(monkeypatch, "openephys", _FakeHostSession)
    with pytest.raises(ValueError, match="No msw.host plugin registered for 'imaging'"):
        make_host_session("imaging", url="127.0.0.1")


def test_plugin_not_satisfying_protocol_raises(monkeypatch):
    _register(monkeypatch, "broken", _NotAHostSession)
    with pytest.raises(TypeError, match="does not satisfy HostSessionProtocol"):
        make_host_session("broken")


def test_setup_config_has_no_host_session_field():
    # host sessions are opted into per run (--host), never pinned in the setup YAML
    assert not hasattr(SetupConfig(name="rig1"), "host_session")


# --host in msw run: every failure aborts unless --force-standalone


class _AttachingHostSession(_FakeHostSession):
    def attach(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            backend="fake", session_name="s1__20260101_000000", subject="s1"
        )


def _resolve(args):
    from murineshiftwork.cli.evaluate import _resolve_host_session

    _resolve_host_session(args)
    return args


def test_unknown_host_type_aborts_and_lists_the_registered(monkeypatch):
    _register(monkeypatch, "openephys", _FakeHostSession)
    with pytest.raises(RuntimeError, match="could not attach") as err:
        _resolve({"host_flag": "photo"})
    assert "registered: openephys" in str(err.value)


def test_unknown_host_type_with_force_standalone_records_standalone(monkeypatch):
    _register(monkeypatch, "openephys", _FakeHostSession)
    args = _resolve({"host_flag": "photo", "force_standalone": True})
    assert "linked_to" not in args


def test_openephys_without_a_url_aborts(monkeypatch):
    _register(monkeypatch, "openephys", _FakeHostSession)
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_open_ephys_url", lambda: ""
    )
    with pytest.raises(RuntimeError, match="no URL"):
        _resolve({"host_flag": "openephys"})


def test_a_host_that_attaches_links_the_run(monkeypatch):
    _register(monkeypatch, "fake", _AttachingHostSession)
    args = _resolve({"host_flag": "fake"})
    assert args["linked_to"] == "s1__20260101_000000"


class _OEHost:
    """Records how the run built and attached it."""

    made: list[dict] = []
    attached: list[dict] = []

    def __init__(self, **kwargs):
        _OEHost.made.append(kwargs)

    def attach(self, subject=None):
        from types import SimpleNamespace

        _OEHost.attached.append({"subject": subject})
        return SimpleNamespace(
            backend="openephys", session_name="m1__20260101_000000", subject=subject
        )

    def start(self):
        pass

    def stop(self):
        pass


class _OldOEHost(_OEHost):
    """A host plugin from before attach(subject=...)."""

    def attach(self):  # type: ignore[override]
        return super().attach()


@pytest.fixture
def oe_host(monkeypatch):
    _OEHost.made, _OEHost.attached = [], []

    def _use(cls):
        _register(monkeypatch, "openephys", cls)

    return _use


def test_openephys_attach_requires_recording_and_the_subject(oe_host, caplog):
    oe_host(_OEHost)
    setup = SetupConfig(name="rig", open_ephys_url="oe-host.example")
    with caplog.at_level("INFO"):
        args = _resolve(
            {"host_flag": "openephys", "setup_config": setup, "subject": "m1"}
        )
    assert _OEHost.made == [{"url": "oe-host.example", "require_recording": True}]
    assert _OEHost.attached == [{"subject": "m1"}]
    assert args["linked_to"] == "m1__20260101_000000"
    assert "from setup yaml open_ephys_url" in caplog.text


def test_openephys_url_from_machine_config_is_logged(oe_host, monkeypatch, caplog):
    oe_host(_OEHost)
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_open_ephys_url",
        lambda: "oe-machine.example",
    )
    with caplog.at_level("INFO"):
        _resolve({"host_flag": "openephys", "subject": "m1"})
    assert "oe-machine.example (from machine config" in caplog.text


def test_older_host_plugin_without_subject_still_attaches(oe_host):
    oe_host(_OldOEHost)
    args = _resolve({"host_flag": "openephys:oe-host.example", "subject": "m1"})
    assert _OEHost.attached == [{"subject": None}]
    assert args["linked_to"] == "m1__20260101_000000"
