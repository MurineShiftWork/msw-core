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
