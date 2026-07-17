"""Re-entry gate + phantom-record backstop.

SAFETY: every test here monkeypatches the executor / bot helpers, so NOTHING
reaches Binance and no DB row is written — safe to run while the live bot trades.

Covers the two 2026-06-22 fixes:
  - executor.open_position_symbols()  → the live re-entry gate input
  - bot.activate_queued_signals()     → must NOT record a signal when no resting
                                        order is actually on the exchange (phantom)
"""
import types

import executor as E
from market_data import is_tradfi_market


# ── Fix 2: live-position re-entry gate input ─────────────────────────────────
def test_open_position_symbols_returns_live_symbols(monkeypatch):
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {
        "ok": True,
        "positions": [
            {"symbol": "MEGA/USDT:USDT", "side": "LONG", "contracts": 106.0},
            {"symbol": "SOL/USDT:USDT", "side": "SHORT", "contracts": 2.0},
        ],
    })
    assert E.open_position_symbols() == {"MEGA/USDT:USDT", "SOL/USDT:USDT"}
    assert E.has_open_position("MEGA/USDT:USDT") is True
    assert E.has_open_position("BTC/USDT:USDT") is False


def test_open_position_symbols_empty_when_snapshot_failed(monkeypatch):
    # A transient API failure must NOT block trading — return an empty set so the
    # re-entry gate simply doesn't fire this scan.
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {
        "ok": False, "error": "boom", "positions": [],
    })
    assert E.open_position_symbols() == set()
    assert E.has_open_position("MEGA/USDT:USDT") is False


def test_open_position_symbols_handles_empty_and_none(monkeypatch):
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": True, "positions": []})
    assert E.open_position_symbols() == set()
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": True, "positions": None})
    assert E.open_position_symbols() == set()


def test_open_position_symbols_skips_blank_symbol(monkeypatch):
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {
        "ok": True,
        "positions": [{"symbol": None, "contracts": 1.0},
                      {"symbol": "ETH/USDT:USDT", "contracts": 1.0}],
    })
    assert E.open_position_symbols() == {"ETH/USDT:USDT"}


# ── Fix 1: phantom-record backstop in activate_queued_signals ────────────────
def _arm_one_long(bot, *, has_resting):
    """Queue a single entry-touched LONG and stub every external call so
    activate_queued_signals runs in isolation. `has_resting` decides whether a
    real resting order exists on the exchange."""
    sym = "FOO/USDT:USDT"
    bot.queued_signals = {(sym, "1h", "LONG"): {
        "symbol": sym, "timeframe": "1h", "direction": "LONG",
        "entry": 100.0, "tp": 110.0, "tp2": 120.0, "sl": 90.0,
        "lights_count": 5, "rsi_value": 50.0, "trend_aligned": True,
        "smc_zone": None, "manage": "bracket", "trail_dist": None,
        "strategy": "default",
    }}
    # Price sitting exactly at entry → entry is "touched"; TP/SL untouched.
    bot.get_live_tickers = lambda syms: {sym: {"last": 100.0}}
    bot.get_symbol_ohlcv = lambda *a, **k: ([], None)
    bot.target_reached_before_entry = lambda *a, **k: False
    bot.update_signal_state_in_ui = lambda *a, **k: None
    bot.executor.uses_resting_orders = lambda: True
    bot.executor.has_resting_order = lambda s, d: has_resting
    bot.executor.on_resting_filled = lambda s, d: None
    return sym


def test_phantom_setup_is_not_recorded(monkeypatch):
    """No resting order on the exchange → no record, queue entry cleared."""
    import bot
    calls = []
    monkeypatch.setattr(bot, "record_signal", lambda *a, **k: calls.append(a) or (True, "recorded"))
    _arm_one_long(bot, has_resting=False)
    bot.activate_queued_signals()
    assert calls == []                 # record_signal must NOT have been called
    assert bot.queued_signals == {}    # phantom setup dropped from the queue


def test_real_fill_is_recorded(monkeypatch):
    """A resting order IS on the exchange → the legit fill records normally."""
    import bot
    calls = []
    monkeypatch.setattr(bot, "record_signal", lambda *a, **k: calls.append(a) or (True, "recorded"))
    _arm_one_long(bot, has_resting=True)
    bot.activate_queued_signals()
    assert len(calls) == 1             # record_signal was called for the real fill


# ── Cap reconcile: S2 has no per-fill callback, so each sweep must release
#    brackets whose position has closed (else the cap wedges shut). ────────────
def test_reconcile_releases_closed_keeps_open(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {
        "ok": True,
        "positions": [{"symbol": "AAA/USDT:USDT", "side": "LONG", "contracts": 5.0}],
    })
    closed = []
    monkeypatch.setattr(E, "on_trade_closed", lambda s, d: closed.append((s, d)))
    # AAA still open on the exchange; BBB has closed. Both old enough to be eligible.
    monkeypatch.setattr(E, "_active_brackets", {
        ("AAA/USDT:USDT", "LONG"): {"opened_at": 0.0},
        ("BBB/USDT:USDT", "SHORT"): {"opened_at": 0.0},
    })
    res = E.reconcile_open_positions()
    assert res["ok"] is True
    assert closed == [("BBB/USDT:USDT", "SHORT")]        # only the closed one released
    assert res["released"] == [("BBB/USDT:USDT", "SHORT")]


def test_reconcile_skips_recently_opened(monkeypatch):
    import time as _t
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": True, "positions": []})
    closed = []
    monkeypatch.setattr(E, "on_trade_closed", lambda s, d: closed.append((s, d)))
    # Just-opened: the fill may not have propagated into the snapshot yet → keep it.
    monkeypatch.setattr(E, "_active_brackets", {("CCC/USDT:USDT", "LONG"): {"opened_at": _t.time()}})
    res = E.reconcile_open_positions()
    assert closed == [] and res["released"] == []


def test_reconcile_no_prune_on_snapshot_failure(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": False, "positions": []})
    closed = []
    monkeypatch.setattr(E, "on_trade_closed", lambda s, d: closed.append((s, d)))
    monkeypatch.setattr(E, "_active_brackets", {("DDD/USDT:USDT", "LONG"): {"opened_at": 0.0}})
    res = E.reconcile_open_positions()
    assert res["ok"] is False and closed == []           # fail-safe: prune nothing


# ── Hourly RSI-extreme Telegram digest (alert-only) ──────────────────────────
def test_rsi_digest_lists_extremes(monkeypatch):
    import bot
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(bot, "RSI_ALERT_ENABLED", True)
    monkeypatch.setattr(bot, "RSI_ALERT_ALWAYS", False)
    monkeypatch.setattr(bot, "RSI_ALERT_HIGH", 90.0)
    monkeypatch.setattr(bot, "RSI_ALERT_LOW", 10.0)
    monkeypatch.setattr(bot, "RSI_ALERT_TIMEFRAME", "1h")
    bot._send_rsi_extreme_alert([
        {"symbol": "AAA", "tf": "1h", "rsi": 93.2, "kind": "overbought", "price": 1.23},
        {"symbol": "BBB", "tf": "1h", "rsi": 6.1, "kind": "oversold", "price": 0.004},
    ])
    assert len(sent) == 1
    assert "AAA" in sent[0] and "BBB" in sent[0]
    assert "Overbought" in sent[0] and "Oversold" in sent[0]


def test_rsi_digest_silent_when_none(monkeypatch):
    import bot
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(bot, "RSI_ALERT_ENABLED", True)
    monkeypatch.setattr(bot, "RSI_ALERT_ALWAYS", False)
    bot._send_rsi_extreme_alert([])
    assert sent == []                                    # no extremes → no message (no spam)


def test_rsi_digest_heartbeat_when_always(monkeypatch):
    import bot
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(bot, "RSI_ALERT_ENABLED", True)
    monkeypatch.setattr(bot, "RSI_ALERT_ALWAYS", True)
    monkeypatch.setattr(bot, "RSI_ALERT_HIGH", 90.0)
    monkeypatch.setattr(bot, "RSI_ALERT_LOW", 10.0)
    monkeypatch.setattr(bot, "RSI_ALERT_TIMEFRAME", "1h")
    bot._send_rsi_extreme_alert([])
    assert len(sent) == 1 and "no coins" in sent[0]


# ── Strategy-2 30-min Telegram digest (grouped, clean form) ──────────────────
def test_strategy2_digest_is_grouped_and_sorted(monkeypatch):
    import strategy2_scanner as SC
    sent = []
    monkeypatch.setattr(SC.telegram_utils, "send_message", lambda m, *a, **k: sent.append(m))
    SC._send_digest([
        {"base": "AAA", "direction": "long", "score": 88, "price": 1.234},
        {"base": "BBB", "direction": "short", "score": 12, "price": 0.005},
        {"base": "CCC", "direction": "long", "score": 91, "price": 64230.0},
    ])
    assert len(sent) == 1                      # ONE message, not three
    msg = sent[0]
    assert "策略2 訊號榜" in msg and "3 個新訊號" in msg
    assert "🟢 做多" in msg and "🔴 做空" in msg
    assert msg.index("CCC") < msg.index("AAA")   # longs highest-conviction first


def test_strategy2_digest_silent_when_empty(monkeypatch):
    import strategy2_scanner as SC
    sent = []
    monkeypatch.setattr(SC.telegram_utils, "send_message", lambda m, *a, **k: sent.append(m))
    SC._send_digest([])
    assert sent == []                          # nothing new → no message


# ── TradFi stock perps (2026-07-03): the account has not signed Binance's
#    TradFi agreement, so AAPL/QQQ/… reject every order with -4411. They must be
#    dropped from the universe AND refused by the executor as a backstop. ──────
def test_is_tradfi_market_classification():
    equity = {"info": {"underlyingType": "EQUITY", "contractType": "TRADIFI_PERPETUAL"}}
    coin = {"info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}}
    assert is_tradfi_market(equity) is True
    assert is_tradfi_market(coin) is False
    assert is_tradfi_market({}) is False       # missing info → not TradFi
    assert is_tradfi_market(None) is False


def test_scanner_universe_drops_tradfi_perps():
    import strategy2_scanner as SC
    fake_markets = {
        "BTC/USDT:USDT": {"swap": True, "quote": "USDT", "active": True,
                          "info": {"underlyingType": "COIN"}},
        "AAPL/USDT:USDT": {"swap": True, "quote": "USDT", "active": True,
                           "info": {"underlyingType": "EQUITY"}},
    }
    client = types.SimpleNamespace(
        call=lambda m, *a, **k: fake_markets if m == "load_markets" else {})
    syms = SC.universe(client)
    assert "BTC/USDT:USDT" in syms
    assert "AAPL/USDT:USDT" not in syms


def _fake_exchange_with(market_info):
    return types.SimpleNamespace(market=lambda s: {"info": market_info})


def test_untradeable_market_blocks_tradfi(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "_markets_loaded", True)
    monkeypatch.setattr(E, "EXCLUDE_TRADFI_PERPS", True)
    monkeypatch.setattr(E, "_get_exchange",
                        lambda: _fake_exchange_with({"underlyingType": "EQUITY"}))
    blocked, why = E.untradeable_market("AAPL/USDT:USDT")
    assert blocked is True and "TradFi" in why


def test_untradeable_market_allows_coin_perp(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "_markets_loaded", True)
    monkeypatch.setattr(E, "EXCLUDE_TRADFI_PERPS", True)
    monkeypatch.setattr(E, "_get_exchange",
                        lambda: _fake_exchange_with({"underlyingType": "COIN"}))
    assert E.untradeable_market("BTC/USDT:USDT") == (False, "")


def test_untradeable_market_noop_when_flag_off(monkeypatch):
    # After the user signs the agreement, EXCLUDE_TRADFI_PERPS=false must let
    # stock perps trade like any other market.
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "_markets_loaded", True)
    monkeypatch.setattr(E, "EXCLUDE_TRADFI_PERPS", False)
    monkeypatch.setattr(E, "_get_exchange",
                        lambda: _fake_exchange_with({"underlyingType": "EQUITY"}))
    assert E.untradeable_market("AAPL/USDT:USDT") == (False, "")


def test_open_trade_skips_untradeable_before_any_order(monkeypatch):
    monkeypatch.setattr(E, "untradeable_market", lambda s: (True, "TradFi stock perp"))
    sent = []
    monkeypatch.setattr(E, "send_message", lambda *a, **k: sent.append(a))
    plan = E.open_trade("AAPL/USDT:USDT", "LONG", 100.0, 99.0, 101.0, 102.0, 5, True, notify=False)
    assert plan["error"] and "untradeable" in plan["error"]
    assert plan["orders"] == [] and plan["live"] is False
    assert sent == []                          # skipped silently, no alert spam


# ── Free-margin pre-check (2026-07-03): skip cleanly instead of eating a
#    -2019 'Margin is insufficient' rejection on every over-committed signal. ──
def test_insufficient_free_margin_blocks_when_broke(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot",
                        lambda *a, **k: {"ok": True, "balance": {"available": 1.0}})
    lacking, why = E.insufficient_free_margin(1.5)
    assert lacking is True and "free 1.00" in why


def test_insufficient_free_margin_allows_when_funded(monkeypatch):
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot",
                        lambda *a, **k: {"ok": True, "balance": {"available": 10.0}})
    assert E.insufficient_free_margin(1.5) == (False, "")


def test_insufficient_free_margin_fails_open(monkeypatch):
    # Unreadable snapshot / missing field must NOT block trading — the exchange
    # remains the final arbiter for those cases.
    monkeypatch.setattr(E, "is_live", lambda: True)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": False})
    assert E.insufficient_free_margin(1.5) == (False, "")
    monkeypatch.setattr(E, "account_snapshot",
                        lambda *a, **k: {"ok": True, "balance": {"available": None}})
    assert E.insufficient_free_margin(1.5) == (False, "")
