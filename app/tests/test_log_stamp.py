"""🕐 Timestamped log lines — because "is this error current?" was unanswerable.

Three engine logs were bare print() output with no time on any line. The cost
was not theoretical: an audit on 2026-08-13 used line position as a proxy for
recency and reported a live 500 that was 24 days old, and the timestamp-aware
replacement then reported "0 errors in 24 hours" for those same three logs —
vacuously, because it found no timestamps to parse and skipped every line.

A check that reports perfect health for the files it cannot read is worse than
no check, so these tests are mostly about the wrapper being un-fool-able.
"""
import io
import re
import sys

import log_stamp

STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def _wrap():
    buf = io.StringIO()
    return log_stamp._Stamped(buf), buf


def test_a_line_gets_a_timestamp():
    s, buf = _wrap()
    s.write("hello\n")
    assert STAMP.match(buf.getvalue())
    assert buf.getvalue().rstrip().endswith("hello")


def test_print_emits_text_and_newline_separately_and_still_gets_one_stamp():
    """THE implementation trap. print() calls write() twice — once for the
    text, once for "\\n". Stamping per call instead of per line puts a
    timestamp in the middle of the output."""
    s, buf = _wrap()
    s.write("[strategy2] sweep done")
    s.write("\n")
    out = buf.getvalue()
    assert out.count("[strategy2]") == 1
    assert len(STAMP.findall(out)) == 0 or out.startswith("[")
    # exactly one timestamp, at the very start
    assert len(re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out)) == 1


def test_every_line_of_a_multiline_write_is_stamped():
    """Tracebacks arrive as one multi-line write. One stamp on the first line
    would leave the frames unattributed, which is precisely the part you need
    to date."""
    s, buf = _wrap()
    s.write("Traceback (most recent call last):\n  File x\n  TypeError\n")
    lines = [l for l in buf.getvalue().split("\n") if l]
    assert len(lines) == 3
    assert all(STAMP.match(l) for l in lines), buf.getvalue()


def test_a_blank_line_is_not_stamped():
    """Spacer lines are used for readability; stamping them turns every gap
    into noise."""
    s, buf = _wrap()
    s.write("\n")
    assert buf.getvalue() == "\n"


def test_the_text_is_never_altered():
    s, buf = _wrap()
    s.write("值 100% · 中文 · <pre>x</pre>\n")
    assert "值 100% · 中文 · <pre>x</pre>" in buf.getvalue()


def test_install_is_idempotent():
    """A module calling this at import AND a __main__ calling it again would
    otherwise double-stamp every line for the rest of the process."""
    real_out, real_err = sys.stdout, sys.stderr
    try:
        assert log_stamp.install() is True
        assert log_stamp.install() is False
        assert isinstance(sys.stdout, log_stamp._Stamped)
        assert not isinstance(sys.stdout._s, log_stamp._Stamped)
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def test_the_wrapper_still_looks_like_a_stream():
    """waitress and ccxt poke at fileno()/isatty(). A wrapper that hides them
    breaks logging in ways that only show up in production."""
    s, buf = _wrap()
    s.flush()
    assert hasattr(s, "getvalue")            # passthrough via __getattr__
    assert s.write("") == 0


def test_every_long_running_entry_point_installs_it():
    """The point is that ALL the engine logs become datable. One left out is
    the one whose error you cannot place."""
    import os
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fn in ("strategy2_scanner.py", "strategy3_scanner.py", "bot.py", "app.py"):
        src = open(os.path.join(app_dir, fn), encoding="utf-8").read()
        main = src[src.index('if __name__ == "__main__":'):]
        assert "log_stamp.install()" in main, f"{fn} writes undatable log lines"
