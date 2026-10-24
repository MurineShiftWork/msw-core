"""Session provenance: `_get_host_info()` records the physical machine.

The session `process` block long captured only the logical `setup` name + `out_path`,
so sessions could not be attributed to a rig (e.g. to spot one still on old software).
`_get_host_info()` adds machine identity (cross-platform, Win + Linux); it must be total
(never raise on a session start) and degrade each field independently. `mac` is the
stable hardware id; there is deliberately no FQDN (reverse-DNS can hang).
"""

import re
import time
from importlib.metadata import distribution
from types import SimpleNamespace
from unittest import mock

import murineshiftwork.logic.host_info as host_info
from murineshiftwork.logic.host_info import (
    _fqdn,
    _get_host_info,
    _ip_address,
    _mac_address,
)

_EXPECTED_KEYS = {"hostname", "fqdn", "ip", "mac", "platform", "user"}
_IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"


def test_returns_all_expected_keys():
    info = _get_host_info()
    assert set(info) == _EXPECTED_KEYS
    assert all(isinstance(v, str) for v in info.values())


def test_hostname_mac_platform_populated_on_a_normal_host():
    info = _get_host_info()
    assert info["hostname"]
    assert info["platform"]
    assert re.fullmatch(
        r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", info["mac"]
    )  # xx:xx:xx:xx:xx:xx


def test_mac_address_format():
    assert re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", _mac_address())


def test_ip_address_is_ipv4_or_empty():
    ip = _ip_address()
    assert ip == "" or re.fullmatch(_IPV4, ip)


def test_ip_address_empty_when_no_route():
    # no default route / offline: the connect fails and we degrade to "", never raise
    with mock.patch(
        "murineshiftwork.logic.host_info.socket.socket", side_effect=OSError
    ):
        assert _ip_address() == ""


def _slow_getfqdn():
    time.sleep(10)
    return "never"


def test_fqdn_bounded_on_a_slow_lookup():
    # a blocking reverse-DNS lookup must not hang: the thread timeout returns "" fast
    with mock.patch(
        "murineshiftwork.logic.host_info.socket.getfqdn", side_effect=_slow_getfqdn
    ):
        assert _fqdn(timeout=0.1) == ""  # gave up, did not hang


def test_a_failing_probe_degrades_to_empty_not_raise():
    with mock.patch(
        "murineshiftwork.logic.host_info.socket.gethostname",
        side_effect=OSError("boom"),
    ):
        info = _get_host_info()
    assert info["hostname"] == ""  # degraded, not raised
    assert info["user"]  # other probes unaffected
    assert info["mac"]


def test_all_probes_failing_still_returns_the_dict():
    with (
        mock.patch(
            "murineshiftwork.logic.host_info.socket.gethostname", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.host_info.socket.getfqdn", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.host_info.socket.socket", side_effect=OSError
        ),
        mock.patch("murineshiftwork.logic.host_info.uuid.getnode", side_effect=OSError),
        mock.patch(
            "murineshiftwork.logic.host_info.platform.platform", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.host_info.getpass.getuser", side_effect=OSError
        ),
    ):
        info = _get_host_info()
    assert info == {
        "hostname": "",
        "fqdn": "",
        "ip": "",
        "mac": "",
        "platform": "",
        "user": "",
    }


# --- installed package versions + task package provenance ---------------------------------


def test_package_versions_lists_installed_msw_distributions():
    versions = host_info._get_package_versions()
    assert versions["msw-core"] == distribution("msw-core").version
    assert versions["msw-io"] == distribution("msw-io").version
    assert "pyyaml" not in versions  # only the MSW stack, not every dependency
    assert all(isinstance(v, str) and v for v in versions.values())
    assert list(versions) == sorted(versions)


def test_package_versions_omits_uninstalled_distributions(monkeypatch):
    monkeypatch.setattr(
        host_info, "MSW_DISTRIBUTIONS", ("msw-core", "msw-not-a-real-dist")
    )
    assert "msw-not-a-real-dist" not in host_info._get_package_versions()


def test_package_versions_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("broken metadata")

    monkeypatch.setattr(host_info, "distributions", _boom)
    assert host_info._get_package_versions() == {}


def test_distribution_owning_regular_and_editable_installs():
    import murineshiftwork.io as msw_io  # msw-io: regular install from the index

    owner = host_info._distribution_owning(msw_io.__file__, "murineshiftwork")
    assert (
        owner is not None and host_info._canonical(owner.metadata["Name"]) == "msw-io"
    )
    # msw-core itself (editable install in the dev env, regular in a wheel install)
    owner = host_info._distribution_owning(host_info.__file__, "murineshiftwork")
    assert owner is not None
    assert host_info._canonical(owner.metadata["Name"]) == "msw-core"


def test_task_package_from_entry_point(monkeypatch):
    ep = SimpleNamespace(name="my_task", value="pkg.task", dist=distribution("msw-io"))
    monkeypatch.setattr(host_info, "entry_points", lambda group: [ep])
    assert host_info._get_task_package("my_task") == {
        "name": "msw-io",
        "version": distribution("msw-io").version,
    }


def test_task_package_from_module_file_ownership(monkeypatch):
    monkeypatch.setattr(host_info, "entry_points", lambda group: [])
    monkeypatch.setattr(host_info, "_task_module_name", lambda _t: "murineshiftwork.io")
    assert host_info._get_task_package("x") == {
        "name": "msw-io",
        "version": distribution("msw-io").version,
    }


def test_task_package_unknown_task_is_empty():
    assert host_info._get_task_package("definitely_not_a_task_xyz") == {}


def test_canonical_name():
    assert host_info._canonical("MSW_Tasks.Lab") == "msw-tasks-lab"
