"""🇺🇸 US100 oversold-in-uptrend scan.

Network fully stubbed — no test may reach Yahoo. Synthetic bars are built to
drive each gate individually, so a broken gate fails one test, not all of them.
"""
from datetime import datetime

import pytest

import us_stocks as U

DAY = 86400


def _bars(closes, atr=1.0, start=1_700_000_000):
    """Daily bars from a close series, with a controllable range per bar."""
    out = []
    for i, c in enumerate(closes):
        out.append((start + i * DAY, c, c + atr / 2, c - atr / 2, c, 1_000_000))
    return out


def _uptrend(n=260, base=100.0, step=0.5):
    return [base + step * i for i in range(n)]


# ── RSI ──────────────────────────────────────────────────────────────────────
def test_rsi_needs_15_bars():
    assert U._rsi14([1.0] * 14) is None


def test_rsi_is_100_when_nothing_ever_falls():
    """No down move is a real reading, not a divide-by-zero to swallow."""
    assert U._rsi14([100 + i for i in range(40)]) == 100.0


def test_rsi_is_low_after_a_run_of_losses():
    closes = [100 + i for i in range(40)] + [140 - 2 * i for i in range(20)]
    assert U._rsi14(closes) < 30


def test_rsi_sits_midrange_on_noise():
    closes = [100 + (1 if i % 2 else -1) for i in range(60)]
    assert 30 < U._rsi14(closes) < 70


# ── regime ───────────────────────────────────────────────────────────────────
def test_regime_needs_history():
    assert U.regime(_bars(_uptrend(50)))["ok"] is False


def test_regime_ok_in_a_rising_market():
    r = U.regime(_bars(_uptrend(200)))
    assert r["ok"] and "100 日均線" in r["why"]


def test_regime_blocks_below_the_100_sma():
    closes = _uptrend(200) + [200 - 3 * i for i in range(60)]
    r = U.regime(_bars(closes))
    assert r["ok"] is False and "跌破" in r["why"]


def test_regime_blocks_a_flat_but_stalled_tape():
    """Above the SMA yet lower than 20 sessions ago — the 'rising' half."""
    closes = _uptrend(200) + [300 - 0.4 * i for i in range(25)]
    r = U.regime(_bars(closes))
    assert r["ok"] is False and "走低" in r["why"]


# ── the setup ────────────────────────────────────────────────────────────────
def _oversold_in_uptrend():
    """Long uptrend, then a sharp drop that stays above the 200-SMA."""
    return _uptrend(260, base=100.0, step=0.6) + [255 - 4.0 * i for i in range(12)]


def test_setup_fires_when_oversold_inside_an_uptrend():
    s = U.setup(_bars(_oversold_in_uptrend()))
    assert s and s["kind"] == "oversold"
    assert s["rsi"] < U.RSI_MAX


def test_setup_needs_enough_history():
    assert U.setup(_bars(_uptrend(100))) is None


def test_setup_refuses_below_the_200_sma():
    """Only ever long inside an uptrend — an oversold downtrend is a falling
    knife, and the backtest's edge is measured with this gate in place."""
    closes = _uptrend(260) + [100 - 2 * i for i in range(60)]
    assert U.setup(_bars(closes)) is None


def test_setup_refuses_a_strong_stock_that_is_not_oversold():
    assert U.setup(_bars(_uptrend(300))) is None


def test_levels_are_atr_multiples_around_the_close():
    s = U.setup(_bars(_oversold_in_uptrend(), atr=2.0))
    assert s["sl"] == pytest.approx(s["ref"] - U.SL_ATR * s["atr"])
    assert s["tp"] == pytest.approx(s["ref"] + U.TP_ATR * s["atr"])
    assert s["sl"] < s["ref"] < s["tp"]


def test_reward_beats_risk():
    s = U.setup(_bars(_oversold_in_uptrend()))
    assert (s["tp"] - s["ref"]) > (s["ref"] - s["sl"])


# ── digest ───────────────────────────────────────────────────────────────────
NOW = datetime(2026, 8, 3, 9, 0)


def test_bearish_regime_says_stand_aside_and_lists_nothing():
    body = U.build_digest(NOW, {"ok": False, "why": "S&P 跌破 100 日均線"},
                          [("AAPL", "Apple", {})])
    assert "今日不進場" in body and "AAPL" not in body


def test_digest_lists_setups_with_levels():
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "站上"}, [("AAPL", "Apple", s)])
    assert "AAPL" in body and "停損" in body and "停利" in body


def test_digest_reports_the_edge_not_just_the_win_rate():
    """A win rate alone is what mis-sells a strategy — this project has 48
    crypto configs at 55-78% WR of which 46 lost money."""
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [("AAPL", "Apple", s)])
    assert "真實優勢" in body and "隨機日" in body
    assert "偏樂觀" in body                    # the backtest's own caveats


def test_digest_is_honest_when_nothing_triggers():
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [])
    assert "沒有標的觸發" in body


def test_digest_caps_the_list():
    s = U.setup(_bars(_oversold_in_uptrend()))
    many = [(f"S{i}", f"n{i}", s) for i in range(U.MAX_SHOW + 5)]
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, many)
    assert "另外 5 檔" in body


# ── scheduling ───────────────────────────────────────────────────────────────
def test_not_due_before_the_hour():
    assert U._due({}, datetime(2026, 8, 3, U.SEND_HOUR - 1)) is False


def test_not_due_twice_in_one_day():
    st = {"last_scan": "2026-08-03"}
    assert U._due(st, datetime(2026, 8, 3, U.SEND_HOUR + 2)) is False


def test_due_on_a_fresh_day():
    st = {"last_scan": "2026-08-02"}
    assert U._due(st, datetime(2026, 8, 3, U.SEND_HOUR)) is True


def test_tick_is_a_no_op_when_not_due(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "STATE_FILE", str(tmp_path / "s.json"))
    U._save_state({"last_scan": datetime.now(U.TZ).strftime("%Y-%m-%d")})
    monkeypatch.setattr(U, "scan", lambda **k: pytest.fail("scanned when not due"))
    assert U.tick() is False


def test_tick_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(U, "SEND_HOUR", 0)
    monkeypatch.setattr(U, "scan", lambda **k: (_ for _ in ()).throw(RuntimeError("yahoo down")))
    assert U.tick() is False


def test_a_failed_send_is_retried_not_marked_done(monkeypatch, tmp_path):
    import telegram_utils
    monkeypatch.setattr(U, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(U, "SEND_HOUR", 0)
    monkeypatch.setattr(U, "scan", lambda **k: ({"ok": True, "why": "x"}, []))
    monkeypatch.setattr(telegram_utils, "send_message", lambda *a, **k: False)
    assert U.tick() is False
    assert "last_scan" not in U._load_state()


def test_cooldown_suppresses_a_recent_name(monkeypatch, tmp_path):
    """A name that already fired is probably still inside its 40-day trade."""
    import time as _t
    import telegram_utils
    monkeypatch.setattr(U, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(U, "SEND_HOUR", 0)
    U._save_state({"recent": {"AAPL": _t.time()}})
    s = U.setup(_bars(_oversold_in_uptrend()))
    monkeypatch.setattr(U, "scan", lambda **k: ({"ok": True, "why": "x"},
                                                [("AAPL", "Apple", s)]))
    sent = {}
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.update(msg=msg, ch=k.get("channel")) or True)
    assert U.tick() is True
    assert "AAPL" not in sent["msg"]
    assert sent["ch"] == "twstocks"


def test_the_digest_carries_no_account_data(monkeypatch):
    """It goes to a group topic anyone with the invite link can read."""
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [("AAPL", "Apple", s)])
    for banned in ("餘額", "淨值", "持倉", "未實現", "保證金", "Bybit", "USDT"):
        assert banned not in body


# ── /us web view ─────────────────────────────────────────────────────────────
def _state(monkeypatch, tmp_path, setups, last_scan="2026-08-03", ok=True):
    monkeypatch.setattr(U, "STATE_FILE", str(tmp_path / "s.json"))
    U._save_state({"last_scan": last_scan, "last_regime": {"ok": ok, "why": "x"},
                   "active_setups": setups})


def _quotes(monkeypatch, mapping):
    import stocks_data
    monkeypatch.setattr(stocks_data, "us_quote_rows",
                        lambda: [{"ticker": k, "price": v, "change_pct": 0.0}
                                 for k, v in mapping.items()])


S1 = {"code": "AMD", "name": "AMD", "date": "2026-08-03",
      "ref": 100.0, "sl": 90.0, "tp": 120.0, "rsi": 27.0, "atr": 3.3}


def test_web_view_survives_a_dead_quote_feed(monkeypatch, tmp_path):
    """The page must render off the persisted scan alone — a quote hiccup
    cannot be allowed to 500 a public, no-login page."""
    import stocks_data
    _state(monkeypatch, tmp_path, [S1])
    monkeypatch.setattr(stocks_data, "us_quote_rows",
                        lambda: (_ for _ in ()).throw(RuntimeError("yahoo down")))
    v = U.web_view()
    assert len(v["setups"]) == 1 and v["setups"][0]["price"] is None


def test_buy_zone_marks_price_still_near_the_reference(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, [S1])
    _quotes(monkeypatch, {"AMD": 101.0})          # inside 0.25 × (100−90) = 2.5
    assert U.web_view()["setups"][0]["buy_zone"] is True


def test_price_run_past_the_band_is_not_a_buy_zone(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, [S1])
    _quotes(monkeypatch, {"AMD": 106.0})
    r = U.web_view()["setups"][0]
    assert r["buy_zone"] is False and r["dist_pct"] == 6.0


def test_reaching_the_target_closes_the_row(monkeypatch, tmp_path):
    """Without this a resolved setup sits on the page as a live idea for 40
    sessions. It reads the CURRENT quote, so it means 'price is now beyond the
    target', not 'the trade filled there'."""
    _state(monkeypatch, tmp_path, [S1])
    _quotes(monkeypatch, {"AMD": 121.0})
    r = U.web_view()["setups"][0]
    assert r["status"] == "tp" and not r["buy_zone"]


def test_breaking_the_stop_closes_the_row(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, [S1])
    _quotes(monkeypatch, {"AMD": 89.0})
    assert U.web_view()["setups"][0]["status"] == "sl"


def test_actionable_rows_sort_above_resolved_ones(monkeypatch, tmp_path):
    rows = [dict(S1, code="DONE", tp=105.0),
            dict(S1, code="LIVE"),
            dict(S1, code="OLD", date="2026-07-20")]
    _state(monkeypatch, tmp_path, rows)
    _quotes(monkeypatch, {"DONE": 106.0, "LIVE": 100.5, "OLD": 100.5})
    order = [r["code"] for r in U.web_view()["setups"]]
    assert order[0] == "LIVE"                  # in its band AND today
    assert order[-1] == "DONE"                 # resolved sinks


def test_web_view_reports_the_regime_and_the_edge(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, [], ok=False)
    v = U.web_view()
    assert v["regime"]["ok"] is False
    assert "真實優勢" in v["edge"] and "隨機日" in v["edge"]
    assert "RSI14" in v["rule"]


def test_business_days_skip_the_weekend():
    from datetime import datetime as dt
    # 2026-07-31 is a Friday; the following Monday is one business day on
    assert U._biz_days_since("2026-07-31", dt(2026, 8, 3)) == 1


def test_business_days_tolerates_junk():
    from datetime import datetime as dt
    assert U._biz_days_since("not-a-date", dt(2026, 8, 3)) == 0
    assert U._biz_days_since(None, dt(2026, 8, 3)) == 0
