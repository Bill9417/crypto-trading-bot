"""Live strategy resolution + executor bracket/move-stop helpers.

The bot trades Strategy 1 only (Strategies 2–5 + the live trailing manager + the
alt-strategy dispatch were removed 2026-06-28). The executor still carries the
trailing-aware bracket plumbing (DB columns + manage flag) for back-compat, so
those unit tests stay.

SAFETY: every test that touches the executor forces dry-run (executor.LIVE_TRADING
False) and/or mocks the exchange, so NOTHING here can ever reach Binance — these
run safely even while the live bot is trading.
"""
import types

import bot
import config as C
import executor as E


# ── live strategy resolution ─────────────────────────────────────────────────
def test_filter_records_by_strategy():
    import app as A
    rec = lambda strat: types.SimpleNamespace(strategy=strat)
    recs = [rec("default"), rec("legacy_x"), rec(None), rec("legacy_y")]
    # legacy NULL rows count as 'default' (S1)
    assert [r.strategy for r in A._filter_records_by_strategy(recs, "default")] == ["default", None]
    # scoping to any other tag excludes the default + NULL rows
    assert len(A._filter_records_by_strategy(recs, "legacy_x")) == 1
    assert A._filter_records_by_strategy(recs, "all") == recs       # no scoping
    assert A._filter_records_by_strategy(recs, None) == recs        # no scoping


def test_resolve_live_strategy_is_always_s1_bracket():
    # Only Strategy 1 remains, so resolution is constant regardless of env.
    assert bot.resolve_live_strategy() == ("default", "bracket")


# ── .env persistence helpers ─────────────────────────────────────────────────
def test_config_env_roundtrip(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LIVE_TRADING=true\nLEVERAGE=4\n")
    monkeypatch.setattr(C, "ENV_PATH", str(env))
    assert C.read_env_var("LIVE_STRATEGY", "default") == "default"   # absent → default
    C.set_env_var("LIVE_STRATEGY", "default")
    assert C.read_env_var("LIVE_STRATEGY") == "default"
    # replace-in-place, not append-a-duplicate; untouched keys preserved
    C.set_env_var("LIVE_STRATEGY", "legacy_x")
    body = env.read_text()
    assert body.count("LIVE_STRATEGY=") == 1
    assert "LIVE_STRATEGY=legacy_x" in body
    assert "LEVERAGE=4" in body


# ── executor: trailing-aware bracket + move_stop (executor.py unchanged) ──────
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
