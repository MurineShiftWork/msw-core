"""Version metadata is present and well-formed for the installed package."""

from importlib.metadata import version

import murineshiftwork  # noqa: F401  (import the package under test)


def test_version_is_non_empty_str():
    ver = version("msw-core")
    assert isinstance(ver, str)
    assert ver


def test_version_file_is_owned_by_this_distribution():
    # murineshiftwork is a namespace shared by several distributions; a shared
    # murineshiftwork/_version.py would be overwritten by whichever installs last.
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version_file = cfg["tool"]["hatch"]["build"]["hooks"]["vcs"]["version-file"]
    assert version_file == "src/murineshiftwork/_version_msw_core.py"
