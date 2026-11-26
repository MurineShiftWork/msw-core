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
- held by a dead pid, or unreadable: the previous session died without cleanup. Take
  the lock, warn and run the stale handler (reset cameras left recording, mark its
  ``running`` acquisitions ``interrupted``).

A ``--simulate`` run takes the lock too (``leave_stale``), so it refuses while a
session is live, but it leaves a dead owner's lock in place and runs without one:
it resolves no cameras, so the camera reset of the recovery is left to the next
run on hardware or to ``msw cameras reset``, which clears a dead owner's lock after
resetting the cameras (``clear_stale_lock``). A live owner's lock is never removed.

The lock is released by the owning process on normal exit and on exceptions
(``finally`` plus ``atexit``). A pid reused by an unrelated process after a crash
reads as live and refuses; remove the file by hand then.

Concurrent acquirers (two runs started together) get exactly one owner. Every
change to the lock name (take, take over, rewrite, release, clear) happens while
holding the setup's guard: an OS file lock on ``<setup>.lock.guard`` (``flock`` on
POSIX, ``msvcrt.locking`` on Windows). The guard serialises the read, the owner
check and the takeover, so two runs never both see a dead owner and both take
over, and nothing removes or replaces a lock another run has just written. The
OS drops the guard when its holder exits or dies, so a crash cannot leave it
held; the guard file itself is never removed (removing it would let a second
run lock a new file while the first still holds the old one).

- the lock is written to a private file and renamed onto the lock name, so a
  reader never sees a partly written lock.
- a dead owner's lock is moved to ``<setup>.lock.recover`` before the new owner
  writes its own; the new owner takes the ``.recover`` fields (still under the
  guard) and runs the stale recovery (``on_stale``) once the guard is released.
  A ``leave_stale`` run leaves the dead owner's lock in place.
- on Windows, a rename or delete fails while another process (a status reader,
  an antivirus scan) has the file open; those calls are retried for a short time.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

LOCK_DIR = Path("~/.murineshiftwork/locks").expanduser()

#: the lock this process holds, so TaskProcess can record the session container
_held: SetupLock | None = None

#: seconds an acquirer waits for another run to finish checking or taking the lock
_GUARD_TIMEOUT = 10.0
#: seconds a rename or delete is retried while another process has the file open
_SHARING_TIMEOUT = 2.0


#: ``owner_state``: no lock file
FREE = "free"
#: ``owner_state``: held by a live pid on this host
LIVE = "live"
#: ``owner_state``: held on another host; its liveness cannot be checked from here
FOREIGN = "foreign"
#: ``owner_state``: held by a dead pid on this host, or unreadable
STALE = "stale"


class SessionLockError(RuntimeError):
    """Another live session holds the setup."""


def owner_state(old: dict[str, Any] | None) -> str:
    """``FREE``, ``LIVE``, ``FOREIGN`` or ``STALE`` for the fields of a lock file
    (``read_lock``). This process's own pid counts as stale (a lock it left)."""
    if old is None:
        return FREE
    host = str(old.get("host") or "")
    if host and host != socket.gethostname():
        return FOREIGN
    pid = _owner_pid(old)
    if pid and pid != os.getpid() and pid_alive(pid):
        return LIVE
    return STALE


def _owner_pid(old: dict[str, Any]) -> int:
    try:
        return int(old.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


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
        on_stale: Called with the dead owner's lock fields once the lock is taken.
            Its exceptions are logged, never raised: recovery must not block the run.
        lock_dir: Lock directory (tests).
        leave_stale: A dead owner's lock is left in place and not taken; the
            run goes on without a lock (``--simulate``). ``on_stale`` is not run.
    """

    def __init__(
        self,
        setup: str,
        *,
        on_stale: Callable[[dict[str, Any]], None] | None = None,
        lock_dir: Path | None = None,
        leave_stale: bool = False,
    ) -> None:
        self.setup = setup
        self.path = lock_path(setup, lock_dir)
        self._on_stale = on_stale
        self._leave_stale = leave_stale
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
        self._data = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "setup": self.setup,
            "session_container": "",
        }
        pending = None
        with _guarded(self.path):
            old = read_lock(self.path)
            if old is not None:
                if not self._check_owner(old):
                    return
                _retry_sharing(os.replace, self.path, _pending_path(self.path))
            self._write()
            if not self._leave_stale:
                pending = _take_pending(self.path)
        self._held = True
        _held = self
        atexit.register(self.release)
        if pending is not None:
            self._recover(pending)

    def _check_owner(self, old: dict[str, Any]) -> bool:
        """Refuse a live or foreign owner. Returns whether to take over a dead one
        (False for ``leave_stale``)."""
        pid = _owner_pid(old)
        owner = _describe(old)
        state = owner_state(old)
        if state == FOREIGN:
            raise SessionLockError(
                f"setup {self.setup!r} is locked by {owner}. If that session is not "
                f"running, remove {self.path}"
            )
        if state == LIVE:
            raise SessionLockError(
                f"setup {self.setup!r} is in use by a running session ({owner}). "
                f"End it first; if pid {pid} is not an msw session, remove {self.path}"
            )
        if self._leave_stale:
            log.warning(
                "Setup %r: the previous session did not end cleanly (%s); its lock "
                "is left for the next run on hardware or 'msw cameras reset' to "
                "recover",
                self.setup,
                owner,
            )
            return False
        return True

    def _recover(self, old: dict[str, Any]) -> None:
        """Recover the dead owner's session; runs once, by the new owner."""
        log.warning(
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "Setup %r: the previous session did not end cleanly (%s).\n"
            "Resetting its cameras and marking its running acquisitions interrupted.\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            self.setup,
            _describe(old),
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
        with contextlib.suppress(OSError, SessionLockError), _guarded(self.path):
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
        with contextlib.suppress(OSError, SessionLockError), _guarded(self.path):
            current = read_lock(self.path)
            if current and current.get("pid") == os.getpid():
                _retry_sharing(os.unlink, self.path)

    def _write(self) -> None:
        """Write the lock fields to the lock name; the caller holds the guard."""
        tmp = _private_name(self.path, "tmp")
        tmp.write_text(yaml.safe_dump(self._data, sort_keys=False))
        try:
            _retry_sharing(os.replace, tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)


def _describe(old: dict[str, Any]) -> str:
    return (
        f"pid {_owner_pid(old)} on {old.get('host') or '?'} since "
        f"{old.get('started_at', '?')}, "
        f"session {old.get('session_container') or '(no folder yet)'}"
    )


def _private_name(path: Path, kind: str) -> Path:
    """A name next to ``path`` that no other process uses."""
    return path.with_name(f"{path.name}.{kind}-{os.getpid()}-{uuid.uuid4().hex}")


def _guard_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.guard")


@contextlib.contextmanager
def _guarded(path: Path) -> Iterator[None]:
    """Hold the guard of the lock ``path`` (an OS lock on ``<setup>.lock.guard``).

    Raises ``SessionLockError`` when another run holds it for ``_GUARD_TIMEOUT``.
    """
    fd = os.open(_guard_path(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + _GUARD_TIMEOUT
        delay = 0.001
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise SessionLockError(
                    f"{path.name}: another run kept {_guard_path(path)} locked for "
                    f"{_GUARD_TIMEOUT:.0f} s; try again"
                )
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    """Take the OS lock on the open guard file ``fd`` if it is free."""
    if sys.platform.startswith("win"):
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _retry_sharing(op: Callable[..., Any], *args: Any) -> Any:
    """Run a rename or delete, retrying while Windows refuses it because another
    process has the file open (``PermissionError``)."""
    deadline = time.monotonic() + _SHARING_TIMEOUT
    while True:
        try:
            return op(*args)
        except PermissionError:
            if not sys.platform.startswith("win") or time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def _pending_path(path: Path) -> Path:
    """A dead owner's lock moved aside, waiting for the new owner to recover it."""
    return path.with_name(f"{path.name}.recover")


def _take_pending(path: Path) -> dict[str, Any] | None:
    """The fields of a dead owner's lock waiting for recovery, removed, or None.
    The caller holds the guard and has written its lock: a failed delete is
    left for the next owner, which recovers the same session again."""
    pending = _pending_path(path)
    old = read_lock(pending)
    if old is not None:
        with contextlib.suppress(OSError):
            _retry_sharing(pending.unlink, True)
    return old


def clear_stale_lock(setup: str, lock_dir: Path | None = None) -> dict[str, Any] | None:
    """Remove the lock of ``setup`` when its owner is dead (``STALE``), after
    marking the owner's ``running`` acquisitions ``interrupted``.

    A live or foreign owner's lock, or no lock, is left alone.

    Returns:
        The removed lock's fields, or None when nothing was removed.
    """
    path = lock_path(setup, lock_dir)
    if not path.parent.is_dir():
        return None
    with _guarded(path):
        old = read_lock(path)
        if old is None or owner_state(old) != STALE:
            return None
        _retry_sharing(path.unlink)
    if old.get("session_container"):
        interrupt_running_acquisitions(old["session_container"])
    log.warning("setup %r: removed the lock of a dead session (%s)", setup, path)
    return old


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
