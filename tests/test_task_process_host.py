"""Session provenance: `_get_host_info()` records the physical machine.

The session `process` block long captured only the logical `setup` name + `out_path`,
so sessions could not be attributed to a rig (e.g. to spot one still on old software).
`_get_host_info()` adds machine identity (cross-platform, Win + Linux); it must be total
(never raise on a session start) and degrade each field independently. `mac` is the
stable hardware id; there is deliberately no FQDN (reverse-DNS can hang).
"""

import re
import time
from unittest import mock

from murineshiftwork.logic.task_process import (
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
        "murineshiftwork.logic.task_process.socket.socket", side_effect=OSError
    ):
        assert _ip_address() == ""


def _slow_getfqdn():
    time.sleep(10)
    return "never"


def test_fqdn_bounded_on_a_slow_lookup():
    # a blocking reverse-DNS lookup must not hang: the thread timeout returns "" fast
    with mock.patch(
        "murineshiftwork.logic.task_process.socket.getfqdn", side_effect=_slow_getfqdn
    ):
        assert _fqdn(timeout=0.1) == ""  # gave up, did not hang


def test_a_failing_probe_degrades_to_empty_not_raise():
    with mock.patch(
        "murineshiftwork.logic.task_process.socket.gethostname",
        side_effect=OSError("boom"),
    ):
        info = _get_host_info()
    assert info["hostname"] == ""  # degraded, not raised
    assert info["user"]  # other probes unaffected
    assert info["mac"]


def test_all_probes_failing_still_returns_the_dict():
    with (
        mock.patch(
            "murineshiftwork.logic.task_process.socket.gethostname", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.task_process.socket.getfqdn", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.task_process.socket.socket", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.task_process.uuid.getnode", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.task_process.platform.platform", side_effect=OSError
        ),
        mock.patch(
            "murineshiftwork.logic.task_process.getpass.getuser", side_effect=OSError
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
