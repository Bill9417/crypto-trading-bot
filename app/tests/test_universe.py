"""🌌 Market Universe — the 3D scan map at /universe.

Reshapes the scan already on disk into points; it must never trigger a scan or
a network call. The interesting behaviour is honesty about MISSING data: on a
typical bear sweep the engine arms ~31 of ~246 signals, so entry/SL/TP are
None for the rest. Those axes must come back None (and be flagged optional)
rather than defaulting to 0, which would plant a fake cluster at the origin.
"""
import flask_login.utils
import pytest

import app as APP


class _User:
    is_active = True
    is_anonymous = False
    is_authenticated = True
    is_admin = True

    def get_id(self):
        return "1"


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: _User())


def _scan(signals):
    return {"signals": signals, "last_update": "2026-08-02 11:15:03",
            "btc_regime": "bear", "scanned_symbols": len(signals)}


ARMED = {
    "symbol": "ENA/USDT:USDT", "direction": "SHORT", "rsi": 70.6, "score": 9,
    "effective_lights": 8, "conviction": "🚀 MAX CONVICTION",
    "entry": "0.0837", "sl": "0.0848", "tp": "0.0826", "current_price": 0.08334,
    "smc": {"dist_to_high_pct": 1.7, "dist_to_low_pct": 6.26, "zone_label": "Premium Zone"},
    "trade_status": "rejected", "tv_url": "https://tv/ENA",
}
UNARMED = {
    "symbol": "SOL/USDT:USDT", "direction": "LONG", "rsi": 41.0, "score": 4,
    "effective_lights": 3, "conviction": "👀 WATCHLIST",
    "entry": None, "sl": None, "tp": None, "current_price": 150.0,
    "smc": {"dist_to_high_pct": -3.1, "dist_to_low_pct": 9.0, "zone_label": "Discount Zone"},
    "trade_status": "rejected", "tv_url": "https://tv/SOL",
}


def _points(monkeypatch, signals):
    monkeypatch.setattr(APP, "load_data", lambda: _scan(signals))
    with APP.app.test_request_context("/api/universe"):
        return APP.api_universe().get_json()


def test_unarmed_signals_keep_null_axes_not_zero(as_user, monkeypatch):
    """The bug worth preventing: coercing a missing entry to 0 would stack
    215 of 246 bodies onto a fake wall at the origin."""
    data = _points(monkeypatch, [ARMED, UNARMED])
    armed, unarmed = data["points"][0], data["points"][1]
    assert armed["rr"] == pytest.approx(1.0)       # |tp-entry| / |entry-sl|
    assert armed["dent"] is not None
    assert unarmed["rr"] is None and unarmed["dent"] is None
    # ... while the full-coverage axes are populated for both
    for p in data["points"]:
        assert p["rsi"] is not None and p["score"] is not None
        assert p["dhigh"] is not None and p["dlow"] is not None


def test_entry_derived_axes_are_flagged_optional(as_user, monkeypatch):
    """The viewer greys out / annotates partial axes off this flag."""
    data = _points(monkeypatch, [ARMED, UNARMED])
    opt = {a["key"]: a["optional"] for a in data["axes"]}
    assert opt["rr"] is True and opt["dent"] is True
    assert opt["rsi"] is False and opt["score"] is False and opt["dhigh"] is False


def test_direction_and_conviction_are_ranked(as_user, monkeypatch):
    data = _points(monkeypatch, [ARMED, UNARMED])
    assert data["points"][0]["d"] == -1 and data["points"][0]["conv"] == 3
    assert data["points"][1]["d"] == 1 and data["points"][1]["conv"] == 1


def test_rows_without_base_axes_are_dropped(as_user, monkeypatch):
    """A row with no RSI/score can't be placed — skip it rather than emit a
    point the renderer would have to guess a position for."""
    data = _points(monkeypatch, [ARMED, {"symbol": "X/USDT:USDT", "rsi": None, "score": None}])
    assert len(data["points"]) == 1


def test_divide_by_zero_in_rr_is_survived(as_user, monkeypatch):
    """entry == sl would be a ZeroDivisionError; the setup just has no R:R."""
    flat = dict(ARMED, entry="100", sl="100", tp="110")
    data = _points(monkeypatch, [flat])
    assert data["points"][0]["rr"] is None


def test_never_triggers_a_scan_or_network_call(as_user, monkeypatch):
    """This endpoint reshapes what is already on disk. If it ever reaches for
    the exchange it would add 246 symbols of load on every page poll."""
    def _boom(*a, **k):
        raise AssertionError("universe must not hit the exchange")

    monkeypatch.setattr(APP.rest_client, "call", _boom)
    monkeypatch.setattr(APP, "fetch_live_tickers", _boom)
    data = _points(monkeypatch, [ARMED, UNARMED])
    assert data["ok"] is True and len(data["points"]) == 2


def test_page_is_login_gated():
    resp = APP.app.test_client().get("/universe")
    assert resp.status_code in (301, 302, 401)
    assert "/login" in resp.headers.get("Location", "")
