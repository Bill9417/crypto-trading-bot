"""
🕐 Put a timestamp on every log line.

strategy2.log, strategy3.log and bot.log had NO timestamps — every line was
bare `print()` output. That makes the single most common question about a log
unanswerable: *is this error from ten minutes ago or from July?*

It cost real time three separate ways in one day (2026-08-13):
  · tailscale_autofix.log recorded 21 Funnel re-arms with no way to tell
    21-in-an-hour from 21-in-a-month — and those have different causes and
    different fixes, so the right next step was genuinely unknown
  · an audit scanned the last N LINES as a proxy for recency and reported a
    "live" 500 on /api/liq_heatmap that turned out to be 24 days old, because
    line position is not time in an append-only file
  · the timestamp-aware version of that scan then reported "0 errors in 24h"
    for three logs, which was VACUOUS — it never parsed a timestamp, because
    there were none, so it silently skipped every line

That last one is the dangerous shape: a check that cannot fail loudly. It
reported perfect health for exactly the files it could not read.

Wrapping the stream rather than editing call sites: there are hundreds of
print() calls across these engines, prefixing them by hand would be a large
diff that the next print undoes, and the format would drift between modules.
"""
import datetime
import sys

FMT = "%Y-%m-%d %H:%M:%S"


class _Stamped:
    """A write-through stream that prefixes each new line with the time.

    Tracks whether the previous write ended a line, because print() emits the
    text and the "\\n" as separate write() calls — stamping per call rather
    than per line would put a timestamp in the middle of the output.
    """

    def __init__(self, stream):
        self._s = stream
        self._fresh = True          # next non-empty write starts a line

    def write(self, data):
        if not data:
            return 0
        stamp = f"[{datetime.datetime.now().strftime(FMT)}] "
        out = []
        for chunk in data.splitlines(keepends=True):
            if self._fresh and chunk.strip():
                out.append(stamp)
            out.append(chunk)
            self._fresh = chunk.endswith("\n")
        self._s.write("".join(out))
        return len(data)

    def flush(self):
        self._s.flush()

    def __getattr__(self, name):        # isatty, fileno, encoding, …
        return getattr(self._s, name)


def install(stderr: bool = True) -> bool:
    """Stamp stdout (and stderr). Idempotent — returns False if already on.

    Idempotence matters: a module that calls this at import time and a
    __main__ that calls it again would otherwise double-stamp every line.
    """
    if isinstance(sys.stdout, _Stamped):
        return False
    sys.stdout = _Stamped(sys.stdout)
    if stderr and not isinstance(sys.stderr, _Stamped):
        sys.stderr = _Stamped(sys.stderr)
    return True
