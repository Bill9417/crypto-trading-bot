"""
📡 Push, don't poll — tell the browser the moment new data lands.

Measured 2026-08-21, end to end, per dashboard card:

    age of the number on screen = poll interval + upstream cadence

and those two are fixed by completely different things. /api/zones polls every
120 seconds against a scanner that writes every 300, so up to 120 of those
seconds are the browser simply not having asked yet. Refreshing faster would
burn requests to shave a delay that a single push removes entirely.

The other half CANNOT be shortened by refreshing at all. 隧道翻多 reads closed
1h bars — polling it every second would return the same answer 3,600 times.
Pretending otherwise is the failure mode this repo keeps finding: a number
that LOOKS live because it is on a live-looking page. So this module does two
things and is careful about which is which:

  · pushes an event the moment a state file changes, killing the poll lag
  · ships `age` with every push, so a card can say how old its data really is

WHAT IT WATCHES is file mtimes, not application events. Every producer here
already writes its state atomically via os.replace, which bumps mtime exactly
once per completed write — so the file system is already an accurate,
zero-coupling change log. An in-process event bus would have to be wired into
four separate processes (web, bot, S2, S3); mtime works across all of them for
free and cannot get out of sync with what a reader would actually load.

── HOLDING A THREAD IS THE RISK ────────────────────────────────────────────
waitress serves this app with 12 worker threads and an SSE response occupies
one for as long as it is open. Three tabs is three threads; a leaked
connection is a thread gone until restart. So:

  · MAX_STREAMS caps concurrent streams and the cap REFUSES rather than
    queueing, because a queued stream holds a thread while doing nothing
  · every stream ends itself after MAX_AGE_SEC and the browser reconnects —
    a bounded leak instead of a permanent one
  · the poll fallback stays in place, so refusing a stream degrades to
    exactly today's behaviour rather than to a dead card
"""
import json
import os
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))

# feed name -> the state file whose mtime means "there is new data".
# The name is what the browser subscribes to; it matches the card's data-card
# id where there is one, so the wiring reads the same on both sides.
FEEDS = {
    "zones": "zone_outcomes.json",
    "flips": "flip_outcomes.json",
    "thrust": "thrust_state.json",
    "vegas": "vegas_state.json",
    "watch": "daily_watch.json",
    "s4": "strategy4_signals.json",
    "signals": "strategy2_signals.json",
    "scan": "scan_results.json",
    "oi": "crowd_radar_state.json",
    "outcomes": "signal_outcomes.json",
    "whale": "whale_state.json",
    "liq": "liquidations_buffer.json",
}

POLL_SEC = float(os.getenv("LIVE_FEED_POLL_SEC", "1.0"))

# The heartbeat is NOT only for keeping a proxy from reaping an idle stream.
# It is also how a vanished client is noticed at all: WSGI gives the generator
# no disconnect callback, so the socket is only discovered dead when something
# tries to WRITE to it. Measured under waitress — a closed tab releases its
# slot after about two heartbeats (6s at a 3s beat), and NOT before. With no
# heartbeat the slot is held until MAX_AGE_SEC regardless.
#
# So this value sets disconnect-detection time, not just liveness.
HEARTBEAT_SEC = float(os.getenv("LIVE_FEED_HEARTBEAT_SEC", "20"))

# The backstop for the case a heartbeat somehow succeeds into the void. 300s
# rather than 600 because a slot is a worker thread: with MAX_STREAMS of them,
# the worst case is what a burst of opened-and-closed tabs can tie up, and the
# browser reconnecting every 5 minutes costs one request.
MAX_AGE_SEC = float(os.getenv("LIVE_FEED_MAX_AGE_SEC", "300"))
MAX_STREAMS = int(os.getenv("LIVE_FEED_MAX_STREAMS", "4"))

_lock = threading.Lock()
_open_streams = 0


def snapshot() -> dict:
    """{feed: mtime} for every watched file. A missing file is absent from the
    map rather than 0.0 — "never written" and "written at the epoch" are
    different facts, and a reader diffing against 0.0 would report a change
    the first time the file appeared."""
    out = {}
    for feed, name in FEEDS.items():
        try:
            out[feed] = os.path.getmtime(os.path.join(_DIR, name))
        except OSError:
            continue
    return out


def ages(now: float = None) -> dict:
    """{feed: seconds since it was last written}. This is the honest answer to
    "how fresh is this card", and it is the half that pushing cannot fix."""
    now = now if now is not None else time.time()
    return {f: max(0.0, round(now - m, 1)) for f, m in snapshot().items()}


def diff(before: dict, after: dict) -> list:
    """Feeds that changed. A feed appearing for the first time counts — that
    is a scanner's first write, which is exactly when a card most wants to
    stop saying "還沒跑第一輪"."""
    return sorted(f for f, m in after.items() if before.get(f) != m)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def acquire() -> bool:
    """Take a stream slot, or False when the cap is reached."""
    global _open_streams
    with _lock:
        if _open_streams >= MAX_STREAMS:
            return False
        _open_streams += 1
        return True


def release() -> None:
    global _open_streams
    with _lock:
        _open_streams = max(0, _open_streams - 1)


def open_streams() -> int:
    with _lock:
        return _open_streams


def events(now_fn=time.time, sleep_fn=time.sleep, max_iterations: int = None):
    """The SSE generator. Yields strings; the caller owns the slot.

    Sends the CURRENT state first so a card that connects mid-cycle does not
    sit on stale data until the next write. Then one event per change, plus a
    heartbeat so an idle connection is not reaped by a proxy — and so the
    browser can tell "connected and quiet" from "connection died", which are
    the same thing on screen otherwise.
    """
    started = now_fn()
    last = snapshot()
    yield _sse("hello", {"feeds": sorted(FEEDS), "ages": ages(started),
                         "max_age_sec": MAX_AGE_SEC})
    beat = started
    i = 0
    while True:
        if max_iterations is not None and i >= max_iterations:
            return
        i += 1
        sleep_fn(POLL_SEC)
        now = now_fn()
        if now - started >= MAX_AGE_SEC:
            # Bounded on purpose: the browser reconnects and the thread is
            # returned. An unbounded stream is a thread leak with good manners.
            yield _sse("bye", {"reason": "max_age"})
            return
        cur = snapshot()
        changed = diff(last, cur)
        if changed:
            last = cur
            yield _sse("changed", {"feeds": changed, "ages": ages(now)})
            beat = now
        elif now - beat >= HEARTBEAT_SEC:
            beat = now
            yield _sse("beat", {"ages": ages(now)})
