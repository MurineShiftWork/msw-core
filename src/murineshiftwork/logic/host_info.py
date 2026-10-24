"""Session-provenance probes: WHICH machine + WHICH software produced a session.

A session YAML otherwise records only the logical ``setup`` name and ``out_path``, so a
session cannot be attributed to a physical rig or a software build - e.g. to spot a rig still
running old software. These best-effort probes fill that gap: machine identity
(``_get_host_info``), the MSW version and git commit, the version of every installed MSW-stack
distribution (``_get_package_versions``) and the distribution that provides the running task
(``_get_task_package``). They are recorded into the session ``process`` block and logged at
session start.

Everything here is stdlib + cross-platform (Win + Linux), never raises on a session start (each
field degrades independently), and the one probe that can block (``fqdn``, reverse-DNS) is
bounded by a thread timeout. Extracted from ``task_process`` so the orchestrator does not carry
the probing logic (msw-core spine refactor, Phase 0).
"""

import contextlib
import getpass
import importlib.util
import platform
import socket
import subprocess
import uuid
from importlib.metadata import (
    Distribution,
    PackageNotFoundError,
    distributions,
    entry_points,
    packages_distributions,
)
from importlib.metadata import version as _get_version
from pathlib import Path
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


# Distributions of the MSW stack recorded in the session provenance when installed. Any other
# installed ``murineshiftwork`` / ``msw-*`` distribution is picked up by name as well.
MSW_DISTRIBUTIONS: tuple[str, ...] = (
    "murineshiftwork",
    "msw-core",
    "msw-io",
    "msw-plugin-api",
    "msw-agent",
    "msw-metadata",
    "msw-tasks-core",
    "msw-tasks-lab",
    "msw-tasks-example",
    "msw-open-ephys",
    "msw-flir-bonsai",
    "msw-labwatch",
    "rpi-camera-ensemble",
    "acquisition-namespace",
    "ttl-barcoder",
    "pypulsepal",
    "one-axis-stage",
    "serial-scale-bench",
    "serial-scale-hx711",
    "pybpod-api",
)


def _canonical(name: str) -> str:
    """PEP 503 normalised distribution name (``msw_core`` / ``MSW.Core`` -> ``msw-core``)."""
    return "-".join(name.replace("_", "-").replace(".", "-").lower().split("-"))


def _get_package_versions() -> dict[str, str]:
    """``{distribution: version}`` for every installed MSW-stack distribution (never raises).

    Covers :data:`MSW_DISTRIBUTIONS` plus any other installed ``murineshiftwork`` / ``msw-*``
    distribution, so the session records exactly which code produced it. Distributions that
    are not installed are omitted. Sorted by name.
    """
    wanted = {_canonical(n) for n in MSW_DISTRIBUTIONS}
    found: dict[str, str] = {}
    with contextlib.suppress(Exception):
        for dist in distributions():
            name = _canonical(dist.metadata["Name"] or "")
            if name and (name in wanted or name.startswith("msw-")):
                found.setdefault(name, dist.version)  # first on the path wins
    return dict(sorted(found.items()))


def _pth_dirs(dist: Distribution) -> list[Path]:
    """Source directories an editable install of ``dist`` adds to ``sys.path`` via ``.pth``."""
    dirs: list[Path] = []
    for f in dist.files or []:
        if f.suffix != ".pth":
            continue
        with contextlib.suppress(Exception):
            for line in Path(str(dist.locate_file(f))).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "import ")):
                    dirs.append(Path(line).resolve())
    return dirs


def _distribution_owning(file_path: str | Path, top_level: str) -> Distribution | None:
    """The installed distribution that provides ``file_path`` (a module of ``top_level``).

    First checks the files recorded for a regular install of each distribution providing the
    ``top_level`` import package; then the source directories of editable installs (which
    record only a ``.pth`` file, not the modules). Returns ``None`` when no distribution owns
    the file.
    """
    target = Path(file_path).resolve()
    for name in dict.fromkeys(packages_distributions().get(top_level, [])):
        try:
            dist = Distribution.from_name(name)
        except PackageNotFoundError:
            continue
        base = Path(str(dist.locate_file(""))).resolve()
        try:
            rel = target.relative_to(base).as_posix()
        except ValueError:
            continue
        if any(str(f) == rel for f in dist.files or []):
            return dist
    for dist in distributions():
        if any(target.is_relative_to(d) for d in _pth_dirs(dist)):
            return dist
    return None


def _task_module_name(task_name: str) -> str:
    """Module path of ``task_name``, resolved the same way ``load_task_module`` imports it."""
    for ep in entry_points(group="msw.tasks"):
        if ep.name == task_name:
            return ep.value.split(":", 1)[0]
    return f"murineshiftwork.tasks.{task_name}.task"


def _get_task_package(task_name: str) -> dict[str, str]:
    """``{"name", "version"}`` of the distribution providing ``task_name`` (never raises).

    Resolved from the task's ``msw.tasks`` entry point when it has one, else from which
    installed distribution owns the task module's file. ``{}`` when it cannot be determined.
    """
    with contextlib.suppress(Exception):
        for ep in entry_points(group="msw.tasks"):
            if ep.name == task_name and ep.dist is not None:
                return {
                    "name": _canonical(ep.dist.metadata["Name"]),
                    "version": ep.dist.version,
                }
        module = _task_module_name(task_name)
        spec = importlib.util.find_spec(module)
        if spec is not None and spec.origin:
            dist = _distribution_owning(spec.origin, module.split(".", 1)[0])
            if dist is not None:
                return {
                    "name": _canonical(dist.metadata["Name"]),
                    "version": dist.version,
                }
    return {}


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
