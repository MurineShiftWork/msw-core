"""Session log files must be UTF-8.

Regression for a Windows crash: `logging.FileHandler` defaults to the locale codec
(cp1252 on Windows), so any non-latin-1 log text (an "->" arrow U+2192, "µ", …) raised
`UnicodeEncodeError` in the handler's emit. The handlers are now created with
`encoding="utf-8"`. Asserted explicitly (not via the platform default) so it's caught on
a UTF-8 Linux CI too.
"""

import logging

from murineshiftwork.logic.log import add_session_log_handler


def _own_new_handlers(before_ids):
    root = logging.getLogger()
    return [h for h in root.handlers if id(h) not in before_ids]


def test_session_log_handler_is_utf8_and_round_trips_unicode(tmp_path):
    session_base = (
        tmp_path / "s__20260101_000000__msw__t" / "s__20260101_000000__msw__t"
    )
    session_base.parent.mkdir(parents=True)

    root = logging.getLogger()
    before = {id(h) for h in root.handlers}
    prev_level = root.level
    root.setLevel(logging.INFO)  # else the default WARNING filters the INFO record
    add_session_log_handler(str(session_base))
    try:
        new = _own_new_handlers(before)
        assert len(new) == 1
        # the fix: explicit utf-8, independent of the OS locale
        assert getattr(new[0], "encoding", None) == "utf-8"

        # a record with non-latin-1 chars must not raise and must round-trip
        logging.getLogger().info("micro µL arrow → ok")
        new[0].flush()
        log_text = next(tmp_path.rglob("*.msw.log")).read_text(encoding="utf-8")
        assert "→" in log_text
    finally:
        root.setLevel(prev_level)
        for h in _own_new_handlers(before):
            root.removeHandler(h)
            h.close()
