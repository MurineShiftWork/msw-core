"""After a stop is requested, further Ctrl+C presses warn instead of interrupting shutdown."""

import logging
import signal

import pytest

from murineshiftwork.logic.task_process import (
    FORCE_QUIT_PRESSES,
    TaskProcess,
    _guard_shutdown_from_ctrl_c,
)


@pytest.fixture
def restore_sigint():
    old = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, old)


def test_extra_presses_warn_then_force_quit(restore_sigint, caplog):
    _guard_shutdown_from_ctrl_c()
    handler = signal.getsignal(signal.SIGINT)
    with caplog.at_level(logging.WARNING):
        for _ in range(FORCE_QUIT_PRESSES - 1):
            handler(signal.SIGINT, None)  # no exception: shutdown continues
    assert caplog.text.count("Still shutting down cleanly") == FORCE_QUIT_PRESSES - 1
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_stop_task_announces_once(restore_sigint, caplog):
    tp = TaskProcess.__new__(TaskProcess)  # stop_task needs no session set-up
    tp.task_runner = None
    with caplog.at_level(logging.WARNING):
        tp.stop_task()
        tp.stop_task()
    assert caplog.text.count("Session end requested") == 1
    assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
