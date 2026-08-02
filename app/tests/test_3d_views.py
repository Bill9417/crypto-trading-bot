"""🌋🛰️ Liquidation terrain + position-risk endpoints behind /universe.

The load-bearing behaviour in both is what they REFUSE to plot:

  · terrain — only Binance liquidations carry a real traded price. Bybit and
    OKX report the BANKRUPTCY price, which sits past where the tape printed.
    Plotting those would raise mountains at prices that never traded, so they
    are counted and dropped, and an empty terrain must say "no Binance prints"
    rather than "no liquidations".
  · risk — it is the owner's real money, so it is admin-only.
"""
import time

import flask_login.utils
import pytest

import app as APP


class _User:
    is_active = True
    is_anonymous = False
    is_authenticated = True

    def __init__(self, admin=True):
        self.is_admin = admin

    def get_id(self):
        return "1"


def _as(monkeypatch, admin=True):
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: _User(admin))


# ── liquidation terrain ──────────────────────────────────────────────────────
def _events(now_ms):
    """Two Binance prints (with px) + two bankruptcy-priced ones (no px)."""
    return [
        {"sym": "BTC", "ts": now_ms - 1000, "px": 61000.0, "usd": 5000.0, "side": "long",  "ex": "Binance"},
        {"sym": "BTC", "ts": now_ms - 2000, "px": 63000.0, "usd": 8000.0, "side": "short", "ex": "Binance"},
        {"sym": "BTC", "ts": now_ms - 3000, "px": None,    "usd": 9999.0, "side": "long",  "ex": "Bybit"},
        {"sym": "BTC", "ts": now_ms - 4000, "px": 0,       "usd": 7777.0, "side": "short", "ex": "OKX"},
        {"sym": "ETH", "ts": now_ms - 1000, "px": 1800.0,  "usd": 100.0,  "side": "long",  "ex": "Binance"},
    ]


def _terrain(monkeypatch, qs="symbol=BTC&window=21600", events=None):
    import time as _t
    now_ms = _t.time() * 1000
    monkeypatch.setattr(APP.liquidations, "start", lambda: None)
    monkeypatch.setattr(APP.liquidations, "events_copy",
                        lambda: _events(now_ms) if events is None else events)
    monkeypatch.setattr(APP.liquidations, "snapshot", lambda w=60: {"collecting_since": 1785600000.0})
    with APP.app.test_request_context("/api/liq_terrain?" + qs):
        return APP.api_liq_terrain().get_json()


def test_bankruptcy_priced_events_are_excluded_and_counted(monkeypatch):
    _as(monkeypatch)
    d = _terrain(monkeypatch)
    assert d["n_priced"] == 2       # the two Binance prints
    assert d["n_unpriced"] == 2     # Bybit None + OKX 0 — dropped, not plotted
    assert d["long_usd"] == 5000.0 and d["short_usd"] == 8000.0
    assert d["price_domain"] == [61000.0, 63000.0]


def test_other_symbols_do_not_leak_in(monkeypatch):
    _as(monkeypatch)
    d = _terrain(monkeypatch)
    total = sum(c[0] + c[1] for row in d["grid"] for c in row)
    assert total == pytest.approx(13000.0)   # ETH's 100 is not included


def test_empty_terrain_is_ok_not_an_error(monkeypatch):
    """An empty feed is the normal state for a freshly started collector."""
    _as(monkeypatch)
    d = _terrain(monkeypatch, events=[])
    assert d["ok"] is True
    assert d["n_priced"] == 0 and d["grid"] == [] and d["max_usd"] == 0.0
    assert d["price_domain"] is None


def test_single_price_level_does_not_divide_by_zero(monkeypatch):
    """All prints at one price would make (phi - plo) == 0."""
    _as(monkeypatch)
    import time as _t
    now = _t.time() * 1000
    same = [{"sym": "BTC", "ts": now - 500, "px": 61000.0, "usd": 10.0, "side": "long", "ex": "Binance"},
            {"sym": "BTC", "ts": now - 400, "px": 61000.0, "usd": 20.0, "side": "short", "ex": "Binance"}]
    d = _terrain(monkeypatch, events=same)
    assert d["ok"] is True and d["n_priced"] == 2
    assert d["price_domain"][1] > d["price_domain"][0]


def test_window_is_clamped(monkeypatch):
    _as(monkeypatch)
    assert _terrain(monkeypatch, "symbol=BTC&window=999999")["window_sec"] == 86400
    assert _terrain(monkeypatch, "symbol=BTC&window=1")["window_sec"] == 600
    assert _terrain(monkeypatch, "symbol=BTC&window=junk")["window_sec"] == 21600


def test_grid_dimensions_match_the_declared_shape(monkeypatch):
    _as(monkeypatch)
    d = _terrain(monkeypatch)
    assert len(d["grid"]) == d["nz"]
    assert all(len(row) == d["nx"] for row in d["grid"])


# ── position risk ────────────────────────────────────────────────────────────
def _risk(monkeypatch, positions):
    monkeypatch.setattr(APP.strategy3_exec, "account_snapshot",
                        lambda: {"positions": positions})
    with APP.app.test_request_context("/api/risk_bodies"):
        return APP.api_risk_bodies().get_json()


SHORT_POS = {"symbol": "XAUT/USDT:USDT", "side": "SHORT", "entry": 4041.8, "mark": 4046.7,
             "liq": 5555.9, "sl": 4102.4, "notional": 1129.0, "leverage": 50.0, "engine": "s3"}


def test_short_position_distances_and_pnl_sign(monkeypatch):
    """A SHORT is losing when mark rises above entry — the sign flip is the
    easy thing to get backwards."""
    _as(monkeypatch)
    b = _risk(monkeypatch, [SHORT_POS])["bodies"][0]
    assert b["side"] == "SHORT" and b["engine"] == "s3"
    assert b["pnl_pct"] < 0                       # mark above entry on a short
    assert b["to_sl"] == pytest.approx(abs((4102.4 - 4046.7) / 4046.7 * 100))
    assert b["to_liq"] == pytest.approx(abs((5555.9 - 4046.7) / 4046.7 * 100))


def test_long_position_pnl_sign(monkeypatch):
    _as(monkeypatch)
    lng = dict(SHORT_POS, side="LONG", entry=100.0, mark=110.0, liq=50.0, sl=95.0)
    b = _risk(monkeypatch, [lng])["bodies"][0]
    assert b["pnl_pct"] == pytest.approx(10.0)

def test_missing_levels_are_none_not_zero(monkeypatch):
    """No stop set must read as 'none', not as 'stop is 0% away' — which would
    render the position as already dead."""
    _as(monkeypatch)
    b = _risk(monkeypatch, [dict(SHORT_POS, sl=None, liq=None)])["bodies"][0]
    assert b["to_sl"] is None and b["to_liq"] is None


def test_position_without_a_mark_is_skipped(monkeypatch):
    _as(monkeypatch)
    d = _risk(monkeypatch, [{"symbol": "X/USDT:USDT", "side": "LONG"}])
    assert d["bodies"] == []


def test_risk_is_admin_only(monkeypatch):
    _as(monkeypatch, admin=False)
    with APP.app.test_request_context("/api/risk_bodies"):
        resp = APP.api_risk_bodies()
        body, code = resp if isinstance(resp, tuple) else (resp, 200)
        assert code == 403


def test_exchange_failure_does_not_500(monkeypatch):
    _as(monkeypatch)

    def _boom():
        raise RuntimeError("bybit unreachable")

    monkeypatch.setattr(APP.strategy3_exec, "account_snapshot", _boom)
    with APP.app.test_request_context("/api/risk_bodies"):
        resp = APP.api_risk_bodies()
        body, code = resp if isinstance(resp, tuple) else (resp, 200)
        assert code == 200
        assert body.get_json()["ok"] is False


# ── 🐳 whale positioning ─────────────────────────────────────────────────────
# szi is in COIN units, which makes whales incomparable: 29 BTC and 50,000 ETH
# look wildly different until both are priced. The endpoint's job is to make
# them comparable without inventing numbers.
def _whales(monkeypatch, state, price=2000.0, addresses=None):
    import whale_tracker as W
    _as(monkeypatch)                     # the endpoint is login_required
    monkeypatch.setattr(W, "_load_state", lambda: state)
    monkeypatch.setattr(W, "load_addresses",
                        lambda: addresses if addresses is not None
                        else [{"address": "0xaaa", "label": "鯨 A"},
                              {"address": "0xbbb", "label": "鯨 B"}])
    monkeypatch.setattr(APP, "fetch_live_tickers",
                        lambda syms: ({syms[0]: {"last": price}} if price else {}))
    with APP.app.test_request_context("/api/whale_3d?coin=ETH"):
        return APP.api_whale_3d().get_json()


def test_sizes_are_priced_into_usd(monkeypatch):
    d = _whales(monkeypatch, {"0xaaa": {"ETH": {"side": "long", "szi": 10.0}},
                              "0xbbb": {"ETH": {"side": "short", "szi": -5.0}}})
    assert d["ok"] and len(d["whales"]) == 2
    by = {w["label"]: w for w in d["whales"]}
    assert by["鯨 A"]["usd"] == pytest.approx(20000.0)   # 10 ETH @ 2000
    assert by["鯨 B"]["usd"] == pytest.approx(10000.0)   # abs(-5) @ 2000
    assert d["net_usd"] == pytest.approx(10000.0)        # long 20k - short 10k


def test_side_comes_from_the_sign_not_the_label(monkeypatch):
    """A stale/incorrect 'side' string must not flip a position's direction."""
    d = _whales(monkeypatch, {"0xaaa": {"ETH": {"side": "long", "szi": -7.0}}})
    assert d["whales"][0]["side"] == "short"


def test_unpriceable_size_is_none_not_zero(monkeypatch):
    """usd=0 would render as a flat bar — i.e. 'no position' — which is a lie."""
    d = _whales(monkeypatch, {"0xaaa": {"ETH": {"side": "long", "szi": 10.0}}}, price=None)
    assert d["whales"][0]["usd"] is None
    assert d["price"] is None


def test_flat_and_other_coins_are_excluded(monkeypatch):
    d = _whales(monkeypatch, {"0xaaa": {"ETH": {"side": "long", "szi": 0.0}},
                              "0xbbb": {"BTC": {"side": "short", "szi": -1.0}}})
    assert d["whales"] == [] and d["n_long"] == 0 and d["n_short"] == 0
    assert "BTC" in d["coins"]          # still reported as available elsewhere


def test_bad_coin_is_rejected(monkeypatch):
    _as(monkeypatch)
    with APP.app.test_request_context("/api/whale_3d?coin=../etc"):
        resp = APP.api_whale_3d()
        body, code = resp if isinstance(resp, tuple) else (resp, 200)
        assert code == 400


def test_a_broken_tracker_does_not_500(monkeypatch):
    import whale_tracker as W
    _as(monkeypatch)

    def _boom():
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(W, "_load_state", _boom)
    monkeypatch.setattr(W, "load_addresses", lambda: [])
    with APP.app.test_request_context("/api/whale_3d?coin=ETH"):
        resp = APP.api_whale_3d()
        body, code = resp if isinstance(resp, tuple) else (resp, 200)
        assert code == 200 and body.get_json()["ok"] is False


# ── 💥 2D liquidation map ────────────────────────────────────────────────────
def _levels(monkeypatch, events, coin="BTC", price=63000.0):
    _as(monkeypatch)
    monkeypatch.setattr(APP.liquidations, "start", lambda: None)
    monkeypatch.setattr(APP.liquidations, "events_copy", lambda: events)
    monkeypatch.setattr(APP.liquidations, "snapshot",
                        lambda w=60: {"collecting_since": 1785600000.0})
    monkeypatch.setattr(APP, "fetch_live_tickers",
                        lambda s: ({s[0]: {"last": price}} if price else {}))
    with APP.app.test_request_context(f"/api/liq_levels?coin={coin}"):
        return APP.api_liq_levels().get_json()


def test_levels_bin_by_price_and_split_by_side(monkeypatch):
    now = time.time() * 1000
    evs = [{"sym": "BTC", "ts": now - 1000, "px": 61000.0, "usd": 500.0, "side": "long", "ex": "Binance"},
           {"sym": "BTC", "ts": now - 2000, "px": 65000.0, "usd": 300.0, "side": "short", "ex": "Binance"}]
    d = _levels(monkeypatch, evs)
    assert d["ok"] and d["n_priced"] == 2
    assert d["long_usd"] == 500.0 and d["short_usd"] == 300.0
    assert sum(lv["long"] + lv["short"] for lv in d["levels"]) == pytest.approx(800.0)
    # the two prints land in DIFFERENT price bins
    hit = [i for i, lv in enumerate(d["levels"]) if lv["long"] or lv["short"]]
    assert len(hit) == 2


def test_bankruptcy_priced_events_never_reach_the_map(monkeypatch):
    """Same rule as the 3D terrain: Bybit/OKX prices never traded."""
    now = time.time() * 1000
    evs = [{"sym": "BTC", "ts": now - 1000, "px": None, "usd": 9999.0, "side": "long", "ex": "Bybit"},
           {"sym": "BTC", "ts": now - 1000, "px": 0, "usd": 8888.0, "side": "short", "ex": "OKX"}]
    d = _levels(monkeypatch, evs)
    assert d["n_priced"] == 0 and d["n_unpriced"] == 2
    assert d["levels"] == [] and d["long_usd"] == 0.0


def test_spot_price_is_inside_the_plotted_range(monkeypatch):
    """Spot must be on the map even when every liquidation sat far away, or
    'where is price relative to the clusters' is unanswerable."""
    now = time.time() * 1000
    evs = [{"sym": "BTC", "ts": now - 1000, "px": 61000.0, "usd": 100.0, "side": "long", "ex": "Binance"}]
    d = _levels(monkeypatch, evs, price=70000.0)
    assert d["lo"] <= 70000.0 <= d["hi"]


def test_empty_is_ok_and_still_reports_price(monkeypatch):
    d = _levels(monkeypatch, [])
    assert d["ok"] is True and d["levels"] == [] and d["price"] == 63000.0


def test_bad_coin_rejected(monkeypatch):
    _as(monkeypatch)
    with APP.app.test_request_context("/api/liq_levels?coin=../x"):
        resp = APP.api_liq_levels()
        body, code = resp if isinstance(resp, tuple) else (resp, 200)
        assert code == 400
