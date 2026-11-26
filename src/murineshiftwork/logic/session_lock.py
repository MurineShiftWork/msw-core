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

Concurrent acquirers (two runs started together) get exactly one owner:

- a lock is published only by hard-linking a fully written private file to the lock
  name; the link fails when the name exists, so creation is exclusive and a reader
  never sees a half written lock. Where hard links are not supported, an exclusive
  create (``O_CREAT | O_EXCL``) takes its place.
- a dead owner's lock is taken over by renaming it to a private name and checking
  that the renamed file still holds the dead owner's fields. Only one acquirer's
  rename succeeds; one that moved a lock taken in the meantime links it back. The
  dead owner's fields are left as ``<setup>.lock.recover`` and the winner publishes
  its own lock as above, so a run that sees the name free in between competes
  through the same exclusive create.
- the stale recovery (``on_stale``) runs after the lock is taken, by whichever run
  owns it, on the ``.recover`` fields it takes by rename: once, and no other run
  starts on the setup meanwhile. A ``leave_stale`` owner leaves them in place.
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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

LOCK_DIR = Path("~/.murineshiftwork/locks").expanduser()

#: the lock this process holds, so TaskProcess can record the session container
_held: SetupLock | None = None

#: attempts to take the lock while other acquirers move it (each backs off briefly)
_ATTEMPTS = 100


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
        for attempt in range(_ATTEMPTS):
            if _publish(self.path, self._data):
                break
            old = read_lock(self.path)
            if old is None:
                continue  # released or moved since the publish attempt
            if not self._check_owner(old):
                return
            claimed = _claim(self.path, old)
            if claimed is not None:
                # handed to whichever run publishes next, which may not be this one
                claimed.replace(_pending_path(self.path))
            else:
                time.sleep(0.001 * (attempt + 1))
        else:
            raise SessionLockError(
                f"setup {self.setup!r}: could not take {self.path} while other "
                "runs were taking it; try again"
            )
        self._held = True
        _held = self
        atexit.register(self.release)
        if not self._leave_stale:
            pending = _take_pending(self.path)
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
        tmp = _private_name(self.path, "tmp")
        tmp.write_text(yaml.safe_dump(self._data, sort_keys=False))
        tmp.replace(self.path)


def _describe(old: dict[str, Any]) -> str:
    return (
        f"pid {_owner_pid(old)} on {old.get('host') or '?'} since "
        f"{old.get('started_at', '?')}, "
        f"session {old.get('session_container') or '(no folder yet)'}"
    )


def _private_name(path: Path, kind: str) -> Path:
    """A name next to ``path`` that no other process uses."""
    return path.with_name(f"{path.name}.{kind}-{os.getpid()}-{uuid.uuid4().hex}")


def _publish(path: Path, data: dict[str, Any]) -> bool:
    """Create the lock file ``path`` holding ``data`` unless it exists. Returns
    whether this call created it.

    The fields are written to a private file first and hard-linked to ``path``:
    the link is atomic and fails when ``path`` exists, so readers never see a
    partly written lock. Without hard link support (some file systems), an
    exclusive create and a write take its place.
    """
    text = yaml.safe_dump(data, sort_keys=False)
    tmp = _private_name(path, "tmp")
    tmp.write_text(text)
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass  # no hard links here
    finally:
        tmp.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):
        return False  # PermissionError: Windows, the name is being removed
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def _claim(path: Path, old: dict[str, Any]) -> Path | None:
    """Move the lock ``path`` to a private name if it still holds ``old``. Returns
    the private file, or None when this call did not move it.

    Only one process can rename the file away. If the renamed file is not ``old``
    (another run took the lock in the meantime), it is linked back unless the name
    was taken again.
    """
    claimed = _private_name(path, "stale")
    try:
        path.rename(claimed)
    except (FileNotFoundError, PermissionError):
        return None  # moved by another acquirer, or open on Windows: retry
    if read_lock(claimed) == old:
        return claimed
    with contextlib.suppress(OSError):
        os.link(claimed, path)
    claimed.unlink(missing_ok=True)
    return None


def _pending_path(path: Path) -> Path:
    """A dead owner's lock moved aside, waiting for the next owner to recover it."""
    return path.with_name(f"{path.name}.recover")


def _take_pending(path: Path) -> dict[str, Any] | None:
    """The fields of a dead owner's lock waiting for recovery, taken by this
    process only (renamed to a private name), or None."""
    taken = _private_name(path, "recovering")
    try:
        _pending_path(path).rename(taken)
    except (FileNotFoundError, PermissionError):
        return None
    try:
        return read_lock(taken) or {}
    finally:
        taken.unlink(missing_ok=True)


def clear_stale_lock(setup: str, lock_dir: Path | None = None) -> dict[str, Any] | None:
    """Remove the lock of ``setup`` when its owner is dead (``STALE``), after
    marking the owner's ``running`` acquisitions ``interrupted``.

    A live or foreign owner's lock, or no lock, is left alone.

    Returns:
        The removed lock's fields, or None when nothing was removed.
    """
    path = lock_path(setup, lock_dir)
    old = read_lock(path)
    if old is None or owner_state(old) != STALE:
        return None
    claimed = _claim(path, old)
    if claimed is None:
        return None  # taken or removed by another process meanwhile
    claimed.unlink(missing_ok=True)
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
