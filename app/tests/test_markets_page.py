"""📈 /markets — 台股 + 美股 on one public page.

The two markets are not independent: the US close sets the tone for the next
TW open, which is why the combined view exists. Each half must fail on its
own, so a broken TW scan still leaves the US half readable — dad opens this
from a LINE link and a 500 would just look broken.
"""

import app as APP


TW_OK = {"as_of": "2026-07-31", "regime": {"close": 24000, "sma100": 23000, "mom20": -1},
         "regime_ok": False, "setups": [{"code": "2376", "name": "技嘉", "ref": 344.0,
                                         "sl": 308.2, "strategy_tag": "回踩", "buy_zone": True}],
         "open_count": 8, "buy_zone_count": 3}
US_OK = {"session_label": "07-31（週五）", "live": False,
         "indices": [{"label": "道瓊", "price": 52485.03, "chg_pct": 0.53}],
         "vix": {"price": 16.0, "chg_pct": -6.4}, "tnx": {"price": 4.75, "chg_pp": 0.08},
         "adr": {"adr": 404.25, "equiv": 2611.1, "tw_close": 2425.0, "premium_pct": 7.7},
         "breadth": {"up": 53, "down": 47, "total": 100}, "read": "美股收紅。"}


def _render(monkeypatch, tw=None, us=None):
    import tw_stocks
    import us_market

    def _tw():
        if isinstance(tw, Exception):
            raise tw
        return tw if tw is not None else TW_OK

    def _us():
        if isinstance(us, Exception):
            raise us
        return us if us is not None else US_OK

    monkeypatch.setattr(tw_stocks, "web_view", _tw)
    monkeypatch.setattr(us_market, "web_view", _us)
    with APP.app.test_request_context("/markets"):
        return APP.markets_page()


def test_shows_both_markets(monkeypatch):
    html = _render(monkeypatch)
    assert "台股" in html and "美股" in html
    assert "2376" in html and "技嘉" in html      # a TW setup
    assert "道瓊" in html                          # a US index
    assert "16.0" in html                          # VIX


def test_is_public_no_login_needed():
    """dad opens this straight from a LINE link — a redirect to /login is a
    dead end for him."""
    resp = APP.app.test_client().get("/markets")
    assert resp.status_code == 200


def test_a_broken_tw_scan_still_renders_the_us_half(monkeypatch):
    html = _render(monkeypatch, tw=RuntimeError("TWSE down"))
    assert "道瓊" in html and "美股" in html


def test_a_broken_us_feed_still_renders_the_tw_half(monkeypatch):
    html = _render(monkeypatch, us=RuntimeError("yahoo down"))
    assert "2376" in html and "台股" in html


def test_premium_precision_matches_the_source(monkeypatch):
    """us_market rounds premium_pct to 1dp; rendering 2dp would invent
    precision the number does not have."""
    html = _render(monkeypatch)
    assert "+7.7%" in html
    assert "+7.70%" not in html


def test_empty_tw_setups_says_so_rather_than_showing_nothing(monkeypatch):
    html = _render(monkeypatch, tw=dict(TW_OK, setups=[], buy_zone_count=0))
    assert "沒有符合條件" in html


# ── 公開 movers 端點 ─────────────────────────────────────────────────────────
def test_movers_endpoint_is_public_and_trimmed(monkeypatch):
    """It feeds a no-login page, so it must hand out the MINIMUM that answers
    the question — not the full /api/stocks payload, which also carries the
    Binance tokenised-stock perp columns."""
    import stocks_data
    monkeypatch.setattr(stocks_data, "build_stocks", lambda: {
        "generated_at": 1,
        "tw": {"rows": [{"code": "2330", "name": "台積電", "price": 2425.0,
                         "change_pct": 9.98, "volume_lots": 5, "high": 1, "low": 1},
                        {"code": "2317", "name": "鴻海", "price": 200.0, "change_pct": -3.1}],
               "market": {"state": "closed", "next_open_ts": 123}},
        "us": {"rows": [{"ticker": "NVDA", "name": "NVIDIA", "price": 200.75,
                         "change_pct": 2.93, "perp_price": 200.5, "perp_diff_pct": -0.1}],
               "market": {"state": "open"}},
    })
    APP._stocks_cache.update(ts=0.0, data=None)
    resp = APP.app.test_client().get("/api/market_movers")
    assert resp.status_code == 200                       # no login
    d = resp.get_json()
    assert d["tw"]["up"][0]["id"] == "2330"
    assert d["tw"]["advancers"] == 1 and d["tw"]["decliners"] == 1
    assert d["us"]["market"]["state"] == "open"
    # the perp columns must NOT leak into a public endpoint
    blob = resp.get_data(as_text=True)
    assert "perp_price" not in blob and "perp_diff_pct" not in blob
    assert "volume_lots" not in blob


def test_movers_skips_rows_without_a_move(monkeypatch):
    """A None change_pct would sort-crash and render as a fake 0%."""
    import stocks_data
    monkeypatch.setattr(stocks_data, "build_stocks", lambda: {
        "generated_at": 1,
        "tw": {"rows": [{"code": "A", "change_pct": None}, {"code": "B", "change_pct": 1.0}],
               "market": {}},
        "us": {"rows": [], "market": {}},
    })
    APP._stocks_cache.update(ts=0.0, data=None)
    d = APP.app.test_client().get("/api/market_movers").get_json()
    assert [r["id"] for r in d["tw"]["up"]] == ["B"]
    assert d["tw"]["total"] == 1


def test_stocks_are_cached(monkeypatch):
    """Three surfaces want this data; it must not fan out three sets of
    TWSE + Yahoo calls per visitor."""
    import stocks_data
    calls = []
    monkeypatch.setattr(stocks_data, "build_stocks",
                        lambda: calls.append(1) or {"tw": {"rows": [], "market": {}},
                                                    "us": {"rows": [], "market": {}}})
    APP._stocks_cache.update(ts=0.0, data=None)
    for _ in range(3):
        APP._stocks_cached()
    assert len(calls) == 1


def test_stale_data_beats_no_data(monkeypatch):
    """If the upstream dies after a good fetch, keep serving the last good
    snapshot rather than blanking dad's page."""
    import stocks_data
    good = {"tw": {"rows": [], "market": {}}, "us": {"rows": [], "market": {}}}
    monkeypatch.setattr(stocks_data, "build_stocks", lambda: good)
    APP._stocks_cache.update(ts=0.0, data=None)
    assert APP._stocks_cached() is good

    def _boom():
        raise RuntimeError("TWSE down")

    monkeypatch.setattr(stocks_data, "build_stocks", _boom)
    APP._stocks_cache["ts"] = 0.0            # force past the TTL
    assert APP._stocks_cached() is good
