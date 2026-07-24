"""🇹🇼 Public /tw page — dad's plain-Chinese 'good entry' view of the TW scan.

Two properties matter:
  1. It renders WITHOUT auth (dad opens it straight from the LINE link).
  2. It is account-free — only the setups already broadcast to the LINE group,
     never balances / positions / per-account P&L.
"""
import app as APP
import tw_stocks


def _get(path="/tw"):
    return APP.app.test_client().get(path)


def _stub(monkeypatch, view):
    monkeypatch.setattr(tw_stocks, "web_view", lambda *a, **k: view)


_VIEW = {
    "as_of": "2026-07-24", "generated_at": 0, "regime_ok": True,
    "regime": {"ok": True}, "open_count": 1, "new_count": 1, "buy_zone_count": 1,
    "sl_atr": 3.0, "tp_atr": 5.0, "max_hold": 40,
    "setups": [{
        "code": "2330", "name": "台積電", "date": "2026-07-24", "days": 0,
        "ref": 1000.0, "sl": 900.0, "tp": 1200.0,
        "ref_s": "1,000", "sl_s": "900", "tp_s": "1,200",
        "risk_pct": 10.0, "gain_pct": 20.0, "rr": 2.0, "status": "new",
        "hit": None, "price": 1010.0, "price_s": "1,010",
        "change_pct": 1.5, "dist_pct": 1.0, "buy_zone": True,
    }],
}


def test_tw_renders_without_login(monkeypatch):
    _stub(monkeypatch, _VIEW)
    r = _get()
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "台股．好進場點" in html
    assert "台積電" in html and "接近進場價" in html
    assert "1,200" in html                          # _px-formatted target
    assert "今天可以進場" in html                    # bullish regime banner


def test_tw_shows_observe_banner_when_bearish(monkeypatch):
    view = dict(_VIEW, regime_ok=False, regime={"ok": False},
                new_count=0,
                setups=[dict(_VIEW["setups"][0], status="tracking")])
    _stub(monkeypatch, view)
    html = _get().get_data(as_text=True)
    assert "今天先觀望" in html
    assert "沒有新增訊號" in html                    # the observe-day clarifier
    assert "訊號日參考價" in html                    # tracking entry sublabel


def test_tw_never_leaks_account_words(monkeypatch):
    _stub(monkeypatch, _VIEW)
    html = _get().get_data(as_text=True)
    for banned in ("餘額", "可用保證金", "未實現", "帳戶淨值", "USDT"):
        assert banned not in html


def test_tw_route_failsoft_on_view_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("state gone")

    monkeypatch.setattr(tw_stocks, "web_view", boom)
    r = _get()
    assert r.status_code == 200                     # degraded, not a 500
    assert "台股．好進場點" in r.get_data(as_text=True)


def test_api_tw_returns_json(monkeypatch):
    _stub(monkeypatch, _VIEW)
    r = _get("/api/tw")
    assert r.status_code == 200
    assert r.json["setups"][0]["code"] == "2330"
