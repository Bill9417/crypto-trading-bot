"""Live strategy selection + trailing management.

SAFETY: every test that touches the executor forces dry-run (executor.LIVE_TRADING
False) and/or mocks the exchange, so NOTHING here can ever reach Binance — these
run safely even while the live bot is trading.
"""
import datetime
import math
import types

import pytest

import backtest as BT
import bot
import config as C
import executor as E


def _uptrend(n=270, base=100.0):
    """A long enough (≥WINDOW) series that qualifies an S4 long (clean rising-ADX
    breakout with a volume surge on the last bar)."""
    H = 3_600_000
    oh = []
    for i in range(n):
        if i < n - 40:
            mid, vol = base + 3.0 * math.sin(i / 3.0), 1000.0
        else:
            mid, vol = base + 3.0 + 1.6 * (i - (n - 41)), 1100.0
        oh.append([i * H, mid - 0.3, mid + 0.5, mid - 0.8, mid, vol])
    prev = oh[-2][4]
    bo = prev + 5.0
    oh[-1] = [oh[-1][0], prev, bo + 0.3, prev - 0.2, bo, 4000.0]
    return oh


# ── live strategy resolution ─────────────────────────────────────────────────
def test_filter_records_by_strategy():
    import app as A
    rec = lambda strat: types.SimpleNamespace(strategy=strat)
    recs = [rec("default"), rec("trend_trailing"), rec(None), rec("trend_breakout")]
    # legacy NULL rows count as 'default' (S1)
    assert [r.strategy for r in A._filter_records_by_strategy(recs, "default")] == ["default", None]
    assert len(A._filter_records_by_strategy(recs, "trend_trailing")) == 1
    assert A._filter_records_by_strategy(recs, "all") == recs       # no scoping
    assert A._filter_records_by_strategy(recs, None) == recs        # no scoping


def test_resolve_live_strategy(monkeypatch):
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "default")
    assert bot.resolve_live_strategy() == ("default", "bracket")
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "trend_breakout")
    assert bot.resolve_live_strategy() == ("trend_breakout", "bracket")
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "trend_trailing")
    assert bot.resolve_live_strategy() == ("trend_trailing", "trailing")


def test_resolve_live_strategy_unknown_falls_back(monkeypatch):
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "typo_not_a_strategy")
    assert bot.resolve_live_strategy() == ("default", "bracket")


# ── .env persistence helpers ─────────────────────────────────────────────────
def test_config_env_roundtrip(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LIVE_TRADING=true\nLEVERAGE=4\n")
    monkeypatch.setattr(C, "ENV_PATH", str(env))
    assert C.read_env_var("LIVE_STRATEGY", "default") == "default"   # absent → default
    C.set_env_var("LIVE_STRATEGY", "trend_trailing")
    assert C.read_env_var("LIVE_STRATEGY") == "trend_trailing"
    # replace-in-place, not append-a-duplicate; untouched keys preserved
    C.set_env_var("LIVE_STRATEGY", "trend_breakout")
    body = env.read_text()
    assert body.count("LIVE_STRATEGY=") == 1
    assert "LIVE_STRATEGY=trend_breakout" in body
    assert "LEVERAGE=4" in body


# ── executor: trailing-aware bracket + move_stop ─────────────────────────────
def test_resting_plan_trailing_full_stop_no_tp(monkeypatch):
    monkeypatch.setattr(E, "_round_amount", lambda s, a: round(a, 3))
    monkeypatch.setattr(E, "_round_price", lambda s, p: round(p, 4))
    p = E._resting_plan("AAA/USDT:USDT", "LONG", 1.0, 0.9, 1.1, 1.2, 5, True, manage="trailing")
    assert p["manage"] == "trailing"
    assert p["tp_rest"] == p["amount"]                # stop must cover the FULL size
    orders = []
    E._place_take_profits(None, p, orders)            # trailing → no TP, no exchange call
    assert orders == []
    b = E._resting_plan("AAA/USDT:USDT", "LONG", 1.0, 0.9, 1.1, 1.2, 5, True)
    assert b["manage"] == "bracket"
    assert b["tp_rest"] == b["amount"] - b["half"]


def test_move_stop_dry_run_updates_tracked_stop(monkeypatch):
    monkeypatch.setattr(E, "LIVE_TRADING", False)     # force dry-run — no network
    key = ("AAA/USDT:USDT", "LONG")
    monkeypatch.setitem(E._active_brackets, key,
                        {"close_side": "sell", "amount": 6.0, "tp_rest": 6.0,
                         "orders": [{"role": "sl", "id": "x"}]})
    assert E.move_stop("AAA/USDT:USDT", "LONG", 0.95) is True
    assert E._active_brackets[key]["sl"] == 0.95


def test_move_stop_missing_position_is_noop(monkeypatch):
    monkeypatch.setattr(E, "LIVE_TRADING", False)
    assert E.move_stop("ZZZ/USDT:USDT", "LONG", 1.0) is False


# ── live trailing manager (bot._manage_trailing_signal) ──────────────────────
def _wire_trailing_mocks(monkeypatch, candles):
    moves, closed = [], []
    monkeypatch.setattr(bot, "get_symbol_ohlcv", lambda s, tf, limit=260: (candles, "mock"))
    stub = types.SimpleNamespace(
        move_stop=lambda sym, d, p, reason="": moves.append(round(p, 4)) or True,
        on_trade_closed=lambda sym, d: closed.append(("otc", sym)),
        close_position_market=lambda sym: closed.append(("cpm", sym)),
        is_live=lambda: False)
    monkeypatch.setattr(bot, "executor", stub)
    monkeypatch.setattr(bot, "update_signal_state_in_ui", lambda *a, **k: None)
    monkeypatch.setattr(bot, "notify", lambda *a, **k: None)
    monkeypatch.setattr(bot, "record_daily_loss", lambda p: None)
    now = datetime.datetime(2026, 6, 21, 12, 0, 0)
    monkeypatch.setattr(bot, "get_now_taiwan", lambda: now)
    return moves, closed, now


def _sig(now, **over):
    d = dict(symbol="AAA/USDT:USDT", direction="LONG", entry_price=100.0, sl_price=98.0,
             trail_dist=3.0, cur_stop=98.0, peak=100.0, trough=100.0,
             last_bar_ts=21 * 3_600_000, timestamp=now, status="PENDING",
             exit_price=None, exit_timestamp=None, pnl_pct=None)
    d.update(over)
    return types.SimpleNamespace(**d)


def test_trailing_manager_winner_catchup(monkeypatch):
    H = 3_600_000
    candles = [[21*H, 100, 101, 99.9, 101, 0], [22*H, 101, 105, 101, 105, 0],
               [23*H, 105, 110, 104, 110, 0], [24*H, 110, 110, 106, 108, 0]]
    moves, closed, now = _wire_trailing_mocks(monkeypatch, candles)
    sig = _sig(now)
    bot._manage_trailing_signal(sig)
    assert sig.status == "TP"
    assert round(sig.exit_price, 4) == 107.0
    assert round(sig.pnl_pct, 3) == 7.0


def test_trailing_manager_ratchets_each_candle(monkeypatch):
    H = 3_600_000
    full = [[21*H, 100, 101, 99.9, 101, 0], [22*H, 101, 105, 101, 105, 0],
            [23*H, 105, 110, 104, 110, 0], [24*H, 110, 110, 106, 108, 0]]
    moves, closed, now = _wire_trailing_mocks(monkeypatch, full)
    sig = _sig(now)
    # feed one new closed candle per monitor pass (bot keeping up)
    for upto in (2, 3, 4):
        monkeypatch.setattr(bot, "get_symbol_ohlcv", lambda s, tf, limit=260, c=full[:upto]: (c, "mock"))
        bot._manage_trailing_signal(sig)
        if sig.status != "PENDING":
            break
    assert moves == [102.0, 107.0]                    # exchange stop ratcheted up
    assert sig.status == "TP"


def test_scan_alt_strategy_queues_qualifying_s4(monkeypatch):
    """End-to-end of the non-default live dispatch: a qualifying S4 setup queues,
    is tagged 'trailing' with a trail distance, and is placed with manage=trailing.
    Network + exchange are mocked, so nothing reaches Binance."""
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "trend_trailing")
    monkeypatch.setattr(bot, "queued_signals", {})
    # 4H trend aligned for a long (ema well below price)
    monkeypatch.setattr(bot, "check_4h_trend", lambda sym, px: (px - 50, None, None, None))
    placed = []
    monkeypatch.setattr(bot, "executor", types.SimpleNamespace(
        has_capacity=lambda: True, concurrent_commitments=lambda: 0,
        place_resting_order=lambda *a, **k: placed.append(k.get("manage")) or {"ok": True}))
    monkeypatch.setattr(bot, "notify", lambda *a, **k: None)

    row = bot.scan_symbol_alt_strategy("AAA/USDT:USDT", "1h", _uptrend(270), "bull", True, {})
    assert row["trade_status"] == "queued"
    assert row["direction"] == "LONG"
    qk = list(bot.queued_signals)
    assert qk, "setup should have been queued"
    payload = bot.queued_signals[qk[0]]
    assert payload["manage"] == "trailing"
    assert payload["trail_dist"] and payload["trail_dist"] > 0
    assert placed == ["trailing"]   # placed with the trailing management tag


def test_scan_alt_strategy_no_signal_does_not_queue(monkeypatch):
    monkeypatch.setattr(bot, "LIVE_STRATEGY", "trend_trailing")
    monkeypatch.setattr(bot, "queued_signals", {})
    monkeypatch.setattr(bot, "check_4h_trend", lambda sym, px: (px - 50, None, None, None))
    monkeypatch.setattr(bot, "executor", types.SimpleNamespace(
        has_capacity=lambda: True, concurrent_commitments=lambda: 0,
        place_resting_order=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not place"))))
    monkeypatch.setattr(bot, "notify", lambda *a, **k: None)
    flat = [[i * 3_600_000, 100, 100.5, 99.5, 100, 1000] for i in range(270)]
    row = bot.scan_symbol_alt_strategy("AAA/USDT:USDT", "1h", flat, "bull", True, {})
    assert row["trade_status"] == "rejected"
    assert bot.queued_signals == {}


def test_trailing_manager_loser(monkeypatch):
    H = 3_600_000
    candles = [[21*H, 100, 100.2, 99, 99.5, 0], [22*H, 99.5, 99.6, 97, 97.5, 0]]
    moves, closed, now = _wire_trailing_mocks(monkeypatch, candles)
    sig = _sig(now)
    bot._manage_trailing_signal(sig)
    assert sig.status == "SL"
    assert round(sig.exit_price, 4) == 98.0
    assert round(sig.pnl_pct, 3) == -2.0
