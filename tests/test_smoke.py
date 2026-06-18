"""Smoke tests: verify msw-core installs and namespace modules are importable."""

import importlib.util


def test_murineshiftwork_namespace_importable():
    assert importlib.util.find_spec("murineshiftwork") is not None


def test_cli_module_importable():
    assert importlib.util.find_spec("murineshiftwork.cli") is not None


def test_hooks_module_importable():
    assert importlib.util.find_spec("murineshiftwork.hooks") is not None
