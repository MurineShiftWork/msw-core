"""Shared fixtures."""

from __future__ import annotations

import re
import sys
from importlib.metadata import EntryPoint

import pytest
import yaml

_FAKE_PKG = "msw_fake_tasks"

# name -> task.yaml contents
_FAKE_TASKS: dict[str, dict] = {
    "_test_flush_valves": {
        "session_type": "flush",
        "version": 1,
        "default": {"N_FLUSH_CYCLES": 3},
    },
    "_calibration_sound": {"default": {}},
    "demo_sequence": {"default": {"iti": 1.0}, "mode": {"fast": {"iti": 0.5}}},
}


@pytest.fixture
def fake_tasks(tmp_path, monkeypatch):
    """Register an importable fake task package under the ``msw.tasks`` entry-point group.

    msw-core ships no tasks and its CI installs no task packages, so task discovery and the
    evaluate pipeline are exercised against these. The filesystem scan for bundled tasks is
    stubbed out so a workspace with task packages installed sees the same three tasks.
    Returns ``{name: task_dir}``.
    """
    root = tmp_path / "fake_task_pkgs"
    pkg = root / _FAKE_PKG
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    dirs = {}
    eps = []
    for name, task_yaml in _FAKE_TASKS.items():
        d = pkg / name
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / "task.py").write_text("TASK_NAME = " + repr(name) + "\n", encoding="utf-8")
        (d / "task.yaml").write_text(yaml.safe_dump(task_yaml), encoding="utf-8")
        dirs[name] = d
        eps.append(EntryPoint(name, f"{_FAKE_PKG}.{name}.task", "msw.tasks"))

    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(
        "murineshiftwork.cli.tasks.entry_points",
        lambda group: eps if group == "msw.tasks" else [],
    )
    monkeypatch.setattr(
        "murineshiftwork.cli.tasks._list_tasks_filesystem",
        lambda detailed=False: {} if detailed else [],
    )
    yield dirs
    for mod in [m for m in sys.modules if m.split(".")[0] == _FAKE_PKG]:
        del sys.modules[mod]


@pytest.fixture(autouse=True)
def _clear_simulate_mark(monkeypatch):
    """A --simulate run marks the process environment; keep tests independent of each other."""
    monkeypatch.delenv("MSW_SIMULATE", raising=False)


@pytest.fixture(autouse=True)
def _isolated_session_locks(tmp_path_factory, monkeypatch):
    """Session locks go to a temporary dir, never the user's ~/.murineshiftwork/locks."""
    monkeypatch.setattr(
        "murineshiftwork.logic.session_lock.LOCK_DIR",
        tmp_path_factory.mktemp("locks"),
    )


# ---------------------------------------------------------------------------
# Import-skip guard
#
# ``importorskip`` and ``skipif(not HAVE_X)`` turn a broken install into a green run. With
# ``--fail-on-import-skips`` every skip whose reason says a module is missing or could not be
# imported fails the session, unless its reason contains a module named by
# ``--allow-import-skip``. Platform and fixture skips never match and stay allowed. The
# allow-list is per invocation, so each CI job lists the optional packages it does not install.

_IMPORT_SKIP = re.compile(
    r"could not import|No module named|ModuleNotFoundError|ImportError|not installed",
    re.IGNORECASE,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("import-skips")
    group.addoption(
        "--fail-on-import-skips",
        action="store_true",
        help="fail when a test is skipped because a module could not be imported",
    )
    group.addoption(
        "--allow-import-skip",
        action="append",
        default=[],
        metavar="MODULE",
        help="module whose import skip is expected in this run (repeatable)",
    )


class _ImportSkipGuard:
    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed
        self.unexpected: list[tuple[str, str]] = []

    def _record(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        if not report.skipped:
            return
        longrepr = report.longrepr
        reason = str(longrepr[2]) if isinstance(longrepr, tuple) else str(longrepr)
        if _IMPORT_SKIP.search(reason) and not any(m in reason for m in self.allowed):
            self.unexpected.append((report.nodeid, reason))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._record(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self._record(report)

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.unexpected:
            return
        terminalreporter.section("unexpected import skips", red=True)
        for nodeid, reason in self.unexpected:
            terminalreporter.line(f"{nodeid}: {reason}")
        terminalreporter.line(
            "Install the module or pass --allow-import-skip=<module>."
        )

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.unexpected and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--fail-on-import-skips"):
        config.pluginmanager.register(
            _ImportSkipGuard(config.getoption("--allow-import-skip")),
            "import-skip-guard",
        )
