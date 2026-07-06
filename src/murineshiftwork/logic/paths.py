"""Host/environment path helpers.

Namespace path construction lives in ``murineshiftwork.namespace`` and should be
imported from there directly; this module holds only the host-local helpers.
"""

import socket
from pathlib import Path

__all__ = [
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
