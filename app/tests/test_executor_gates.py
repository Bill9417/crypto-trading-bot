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
