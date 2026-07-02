"""Version metadata is present and well-formed for the installed package."""

from importlib.metadata import version

import murineshiftwork  # noqa: F401  (import the package under test)


def test_version_is_non_empty_str():
    ver = version("msw-core")
    assert isinstance(ver, str)
    assert ver
