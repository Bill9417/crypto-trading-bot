"""🧵 Meta Threads daily post.

Network is fully stubbed — a test must never reach graph.threads.net, and must
never touch the real threads_state.json (it holds a live 60-day token).
"""
from datetime import datetime

import pytest

import threads_post as T


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "STATE_FILE", str(tmp_path / "threads.json"))
    monkeypatch.setattr(T, "APP_ID", "app123")
    monkeypatch.setattr(T, "APP_SECRET", "secret456")
    monkeypatch.setattr(T, "ENABLED", True)
    monkeypatch.setattr(T.requests, "post",
                        lambda *a, **k: pytest.fail("test hit the network (post)"))
    monkeypatch.setattr(T.requests, "get",
                        lambda *a, **k: pytest.fail("test hit the network (get)"))


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _connect(user_id="u1", age_s=0):
    import time
    T._save_state({"access_token": "tok", "user_id": user_id,
                   "obtained_at": time.time() - age_s})


DATA = {
    "prices": {"BTC": {"last": 63094.0, "pct": 1.2},
               "ETH": {"last": 2043.7, "pct": -0.8},
               "SOL": {"last": 93.5, "pct": 2.1}},
    "fng": {"value": 42, "label": "Fear"},
    "global": {"btc_dominance": 58.3},
    "today_events": ["• 21:30 美國 CPI"],
    "signals": {"n": 12, "premium": 3},
    "outcomes": {"n": 18, "hit_pct": 56.0},
}
NOW = datetime(2026, 8, 3, 9, 0)


# ── length, by Meta's documented rule ────────────────────────────────────────
def test_plain_text_counts_one_per_character():
    assert T.threads_len("hello") == 5
    assert T.threads_len("加密市場") == 4          # CJK is not byte-counted


def test_emoji_costs_its_utf8_bytes():
    """Meta counts emojis as UTF-8 bytes, so a rocket costs 4, not 1. Missing
    this is how a post gets silently rejected."""
    assert T.threads_len("🚀") == 4
    assert T.threads_len("a🚀b") == 6


def test_empty_and_none_are_zero():
    assert T.threads_len("") == 0 and T.threads_len(None) == 0


# ── the post itself ──────────────────────────────────────────────────────────
def test_post_fits_metas_limit():
    body = T.build_post(DATA, NOW)
    assert 0 < T.threads_len(body) <= T.MAX_LEN


def test_post_carries_the_market_facts():
    body = T.build_post(DATA, NOW)
    assert "BTC" in body and "63,094" in body.replace(",", ",")
    assert "恐懼貪婪 42" in body and "58.3%" in body
    assert "CPI" in body
    assert "12 個訊號" in body


def test_post_is_account_free():
    """This is PUBLIC marketing. A balance, a position or a P&L must never
    reach it — same rule the Telegram morning brief is held to."""
    body = T.build_post(DATA, NOW)
    for banned in ("USDT", "餘額", "淨值", "持倉", "未實現", "保證金",
                   "Bybit", "Binance", "帳戶"):
        assert banned not in body, f"account data leaked into a public post: {banned}"


def test_a_long_calendar_headline_cannot_overflow():
    data = dict(DATA, today_events=["• " + "超長的美國經濟數據標題" * 30])
    body = T.build_post(data, NOW)
    assert T.threads_len(body) <= T.MAX_LEN


def test_missing_pieces_degrade_quietly():
    body = T.build_post({}, NOW)
    assert T.threads_len(body) <= T.MAX_LEN and "早報" in body


def test_thin_outcome_sample_is_not_advertised():
    """Fewer than 5 settled signals is not a track record worth publishing."""
    body = T.build_post(dict(DATA, outcomes={"n": 3, "hit_pct": 100.0}), NOW)
    assert "先到目標" not in body


# ── publishing is two calls ──────────────────────────────────────────────────
def test_container_refuses_an_oversized_post(monkeypatch):
    _connect()
    res = T.create_container("x" * (T.MAX_LEN + 1))
    assert not res["ok"] and "refusing" in res["error"]


def test_container_requires_a_connection():
    res = T.create_container("hi")
    assert not res["ok"] and "not connected" in res["error"]


def test_container_then_publish(monkeypatch):
    _connect()
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        return _Resp({"id": "c1" if url.endswith("/threads") else "p1"})

    monkeypatch.setattr(T.requests, "post", fake_post)
    c = T.create_container("hello", link="https://example.test/welcome")
    assert c == {"ok": True, "creation_id": "c1"}
    assert calls[0][1]["media_type"] == "TEXT"
    assert calls[0][1]["link_attachment"] == "https://example.test/welcome"
    p = T.publish_container("c1")
    assert p == {"ok": True, "id": "p1"}
    assert calls[1][0].endswith("/threads_publish")
    assert calls[1][1]["creation_id"] == "c1"


def test_meta_error_is_surfaced_not_swallowed(monkeypatch):
    _connect()
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(
        {"error": {"message": "Invalid OAuth access token"}}))
    res = T.create_container("hi")
    assert not res["ok"] and "Invalid OAuth" in res["error"]


# ── scheduling: container on one tick, publish on the next ───────────────────
def _gather_stub(monkeypatch):
    import morning_brief
    monkeypatch.setattr(morning_brief, "_gather", lambda c, n: DATA)


def test_tick_creates_a_container_but_does_not_publish(monkeypatch):
    _connect()
    _gather_stub(monkeypatch)
    monkeypatch.setattr(T, "POST_HOUR", 0)
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp({"id": "c9"}))
    assert T.tick(None) is False                      # nothing public yet
    st = T._load_state()
    assert st["pending"]["creation_id"] == "c9"
    assert "last_post" not in st


def test_next_tick_publishes_after_the_delay(monkeypatch):
    import time
    _connect()
    _gather_stub(monkeypatch)
    monkeypatch.setattr(T, "POST_HOUR", 0)
    T._save_state(dict(T._load_state(),
                       pending={"creation_id": "c9", "date": "2026-08-03",
                                "ts": time.time() - 999}))
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp({"id": "p9"}))
    assert T.tick(None) is True
    st = T._load_state()
    assert st["last_post"] == "2026-08-03" and st["last_post_id"] == "p9"
    assert "pending" not in st


def test_container_is_not_published_before_the_delay(monkeypatch):
    import time
    _connect()
    T._save_state(dict(T._load_state(),
                       pending={"creation_id": "c9", "date": "2026-08-03",
                                "ts": time.time()}))
    assert T.tick(None) is False                      # requests stub would fail
    assert T._load_state()["pending"]["creation_id"] == "c9"


def test_a_failed_publish_drops_the_container(monkeypatch):
    """Retrying a stale creation_id just re-fails; double-posting is worse."""
    import time
    _connect()
    T._save_state(dict(T._load_state(),
                       pending={"creation_id": "c9", "date": "2026-08-03",
                                "ts": time.time() - 999}))
    monkeypatch.setattr(T.requests, "post",
                        lambda *a, **k: _Resp({"error": {"message": "expired"}}))
    assert T.tick(None) is False
    st = T._load_state()
    assert "pending" not in st and "last_post" not in st


def test_tick_posts_once_a_day(monkeypatch):
    _connect()
    _gather_stub(monkeypatch)
    monkeypatch.setattr(T, "POST_HOUR", 0)
    T._save_state(dict(T._load_state(),
                       last_post=T.datetime.now(T.TZ).strftime("%Y-%m-%d")
                       if T.TZ else T.datetime.now().strftime("%Y-%m-%d")))
    assert T.tick(None) is False                      # requests stub would fail


def test_tick_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(T, "ENABLED", False)
    assert T.tick(None) is False


def test_tick_is_a_no_op_when_not_connected(monkeypatch):
    monkeypatch.setattr(T, "POST_HOUR", 0)
    assert T.tick(None) is False


def test_tick_never_raises(monkeypatch):
    _connect()
    monkeypatch.setattr(T, "POST_HOUR", 0)
    import morning_brief
    monkeypatch.setattr(morning_brief, "_gather",
                        lambda c, n: (_ for _ in ()).throw(RuntimeError("boom")))
    assert T.tick(None) is False                      # logged, not raised


# ── tokens ───────────────────────────────────────────────────────────────────
def test_refresh_is_skipped_inside_the_week(monkeypatch):
    _connect(age_s=3 * 86400)
    assert T.refresh_if_due() == {"ok": True, "skipped": True}


def test_refresh_is_skipped_under_24h_even_when_forced():
    """Meta refuses to refresh a token younger than 24 hours."""
    _connect(age_s=3600)
    assert T.refresh_if_due(force=True) == {"ok": True, "skipped": True}


def test_refresh_replaces_the_token(monkeypatch):
    _connect(age_s=8 * 86400)
    monkeypatch.setattr(T.requests, "get", lambda *a, **k: _Resp(
        {"access_token": "newtok", "expires_in": 5183944}))
    assert T.refresh_if_due()["refreshed"] is True
    assert T._load_state()["access_token"] == "newtok"


def test_refresh_failure_keeps_the_old_token(monkeypatch):
    _connect(age_s=8 * 86400)
    monkeypatch.setattr(T.requests, "get", lambda *a, **k: _Resp(
        {"error": {"message": "token expired"}}))
    res = T.refresh_if_due()
    assert not res["ok"] and T._load_state()["access_token"] == "tok"


def test_exchange_upgrades_short_to_long_lived(monkeypatch):
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(
        {"access_token": "short", "user_id": "42"}))
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append((url, params))
        if url.endswith("/access_token"):
            return _Resp({"access_token": "long60d", "expires_in": 5183944})
        return _Resp({"username": "wolfscanner"})

    monkeypatch.setattr(T.requests, "get", fake_get)
    res = T.exchange_code("abc")
    assert res["ok"] and res["username"] == "wolfscanner"
    st = T._load_state()
    assert st["access_token"] == "long60d" and st["user_id"] == "42"
    assert seen[0][1]["grant_type"] == "th_exchange_token"


def test_auth_url_carries_the_publish_scope():
    url = T.auth_url()
    assert url.startswith(T.AUTHORIZE_URL)
    assert "threads_content_publish" in url and "response_type=code" in url
