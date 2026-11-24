"""One live session per setup on this machine.

``msw run`` takes a per-setup lock before any device opens. The lock is a YAML file
in the machine-local state dir::

    ~/.murineshiftwork/locks/<setup>.lock

    pid: 12345
    host: rig-1
    started_at: '2026-11-24T10:00:00+00:00'
    setup: rig-1
    session_container: /data/m1/m1__20261124_100000   # filled once the folder exists

At acquire:

- no lock: take it.
- held by a live pid on this host: refuse (``SessionLockError``).
- held by a pid on another host (a shared home): refuse; its liveness cannot be
  checked from here, the message names the file to remove.
- held by a dead pid, or unreadable: the previous session died without cleanup. Warn,
  run the stale handler (reset cameras left recording, mark its ``running``
  acquisitions ``interrupted``), then take the lock.

The lock is released by the owning process on normal exit and on exceptions
(``finally`` plus ``atexit``). A pid reused by an unrelated process after a crash
reads as live and refuses; remove the file by hand then.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

LOCK_DIR = Path("~/.murineshiftwork/locks").expanduser()

#: the lock this process holds, so TaskProcess can record the session container
_held: SetupLock | None = None


class SessionLockError(RuntimeError):
    """Another live session holds the setup."""


def lock_path(setup: str, lock_dir: Path | None = None) -> Path:
    return (lock_dir or LOCK_DIR) / f"{setup}.lock"


def pid_alive(pid: int) -> bool:
    """True when process ``pid`` exists on this host."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        # os.kill(pid, 0) terminates the process on Windows: query it instead
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def read_lock(path: Path) -> dict[str, Any] | None:
    """The lock's fields, ``{}`` when unreadable, ``None`` when there is none."""
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def note_session_container(container: str | Path) -> None:
    """Record the session container in the lock this process holds, if any."""
    if _held is not None:
        _held.set_container(container)


class SetupLock:
    """The lock of one setup; use as a context manager.

    Args:
        setup: Setup name (the lock file name).
        on_stale: Called with the dead owner's lock fields before the lock is taken.
            Its exceptions are logged, never raised: recovery must not block the run.
        lock_dir: Lock directory (tests).
    """

    def __init__(
        self,
        setup: str,
        *,
        on_stale: Callable[[dict[str, Any]], None] | None = None,
        lock_dir: Path | None = None,
    ) -> None:
        self.setup = setup
        self.path = lock_path(setup, lock_dir)
        self._on_stale = on_stale
        self._data: dict[str, Any] = {}
        self._held = False

    def __enter__(self) -> SetupLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> None:
        global _held
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old = read_lock(self.path)
        if old is not None:
            self._check_owner(old)
        self._data = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "setup": self.setup,
            "session_container": "",
        }
        self._write()
        self._held = True
        _held = self
        atexit.register(self.release)

    def _check_owner(self, old: dict[str, Any]) -> None:
        try:
            pid = int(old.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        host = str(old.get("host") or "")
        here = socket.gethostname()
        owner = (
            f"pid {pid} on {host or '?'} since {old.get('started_at', '?')}, "
            f"session {old.get('session_container') or '(no folder yet)'}"
        )
        if host and host != here:
            raise SessionLockError(
                f"setup {self.setup!r} is locked by {owner}. If that session is not "
                f"running, remove {self.path}"
            )
        if pid and pid != os.getpid() and pid_alive(pid):
            raise SessionLockError(
                f"setup {self.setup!r} is in use by a running session ({owner}). "
                f"End it first; if pid {pid} is not an msw session, remove {self.path}"
            )
        log.warning(
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "Setup %r: the previous session did not end cleanly (%s).\n"
            "Resetting its cameras and marking its running acquisitions interrupted.\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            self.setup,
            owner,
        )
        if self._on_stale is not None:
            try:
                self._on_stale(old)
            except Exception:  # noqa: BLE001 - recovery never blocks the new run
                log.warning("stale session recovery failed", exc_info=True)

    def set_container(self, container: str | Path) -> None:
        if not self._held:
            return
        self._data["session_container"] = str(container)
        with contextlib.suppress(OSError):
            self._write()

    def release(self) -> None:
        """Remove the lock if this process still owns it. Safe to call repeatedly."""
        global _held
        if not self._held:
            return
        self._held = False
        if _held is self:
            _held = None
        with contextlib.suppress(Exception):
            atexit.unregister(self.release)
        current = read_lock(self.path)
        if current and current.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".lock.tmp")
        tmp.write_text(yaml.safe_dump(self._data, sort_keys=False))
        tmp.replace(self.path)


def interrupt_running_acquisitions(container: str | Path) -> list[str]:
    """Mark the ``running`` acquisitions of a dead session ``interrupted``.

    Returns the basenames changed. A missing container or manifest changes nothing.
    """
    from murineshiftwork.namespace.manifest import finalize_acquisition_in_session

    manifest = Path(container) / "session_manifest.yaml"
    if not manifest.is_file():
        return []
    data = yaml.safe_load(manifest.read_text()) or {}
    changed = []
    for entry in data.get("acquisitions") or []:
        if entry.get("status") == "running" and entry.get("basename"):
            finalize_acquisition_in_session(
                container, entry["basename"], status="interrupted"
            )
            changed.append(entry["basename"])
    for name in changed:
        log.warning("acquisition %s marked interrupted", name)
    return changed
