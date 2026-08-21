"""📡 Push, don't poll — and the thread budget that makes it risky.

Measured 2026-08-21: the age of a number on a card is (poll interval + upstream
cadence). /api/zones polled every 120s against a scanner writing every 300s, so
up to 120 of those seconds were the browser not having asked yet. A push removes
that half; nothing removes the other half, which is why every event carries the
per-feed age and the cards print it.

THE RISK IS THREADS. waitress serves this app with 12 workers and an SSE
response holds one for as long as it is open. The cap, the self-termination and
the poll fallback are the three things standing between "instant dashboard" and
"the site stops answering after four tabs", so they are what this file tests.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import pytest

import live_feed as L

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Point the watcher at a scratch dir — a test must never make the live
    dashboard think a scanner just wrote."""
    monkeypatch.setattr(L, "_DIR", str(tmp_path))
    monkeypatch.setattr(L, "FEEDS", {"alpha": "a.json", "beta": "b.json"})
    return tmp_path


def _clock():
    t = [1000.0]
    return t, (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def _events(t, now, sleep, n=6):
    return list(L.events(now_fn=now, sleep_fn=sleep, max_iterations=n))


def _kind(frame):
    return frame.split("\n")[0].replace("event: ", "")


def _data(frame):
    return json.loads(frame.split("data: ", 1)[1])


# ── change detection ─────────────────────────────────────────────────────────
def test_a_write_produces_a_push_naming_the_feed(scratch):
    (scratch / "a.json").write_text("{}")
    t, now, sleep = _clock()
    gen = L.events(now_fn=now, sleep_fn=sleep, max_iterations=6)
    first = next(gen)
    assert _kind(first) == "hello"
    time.sleep(0.01)
    (scratch / "a.json").write_text('{"x":1}')
    os.utime(scratch / "a.json", (time.time() + 5, time.time() + 5))
    changed = [f for f in gen if _kind(f) == "changed"]
    assert changed, "a state-file write produced no push"
    assert _data(changed[0])["feeds"] == ["alpha"]


def test_a_feed_appearing_for_the_first_time_counts(scratch):
    """A scanner's FIRST write is exactly when a card most wants to stop
    saying 還沒跑第一輪."""
    t, now, sleep = _clock()
    gen = L.events(now_fn=now, sleep_fn=sleep, max_iterations=6)
    next(gen)
    (scratch / "b.json").write_text("{}")
    changed = [f for f in gen if _kind(f) == "changed"]
    assert changed and "beta" in _data(changed[0])["feeds"]


def test_an_unchanged_file_produces_no_push(scratch):
    (scratch / "a.json").write_text("{}")
    t, now, sleep = _clock()
    frames = _events(t, now, sleep, n=5)
    assert not [f for f in frames if _kind(f) == "changed"]


def test_a_missing_file_is_absent_not_zero(scratch):
    """'never written' and 'written at the epoch' are different facts. A
    reader diffing against 0.0 would report a change the first time the file
    appeared — and an age of 56 years."""
    (scratch / "a.json").write_text("{}")
    snap = L.snapshot()
    assert "alpha" in snap and "beta" not in snap
    assert "beta" not in L.ages()


# ── the honest half ──────────────────────────────────────────────────────────
def test_every_frame_carries_the_per_feed_age(scratch):
    """Pushing removes the poll lag and CANNOT remove the upstream cadence.
    The age is how a card tells the truth about the second half."""
    (scratch / "a.json").write_text("{}")
    os.utime(scratch / "a.json", (time.time() - 300, time.time() - 300))
    t, now, sleep = _clock()
    hello = _data(next(L.events(now_fn=time.time, sleep_fn=sleep,
                                max_iterations=1)))
    assert "ages" in hello
    assert 295 < hello["ages"]["alpha"] < 400, hello["ages"]


def test_a_quiet_stream_still_heartbeats(scratch):
    """'connected and quiet' and 'the connection died' look identical on
    screen without one."""
    (scratch / "a.json").write_text("{}")
    t, now, sleep = _clock()
    frames = _events(t, now, sleep, n=int(L.HEARTBEAT_SEC / L.POLL_SEC) + 2)
    beats = [f for f in frames if _kind(f) == "beat"]
    assert beats, "an idle stream sent nothing at all"
    assert "ages" in _data(beats[0])


# ── the thread budget ────────────────────────────────────────────────────────
def test_a_stream_ends_itself(scratch):
    """Unbounded is a thread leak with good manners. The browser reconnects."""
    (scratch / "a.json").write_text("{}")
    t, now, sleep = _clock()
    frames = _events(t, now, sleep, n=int(L.MAX_AGE_SEC / L.POLL_SEC) + 5)
    assert _kind(frames[-1]) == "bye"
    assert _data(frames[-1])["reason"] == "max_age"


def test_the_cap_refuses_rather_than_queueing():
    """A queued stream holds a worker thread while doing nothing, which is the
    failure the cap exists to prevent."""
    L._open_streams = 0
    try:
        got = [L.acquire() for _ in range(L.MAX_STREAMS + 3)]
        assert got[:L.MAX_STREAMS] == [True] * L.MAX_STREAMS
        assert got[L.MAX_STREAMS:] == [False, False, False]
        assert L.open_streams() == L.MAX_STREAMS
    finally:
        L._open_streams = 0


def test_the_heartbeat_is_fast_enough_to_notice_a_closed_tab():
    """WSGI gives the generator no disconnect callback — a dead socket is only
    discovered when something tries to WRITE to it, so the heartbeat IS the
    disconnect detector. Measured under waitress: a closed tab releases its
    slot after about two beats, and not before.

    With MAX_STREAMS slots against 12 worker threads, detection has to be much
    faster than the backstop or a few opened-and-closed tabs tie up the cap
    for the whole of MAX_AGE_SEC.
    """
    assert L.HEARTBEAT_SEC * 2 < L.MAX_AGE_SEC / 3, (
        f"beat {L.HEARTBEAT_SEC}s vs backstop {L.MAX_AGE_SEC}s — a closed tab "
        f"would hold a worker thread far longer than it needs to")
    assert L.HEARTBEAT_SEC >= 5, "beating this often is chatty for no gain"


def test_the_stream_sets_no_hop_by_hop_headers():
    """PEP 3333 forbids a WSGI app from setting Connection/Keep-Alive, and
    waitress raises AssertionError on one — the endpoint 500s in production
    while Flask's dev server and test client both accept it silently. That is
    exactly how this shipped broken once: every test passed.
    """
    import ast
    with open(os.path.join(APP_DIR, "app.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "api_stream")
    keys = [n.slice.value for n in ast.walk(fn)
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)]
    banned = {"connection", "keep-alive", "transfer-encoding", "upgrade",
              "proxy-authenticate", "te", "trailers"}
    hit = [k for k in keys if k.lower() in banned]
    assert not hit, f"hop-by-hop header set on a WSGI response: {hit}"


def test_the_cap_leaves_room_for_ordinary_requests():
    """12 waitress threads. A cap at or above that would let streams starve
    the pages they exist to update."""
    assert L.MAX_STREAMS <= 6, (
        f"MAX_STREAMS={L.MAX_STREAMS} against 12 worker threads leaves too "
        f"little room for normal requests")


def test_releasing_never_goes_negative():
    L._open_streams = 0
    L.release()
    assert L.open_streams() == 0


def test_slots_are_thread_safe():
    L._open_streams = 0
    try:
        ok = []
        ths = [threading.Thread(target=lambda: ok.append(L.acquire()))
               for _ in range(40)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        assert sum(1 for x in ok if x) == L.MAX_STREAMS, \
            f"{sum(1 for x in ok if x)} slots handed out, cap is {L.MAX_STREAMS}"
    finally:
        L._open_streams = 0


# ── the endpoint ─────────────────────────────────────────────────────────────
def _client():
    import app as APP
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def test_the_endpoint_refuses_when_full():
    L._open_streams = L.MAX_STREAMS
    try:
        r = _client().get("/api/stream")
        assert r.status_code == 503
        assert r.get_json()["max"] == L.MAX_STREAMS
    finally:
        L._open_streams = 0


def test_a_refused_stream_releases_no_slot():
    """Releasing a slot never taken would drift the counter down until the cap
    stopped capping."""
    L._open_streams = L.MAX_STREAMS
    try:
        _client().get("/api/stream")
        assert L.open_streams() == L.MAX_STREAMS
    finally:
        L._open_streams = 0


def test_the_response_is_not_buffered_or_compressed():
    """A proxy buffering the stream turns 'instant' into 'whenever the buffer
    flushes', and gzip would do the same thing locally."""
    L._open_streams = 0
    r = _client().get("/api/stream")
    try:
        assert r.mimetype == "text/event-stream"
        assert r.headers.get("X-Accel-Buffering") == "no"
        assert "no-cache" in (r.headers.get("Cache-Control") or "")
        assert not r.headers.get("Content-Encoding"), "the stream was gzipped"
    finally:
        r.close()
        L._open_streams = 0


def test_the_stream_needs_a_login():
    import app as APP
    APP.app.config["TESTING"] = True
    r = APP.app.test_client().get("/api/stream")
    assert r.status_code in (301, 302, 401), r.status_code


def test_feed_ages_endpoint_reports_the_truth():
    r = _client().get("/api/feed_ages")
    assert r.status_code == 200
    d = r.get_json()
    assert "ages" in d and "max_streams" in d
    assert all(v >= 0 for v in d["ages"].values())


# ── the wiring ───────────────────────────────────────────────────────────────
def test_every_watched_file_belongs_to_a_real_producer():
    """A feed pointing at a file nothing writes would never push, and would
    look exactly like a quiet scanner."""
    missing = [f"{feed}:{name}" for feed, name in L.FEEDS.items()
               if not os.path.exists(os.path.join(APP_DIR, name))]
    assert not missing, f"watched files that do not exist: {missing}"


def test_the_nav_loads_live_js_after_poll_js():
    """wolfLive falls back to wolfPoll, so poll.js must be defined first."""
    with open(os.path.join(APP_DIR, "templates", "_nav.html"),
              encoding="utf-8") as f:
        nav = f.read()
    assert nav.index("poll.js") < nav.index("live.js")
    tag = nav[nav.rindex("<script", 0, nav.index("live.js")):
              nav.index(">", nav.index("live.js"))]
    assert "?v=" in tag, "no cache-bust: browsers pin static files for 7 days"


def test_pushed_cards_subscribe_to_a_feed_that_exists():
    """A typo in the feed name is silent — the card just never gets pushed and
    quietly falls back to its (now slower) poll."""
    import re
    with open(os.path.join(APP_DIR, "templates", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    used = set(re.findall(r"wolfLive\('([a-z0-9_]+)'", html))
    assert used, "no card is on push at all"
    unknown = sorted(used - set(L.FEEDS))
    assert not unknown, f"cards subscribe to feeds that do not exist: {unknown}"


def test_every_pushed_card_keeps_a_fallback_poll():
    """A card whose only trigger is the stream freezes the first time the
    stream is refused — and it IS refused, by design, past the cap."""
    import re
    with open(os.path.join(APP_DIR, "templates", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    calls = re.findall(r"wolfLive\('([a-z0-9_]+)',\s*\w+,\s*(\d+)\)", html)
    assert calls
    for feed, ms in calls:
        assert int(ms) > 0, f"{feed} has no fallback interval"
        assert int(ms) <= 600000, f"{feed} fallback is {ms}ms — too slow to rescue"


def test_live_js_runs_and_exposes_its_api():
    """Executed, not grepped: a syntax error would leave every migrated card
    with no trigger at all, and the page would look fine."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    shim = """
    global.window = global;
    global.document = { querySelector: () => null, addEventListener: () => {},
                        createElement: () => ({ style: {} }) };
    global.setInterval = () => {};
    """
    src = open(os.path.join(APP_DIR, "static", "live.js"), encoding="utf-8").read()
    prog = shim + src + """
    console.log(JSON.stringify({
      live: typeof window.wolfLive,
      age: window.wolfLiveInfo.age('nope'),
      near: window.wolfLiveInfo.human(5),
      far: window.wolfLiveInfo.human(7200),
      connected: window.wolfLiveInfo.connected()
    }));"""
    p = subprocess.run(["node", "-e", prog], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0, p.stderr
    d = json.loads(p.stdout.strip().split("\n")[-1])
    assert d["live"] == "function"
    assert d["age"] is None, "an unknown feed reported an age"
    assert d["near"] == "剛更新" and "小時前" in d["far"]
    assert d["connected"] is False, "claimed connected with no EventSource"


# ── what the age chips found ─────────────────────────────────────────────────
def test_every_outcome_book_that_records_also_settles():
    """供需區 recorded signals for weeks and settled NONE of them, because
    zone_outcomes.tick() existed and nothing called it. The book filled to 76
    rows against a MAX_CONCURRENT of 8, record() then correctly refused every
    new signal, note() declined to save, and zone_outcomes.json stopped being
    written at all — while the card showed a backtest beside an empty live
    record. Nothing surfaced it until a per-card data-age chip did.

    A book that only ever records is a claim nobody can falsify.
    """
    import ast
    with open(os.path.join(APP_DIR, "strategy2_scanner.py"), encoding="utf-8") as f:
        sweep = f.read()
    tree = ast.parse(sweep)
    called = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)}
    books = ["flip_outcomes", "zone_outcomes", "vegas_outcomes"]
    for book in books:
        recorded = f"{book}.note" in called or f"{book}.record" in called
        settled = f"{book}.tick" in called
        if recorded:
            assert settled, (
                f"{book} records signals but nothing settles them — its live "
                f"record can only ever be empty")
