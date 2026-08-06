"""Session-provenance probes: WHICH machine + WHICH software produced a session.

A session YAML otherwise records only the logical ``setup`` name and ``out_path``, so a
session cannot be attributed to a physical rig or a software build - e.g. to spot a rig still
running old software. These best-effort probes fill that gap: machine identity
(``_get_host_info``) plus the MSW version and git commit. They are recorded into the session
``process`` block and logged at session start.

Everything here is stdlib + cross-platform (Win + Linux), never raises on a session start (each
field degrades independently), and the one probe that can block (``fqdn``, reverse-DNS) is
bounded by a thread timeout. Extracted from ``task_process`` so the orchestrator does not carry
the probing logic (msw-core spine refactor, Phase 0).
"""

import contextlib
import getpass
import platform
import socket
import subprocess
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from threading import Thread


def _resolve_msw_version() -> str:
    """Best-effort MSW version for session metadata.

    The umbrella distribution when it is installed (the full rig stack), else the
    standalone msw-core distribution, else "unknown" - so it never raises when
    msw-core runs without the umbrella (e.g. in msw-core's own tests).
    """
    for dist in ("murineshiftwork", "msw-core"):
        try:
            return _get_version(dist)
        except PackageNotFoundError:
            continue
    return "unknown"


def _get_git_commit() -> str:
    """Return the short git commit hash of the current HEAD, or '' if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _mac_address() -> str:
    """The primary interface MAC as ``xx:xx:xx:xx:xx:xx`` (a stable rig hardware id).

    ``uuid.getnode()`` is cross-platform (Win + Linux) and does no network I/O. It
    returns a random 48-bit value with the multicast bit set if it cannot read a real
    MAC; that is still a per-boot identifier and is captured as-is.
    """
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -8, -8))


def _fqdn(timeout: float = 2.0) -> str:
    """``socket.getfqdn()`` bounded by a thread timeout.

    getfqdn may do a reverse-DNS lookup that BLOCKS on a misconfigured network and does
    NOT honour socket timeouts (a libc call), so run it in a daemon thread and give up
    after ``timeout`` seconds - a session start must never hang on name resolution.
    Returns the domain name (or the hostname when there is no domain), else "".
    """
    result: list[str] = []

    def _probe() -> None:
        with contextlib.suppress(Exception):
            result.append(socket.getfqdn())

    t = Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if result else ""


def _ip_address() -> str:
    """The primary outbound IPv4 (source IP for the default route), or "".

    A UDP-socket ``connect`` is a route lookup only - NO packets are sent and there is no
    network round-trip - so it is fast, cross-platform (Win + Linux), and returns the real
    LAN IP the rig is reachable on (not the ``127.0.x.x`` that ``gethostbyname`` often
    yields on Linux). Returns "" when there is no route (offline). No data leaves the host.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(
            ("8.8.8.8", 80)
        )  # arbitrary; picks the source interface, sends nothing
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        if s is not None:
            s.close()


def _get_host_info() -> dict[str, str]:
    """Best-effort machine identity for session provenance (never raises).

    Records WHICH physical rig produced the session. The session YAML otherwise
    captures only the logical ``setup`` name and ``out_path``, so sessions cannot be
    attributed to a machine - e.g. to spot a rig still running old software. All probes
    are stdlib + cross-platform (Win + Linux); each is guarded so a missing/odd platform
    never breaks a session start, and the one that can block (``fqdn``, reverse-DNS) is
    bounded by a thread timeout. ``mac`` is the stable hardware id (``hostname`` /
    ``fqdn`` can be reassigned); ``fqdn`` is kept as useful standard network info.
    """
    info: dict[str, str] = {}
    for key, probe in (
        ("hostname", socket.gethostname),
        ("fqdn", _fqdn),
        ("ip", _ip_address),
        ("mac", _mac_address),
        ("platform", platform.platform),
        ("user", getpass.getuser),
    ):
        try:
            info[key] = str(probe())
        except Exception:
            info[key] = ""
    return info
