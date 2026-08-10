import atexit
import contextlib
import faulthandler
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from murineshiftwork.namespace import msw_file
from murineshiftwork.namespace.paths import MSW_DATETIME_FORMAT
from rich import get_console
from rich.logging import RichHandler

# IDs of handlers that MSW owns on the root logger: anything else is third-party.
_MSW_ROOT_HANDLER_IDS: set[int] = set()

_CENTRAL_LOG_DIR = Path("~/.murineshiftwork/logs").expanduser()
_MAX_LOG_FILES = 100


def _prune_central_logs() -> None:
    """Cap the central log dir: keep the newest run logs, and drop empty (non-crash) fault logs.

    Run logs are pruned to the newest ``_MAX_LOG_FILES``. ``.fault.log`` files are kept only when
    NON-empty (a real crash dump worth reviewing); an EMPTY one means the process was killed without
    a clean exit (e.g. SIGTERM) but did not crash, so atexit never removed its empty startup file.
    """
    run_logs = sorted(
        p for p in _CENTRAL_LOG_DIR.glob("*.log") if not p.name.endswith(".fault.log")
    )
    for old in run_logs[:-_MAX_LOG_FILES]:
        with contextlib.suppress(OSError):
            old.unlink()
    for fault in _CENTRAL_LOG_DIR.glob("*.fault.log"):
        with contextlib.suppress(OSError):
            if fault.stat().st_size == 0:
                fault.unlink()


def _disable_faulthandler_in_child() -> None:
    """Runs in a freshly forked child: stop inheriting the parent's faulthandler so the child's own
    crashes do not pollute the session fault log."""
    with contextlib.suppress(Exception):
        faulthandler.disable()


def _enable_fault_log(central_log_path: Path) -> tuple[Path, Callable[[], None]]:
    """Capture native crashes (SIGSEGV/SIGABRT/SIGFPE/SIGBUS) to a ``.fault.log`` beside the run log.

    A C-level crash kills the process before Python can log it, so a plain log just stops mid-write.
    ``faulthandler`` dumps every thread's Python stack to this file on a fatal signal - the one thing
    ordinary logging cannot record. The file is kept ONLY when a fault occurs: an atexit hook removes
    it on any normal exit (clean finish, sys.exit, or a Python exception), but a fatal signal
    terminates the process before atexit runs, so the dump survives. Same stem as the run log with a
    ``.fault.log`` suffix, so a crash record sits next to its run log.
    """
    fault_log_path = central_log_path.with_name(central_log_path.stem + ".fault.log")
    # kept open for faulthandler's lifetime; closed by the atexit hook below
    fault_file = fault_log_path.open("w")  # noqa: SIM115
    faulthandler.enable(file=fault_file, all_threads=True)

    # A forked child (online-plot / relay subprocess) inherits this faulthandler and its open file,
    # so the child's OWN fatal signal - e.g. a Qt plot subprocess aborting on a headless host - would
    # be written into the session fault log and read as a session crash. Disable faulthandler in any
    # child so the fault log reflects only THIS (main) process. Unix-only: Windows has no fork (it
    # uses spawn, where children do not inherit this), so the problem does not arise there.
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_disable_faulthandler_in_child)

    def _remove_fault_log_on_clean_exit() -> None:
        with contextlib.suppress(Exception):
            faulthandler.disable()
            fault_file.close()
            fault_log_path.unlink(missing_ok=True)

    atexit.register(_remove_fault_log_on_clean_exit)
    return fault_log_path, _remove_fault_log_on_clean_exit


def setup_logging(level=None, log_file=None, task="", subject="", setup=""):
    if level is None:
        level = "DEBUG"

    logger = logging.getLogger()

    if any(id(h) in _MSW_ROOT_HANDLER_IDS for h in logger.handlers):
        return
    for h in list(logger.handlers):
        logger.removeHandler(h)

    # Root at DEBUG so the DEBUG file handler receives everything; the console handler carries
    # the requested level. This decouples the two: the file is full DEBUG while the console
    # honours --log-level (records are filtered at the logger first, so without this the file
    # handler is starved to the console level).
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(message)s")
    formatter.datefmt = "%Y-%m-%d %H:%M:%S.%f"

    # Central log: one timestamped file per run; prune to _MAX_LOG_FILES
    if log_file:
        central_log_path = Path(log_file).expanduser()
        central_log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        _CENTRAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        dt = datetime.now().strftime(MSW_DATETIME_FORMAT)
        _parts = [p for p in [setup, dt, subject, task] if p]
        stem = "--".join(_parts)
        central_log_path = _CENTRAL_LOG_DIR / f"{stem}.log"
        _prune_central_logs()

    # encoding="utf-8": on Windows a FileHandler defaults to the locale codec (cp1252)
    # and raises UnicodeEncodeError on non-latin-1 log text (e.g. a "->" arrow, "µ").
    file_handler = logging.FileHandler(filename=str(central_log_path), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    _MSW_ROOT_HANDLER_IDS.add(id(file_handler))

    _enable_fault_log(central_log_path)

    logging_handler = RichHandler(
        console=get_console(),
        level=level,
        enable_link_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
    )
    logging_handler.setFormatter(formatter)
    logger.addHandler(logging_handler)
    _MSW_ROOT_HANDLER_IDS.add(id(logging_handler))

    logging.info(f"Logging to {central_log_path}")


def add_session_log_handler(session_file_path: str, level: str = "INFO"):
    """Add a per-session FileHandler writing INFO+ records to the session folder."""
    log_path = msw_file(session_file_path, "log")
    handler = logging.FileHandler(filename=str(log_path), encoding="utf-8")
    handler.setLevel(getattr(logging, level.upper()))
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    formatter.datefmt = "%Y-%m-%d %H:%M:%S"
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    _MSW_ROOT_HANDLER_IDS.add(id(handler))
    logging.info(f"Session log: {log_path}")


def patch_logging_levels(target_level="WARNING"):
    for m in ["pybpodapi", "pybpodgui_api", "pybpodgui_plugin", "matplotlib"]:
        logger = logging.getLogger(m)
        logger.setLevel(target_level)


def suppress_third_party_console_handlers():
    """Remove foreign StreamHandlers from all loggers to eliminate duplicate console output."""

    def _is_foreign_stream_handler(h: logging.Handler) -> bool:
        return isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        )

    root = logging.getLogger()
    for handler in list(root.handlers):
        if (
            _is_foreign_stream_handler(handler)
            and id(handler) not in _MSW_ROOT_HANDLER_IDS
        ):
            root.removeHandler(handler)
            logging.debug("Removed foreign StreamHandler from root logger")

    for name, logger in logging.Logger.manager.loggerDict.items():
        if not isinstance(logger, logging.Logger):
            continue
        for handler in list(logger.handlers):
            if _is_foreign_stream_handler(handler):
                logger.removeHandler(handler)
                logging.debug(f"Removed StreamHandler from logger '{name}'")


def json_dumps_type_safe(data):
    return json.dumps(
        data,
        skipkeys=True,
        sort_keys=True,
        indent=4,
        default=lambda x: f"<NoJSON:{type(x)}>",
    )
