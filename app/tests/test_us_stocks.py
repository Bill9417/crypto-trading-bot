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
    """It goes to a group topic anyone with the invite link can read.

    The ban is on the OWNER'S figures, not on vocabulary: the leverage warning
    legitimately says "虧掉 8N% 保證金" about arithmetic, and "Bybit" appears as
    the name of a venue. What must never appear is a real balance, position or
    P&L — so this looks for the SHAPE of account data, not for words."""
    import re
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [("AAPL", "Apple", s)])
    for banned in ("餘額", "淨值", "未實現", "已實現", "可用保證金", "帳戶"):
        assert banned not in body, banned
    # any concrete USDT figure would be account data — the equity plan is in
    # share prices and percentages, never in the account's own currency
    assert not re.search(r"[\d,.]+\s*USDT", body)


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


# ── Bybit stock perps: a ticker is not an asset identifier ───────────────────
# Scanning the 51 US100 names Bybit lists found four outright collisions with
# crypto tokens of the same name — T $0.00 vs AT&T $23.25, C $0.05 vs Citigroup
# $132.45, CVX $1.35 vs Chevron $196.83, DASH $31.19 vs DoorDash $196.16.
# Same failure as the S1 mirror's ON trade on 2026-08-04.
def _perp(monkeypatch, base, perp_price):
    import strategy3_exec as X
    monkeypatch.setattr(U, "_perp_markets", {})

    class _Ex:
        def load_markets(self):
            return {f"{base}/USDT:USDT": {"swap": True, "linear": True, "base": base}}

        def fetch_ticker(self, sym):
            return {"last": perp_price}

    monkeypatch.setattr(X, "client", lambda: _Ex())


def test_a_matching_perp_is_offered(monkeypatch):
    _perp(monkeypatch, "AAPL", 303.30)
    assert U._perp_symbol("AAPL", 308.91) == "AAPL/USDT:USDT"


def test_a_colliding_token_is_refused(monkeypatch):
    """Citigroup's signal must never route to a $0.05 coin called C."""
    _perp(monkeypatch, "C", 0.05)
    assert U._perp_symbol("C", 132.45) is None


def test_overnight_perp_drift_is_tolerated(monkeypatch):
    """Stock perps keep trading while the equity market is shut. Real drift
    measured 2.2% median / 12.1% at the 90th percentile; collisions sit at
    84-100%. Nothing lands between, so 20% is a plateau."""
    _perp(monkeypatch, "PLTR", 144.40)
    assert U._perp_symbol("PLTR", 123.06) is not None      # 17.3% — real drift


def test_an_unlisted_ticker_has_no_perp(monkeypatch):
    _perp(monkeypatch, "AAPL", 303.30)
    assert U._perp_symbol("ZZZZ", 100.0) is None


def test_a_broken_exchange_does_not_break_the_scan(monkeypatch):
    import strategy3_exec as X
    monkeypatch.setattr(U, "_perp_markets", {})
    monkeypatch.setattr(X, "client",
                        lambda: (_ for _ in ()).throw(RuntimeError("bybit down")))
    assert U._perp_symbol("AAPL", 308.91) is None          # equity setup stands


# ── futures targets ──────────────────────────────────────────────────────────
def test_setup_carries_both_exit_plans():
    s = U.setup(_bars(_oversold_in_uptrend()))
    assert s["tp3"] == pytest.approx(s["ref"] * (1 + U.FUT_TP_PCT))
    assert s["sl3"] == pytest.approx(s["ref"] * (1 - U.FUT_SL_PCT))
    assert s["sl3"] < s["ref"] < s["tp3"]


def test_the_futures_stop_is_wider_than_its_target():
    """8% stop against a 3% target is where the EDGE peaks, not the win rate.
    A 15% stop wins 82.5% of the time and earns less. If this ever inverts to
    a tight stop the module is chasing win rate again."""
    assert U.FUT_SL_PCT > U.FUT_TP_PCT


def test_digest_shows_both_plans_and_what_the_futures_one_costs():
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [("AAPL", "Apple", s)])
    assert "現股 停損" in body and "期貨 停損" in body
    assert "77.6%" in body and "54.3%" in body             # both win rates
    assert "+1.34%" in body and "+0.45%" in body           # and both edges
    assert "槓桿會等比放大" in body                          # leverage warns


def test_digest_flags_which_names_are_tradeable_on_bybit():
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"},
                          [("AAPL", "Apple", dict(s, perp="AAPL/USDT:USDT")),
                           ("ZZZZ", "NoPerp", dict(s, perp=None))])
    assert "⚡Bybit 永續" in body and "（1 檔）" in body


def test_no_perp_flag_when_none_are_tradeable():
    s = U.setup(_bars(_oversold_in_uptrend()))
    body = U.build_digest(NOW, {"ok": True, "why": "x"}, [("ZZZZ", "N", s)])
    assert "⚡Bybit 永續" not in body
