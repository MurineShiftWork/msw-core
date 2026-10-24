"""Host/environment path helpers.

Namespace path construction lives in ``murineshiftwork.namespace`` and should be
imported from there directly; this module holds only the host-local helpers.
"""

import contextlib
import logging
import os
import shutil
import socket
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import IO

__all__ = [
    "atomic_open_w",
    "test_path_is_writable",
    "get_host_ip",
    "get_host_name",
]


def test_path_is_writable(path=None):
    try:
        with Path(path).open("w"):
            pass
        if Path(path).exists():
            Path(path).unlink()
    except PermissionError:
        return False
    return True


# On Windows a reader holding the target open makes os.replace fail with PermissionError; retry
# briefly before falling back to an in-place copy.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_S = 0.05


@contextlib.contextmanager
def atomic_open_w(path, encoding: str | None = None) -> Iterator[IO[str]]:
    """Open ``path`` for a whole-file text rewrite that lands atomically.

    Yields a handle on a temporary sibling file; on a clean exit the data is flushed, fsynced
    and moved over ``path`` with ``os.replace`` (atomic on POSIX and Windows when source and
    target share a directory). If the body raises, the temporary file is removed and ``path``
    is left exactly as it was, so a crash mid-write can never leave a truncated file behind.

    The temporary file is created with ``open(..., "x")`` so it gets the normal umask-derived
    permissions, and an existing target's mode is copied onto it before the swap. If the swap
    keeps failing with ``PermissionError`` (Windows, target held open by a reader) the content
    is copied over the target in place instead, with a warning, so the write is never dropped.
    """
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("x", encoding=encoding) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            with contextlib.suppress(OSError):
                shutil.copymode(target, tmp)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                tmp.replace(target)  # os.replace: atomic swap
                return
            except PermissionError:
                if attempt + 1 < _REPLACE_ATTEMPTS:
                    time.sleep(_REPLACE_RETRY_DELAY_S)
        logging.warning(
            "Could not atomically replace %s (held open?); rewriting in place", target
        )
        shutil.copyfile(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def get_host_ip():
    """Source: https://stackoverflow.com/questions/166506/finding-local-ip-addresses-using-pythons-stdlib"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_host_name():
    return socket.gethostname()
