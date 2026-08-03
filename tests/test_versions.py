"""msw version banner: enumerates installed murineshiftwork / msw-* distributions."""

from murineshiftwork.logic.versions import (
    COPYRIGHT,
    msw_installed_versions,
    msw_version_banner,
)


def test_lists_msw_packages_main_first():
    pkgs = msw_installed_versions()
    names = [n for n, _ in pkgs]
    assert "msw-core" in names  # the package under test is always present
    assert all(n == "murineshiftwork" or n.startswith("msw-") for n in names)
    assert all(v for _, v in pkgs)  # every entry has a version
    # the umbrella isn't installed in msw-core's own CI; when it IS, it sorts first
    if "murineshiftwork" in names:
        assert names[0] == "murineshiftwork"


def test_banner_has_versions_and_copyright():
    banner = msw_version_banner()
    assert "msw-core" in banner
    assert banner.rstrip().endswith(COPYRIGHT)
    assert "PolyForm" not in banner  # old string gone
    assert "BSD-3-Clause-NonCommercial" in banner
