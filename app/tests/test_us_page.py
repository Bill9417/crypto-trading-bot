"""The public 美股 page — us_market.web_view() and the /us route.

/tw and /us are the only no-login pages, opened from a LINE link on a phone, so
the bar is: never 500, never mislabel a live session as a close, and never leak
anything account-related.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import us_market as um
from test_us_market import _q, quotes, rows

TZ_NY = ZoneInfo("America/New_York")

LIVE_TS = datetime(2026, 7, 30, 11, 44, tzinfo=TZ_NY).timestamp()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """web_view caches in-process; each test starts cold."""
    monkeypatch.setattr(um, "_web_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(um, "_us_rows", lambda: rows(("NVDA", 9.2), ("TSLA", -2.4)))


def _quotes(monkeypatch, q=None):
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: q or quotes())


def _at(monkeypatch, when):
    """Freeze us_market's clock. Needed for the LIVE cases: session_complete
    asks whether 16:00 ET on the quote's date has passed in REAL time, so a
    fixed intraday timestamp reads as finished once that day is over."""
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    monkeypatch.setattr(um, "datetime", _DT)


MID_SESSION = datetime(2026, 7, 30, 11, 44, tzinfo=TZ_NY)   # New York is trading


# ── web_view ─────────────────────────────────────────────────────────────────
def test_web_view_marks_a_finished_session_as_not_live(monkeypatch):
    _quotes(monkeypatch)
    v = um.web_view()
    assert v["live"] is False
    assert v["session"] == "2026-07-29"
    assert v["error"] is None
    assert v["adr"] and v["breadth"]["total"] == 2 and v["read"]


def test_web_view_marks_an_open_session_LIVE(monkeypatch):
    """The 2026-07-30 mistake, as a page property: intraday prices look exactly
    like a close, so the page must be told which it is holding."""
    _at(monkeypatch, MID_SESSION)
    _quotes(monkeypatch, quotes(**{"^GSPC": _q(7393.0, 7316.15, LIVE_TS)}))
    assert um.web_view()["live"] is True


def test_web_view_caches_and_does_not_refetch(monkeypatch):
    calls = []
    monkeypatch.setattr(um, "fetch_quotes",
                        lambda symbols=None: calls.append(1) or quotes())
    um.web_view()
    um.web_view()
    assert len(calls) == 1


def test_web_view_serves_the_last_snapshot_when_yahoo_dies(monkeypatch):
    _quotes(monkeypatch)
    good = um.web_view()
    assert good["indices"]
    monkeypatch.setattr(um, "_web_cache", {"ts": 0.0, "data": good})   # expire it

    def _boom(symbols=None):
        raise RuntimeError("yahoo 503")
    monkeypatch.setattr(um, "fetch_quotes", _boom)
    v = um.web_view()
    assert v["indices"] == good["indices"]         # still usable
    assert "yahoo 503" in v["error"]               # …and honest about it


def test_web_view_returns_a_renderable_shell_with_no_data_at_all(monkeypatch):
    def _boom(symbols=None):
        raise RuntimeError("offline")
    monkeypatch.setattr(um, "fetch_quotes", _boom)
    v = um.web_view()
    assert v["indices"] == [] and v["breadth"]["total"] == 0
    assert v["market"] and v["error"]              # the template needs `market`


def test_market_status_open_and_closed():
    assert um.market_status(datetime(2026, 7, 30, 11, 0, tzinfo=TZ_NY))["open"] is True
    assert um.market_status(datetime(2026, 7, 30, 17, 0, tzinfo=TZ_NY))["open"] is False
    sat = um.market_status(datetime(2026, 8, 1, 11, 0, tzinfo=TZ_NY))
    assert sat["open"] is False
    nxt = datetime.fromtimestamp(sat["next_open_ts"], TZ_NY)
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (9, 30)   # Monday


# ── the route ────────────────────────────────────────────────────────────────
def _client():
    import app as A
    A.app.config["TESTING"] = True
    return A.app.test_client()


def test_us_page_renders_without_login(monkeypatch):
    _quotes(monkeypatch)
    r = _client().get("/us")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "美股" in body and "台積電 ADR" in body
    assert "已收盤" in body


def test_us_page_says_盤中_while_new_york_trades(monkeypatch):
    _at(monkeypatch, MID_SESSION)
    _quotes(monkeypatch, quotes(**{"^GSPC": _q(7393.0, 7316.15, LIVE_TS)}))
    body = _client().get("/us").get_data(as_text=True)
    assert "盤中" in body and "即時報價" in body
    assert "已收盤" not in body


def test_us_page_survives_a_dead_upstream(monkeypatch):
    def _boom(symbols=None):
        raise RuntimeError("offline")
    monkeypatch.setattr(um, "fetch_quotes", _boom)
    r = _client().get("/us")
    assert r.status_code == 200
    assert "暫時取不到" in r.get_data(as_text=True)


def test_public_pages_carry_no_account_data(monkeypatch):
    """Both pages are reachable with no session at all."""
    _quotes(monkeypatch)
    c = _client()
    for path in ("/us", "/tw"):
        body = c.get(path).get_data(as_text=True)
        assert c.get(path).status_code == 200
        for leak in ("USDT", "Bybit", "Binance", "餘額", "未平倉", "API"):
            assert leak not in body, f"{path} leaked {leak}"


def test_api_us_is_json(monkeypatch):
    _quotes(monkeypatch)
    r = _client().get("/api/us")
    assert r.status_code == 200 and r.is_json
    assert r.get_json()["session"] == "2026-07-29"


def test_both_public_pages_link_to_each_other(monkeypatch):
    _quotes(monkeypatch)
    c = _client()
    assert 'href="/us"' in c.get("/tw").get_data(as_text=True)
    assert 'href="/tw"' in c.get("/us").get_data(as_text=True)
