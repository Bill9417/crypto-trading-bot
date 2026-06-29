"""Strategy-2 LIVE execution layer (strategy2_live).

SAFETY: every test monkeypatches the config flags + the executor, so NOTHING ever
reaches Binance — safe to run while the live bot trades. Covers the 'best plan'
filter, the ATR trade-plan maths, and the maybe_trade safety gates (master switch,
one-engine-at-a-time, capacity, duplicate position).
"""
import config
import executor as E
import strategy2_live as L


# ── trade_levels: ATR stop + 1R/2R targets, capped at MAX_SL_PCT ─────────────
def test_trade_levels_long_uses_atr_and_2R_target():
    entry, sl, tp1, tp2 = L.trade_levels(100.0, is_long=True, atr=1.0)
    assert entry == 100.0
    # SL = entry - ATR*1.5 = 98.5 ; risk = 1.5 ; TP1 = +1R, TP2 = +2R
    assert round(sl, 4) == 98.5
    assert round(tp1, 4) == 101.5
    assert round(tp2, 4) == 103.0
    assert sl < entry < tp1 < tp2


def test_trade_levels_short_is_mirrored():
    entry, sl, tp1, tp2 = L.trade_levels(100.0, is_long=False, atr=1.0)
    assert round(sl, 4) == 101.5
    assert round(tp2, 4) == 97.0
    assert tp2 < tp1 < entry < sl


def test_trade_levels_caps_stop_at_max_sl_pct(monkeypatch):
    monkeypatch.setattr(config, "MAX_SL_PCT", 0.04)
    # A huge ATR would blow past the 4% cap → stop clamped to entry*(1-0.04).
    entry, sl, _, _ = L.trade_levels(100.0, is_long=True, atr=50.0)
    assert round(sl, 4) == 96.0


# ── passes_filter: the high-conviction "best plan" gate ──────────────────────
def test_filter_long_needs_high_score(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    assert L.passes_filter("long", 85, rank=0)[0] is True
    assert L.passes_filter("long", 90, rank=10)[0] is True
    assert L.passes_filter("long", 84, rank=0)[0] is False   # below floor
    assert L.passes_filter("long", 70, rank=0)[0] is False   # alert-but-not-trade


def test_filter_short_needs_low_score(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    monkeypatch.setattr(config, "ENABLE_SHORTS", True)
    assert L.passes_filter("short", 15, rank=0)[0] is True    # ceiling = 100-85
    assert L.passes_filter("short", 10, rank=0)[0] is True
    assert L.passes_filter("short", 16, rank=0)[0] is False
    assert L.passes_filter("short", 30, rank=0)[0] is False


def test_filter_blocks_shorts_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    monkeypatch.setattr(config, "ENABLE_SHORTS", False)
    assert L.passes_filter("short", 10, rank=0)[0] is False


def test_filter_blocks_illiquid_rank(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    assert L.passes_filter("long", 99, rank=150)[0] is False  # outside top-150
    assert L.passes_filter("long", 99, rank=149)[0] is True


# ── maybe_trade: the safety gates around the order ───────────────────────────
def _sig(score, direction="long"):
    return {"symbol": "ABC/USDT:USDT", "base": "ABC", "direction": direction,
            "score": score, "price": 100.0}


def test_maybe_trade_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE", False)
    calls = []
    monkeypatch.setattr(E, "open_trade", lambda *a, **k: calls.append(a))
    assert L.maybe_trade(_sig(99), ohlcv=None, rank=0) is None
    assert calls == []   # nothing placed


def test_maybe_trade_skips_when_s1_running(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE", True)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    monkeypatch.setattr(L, "s1_bot_running", lambda: True)
    placed = []
    monkeypatch.setattr(E, "open_trade", lambda *a, **k: placed.append(a))
    monkeypatch.setattr(L.telegram_utils, "send_message", lambda *a, **k: None)
    assert L.maybe_trade(_sig(99), ohlcv=None, rank=0) is None
    assert placed == []   # one-engine-at-a-time guard held


def test_maybe_trade_respects_capacity_and_dupes(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE", True)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    monkeypatch.setattr(L, "s1_bot_running", lambda: False)
    placed = []
    monkeypatch.setattr(E, "open_trade", lambda *a, **k: placed.append(a))
    # capacity full
    monkeypatch.setattr(E, "has_capacity", lambda: False)
    monkeypatch.setattr(E, "has_open_position", lambda s: False)
    assert L.maybe_trade(_sig(99), ohlcv=None, rank=0) is None
    # already in a position
    monkeypatch.setattr(E, "has_capacity", lambda: True)
    monkeypatch.setattr(E, "has_open_position", lambda s: True)
    assert L.maybe_trade(_sig(99), ohlcv=None, rank=0) is None
    assert placed == []


def test_maybe_trade_places_when_all_gates_pass(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE", True)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_LIGHTS", 5)
    monkeypatch.setattr(config, "MAX_SL_PCT", 0.04)
    monkeypatch.setattr(L, "s1_bot_running", lambda: False)
    monkeypatch.setattr(E, "has_capacity", lambda: True)
    monkeypatch.setattr(E, "has_open_position", lambda s: False)
    captured = {}

    def fake_open_trade(symbol, direction, entry, sl, tp1, tp2, lights, aligned, **k):
        captured.update(symbol=symbol, direction=direction, entry=entry, sl=sl,
                        tp2=tp2, lights=lights, aligned=aligned, manage=k.get("manage"))
        return {"live": True, "error": None}

    monkeypatch.setattr(E, "open_trade", fake_open_trade)
    plan = L.maybe_trade(_sig(90), ohlcv=None, rank=3)
    assert plan and plan.get("error") is None
    assert captured["symbol"] == "ABC/USDT:USDT"
    assert captured["direction"] == "LONG"
    assert captured["lights"] == 5 and captured["aligned"] is True
    assert captured["manage"] == "bracket"
    # 100 price, no ATR → fixed 3% stop, TP2 at 2R below/above per direction
    assert captured["sl"] < captured["entry"] < captured["tp2"]


def test_maybe_trade_skips_low_conviction(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY2_LIVE", True)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_MIN_SCORE", 85)
    monkeypatch.setattr(config, "STRATEGY2_LIVE_TOP_N", 150)
    placed = []
    monkeypatch.setattr(E, "open_trade", lambda *a, **k: placed.append(a))
    assert L.maybe_trade(_sig(72), ohlcv=None, rank=0) is None  # 72 < 85
    assert placed == []


# ── _atr sanity ──────────────────────────────────────────────────────────────
def test_atr_none_without_history():
    assert L._atr(None) is None
    assert L._atr([[0, 1, 2, 0.5, 1, 9]] * 3, period=14) is None  # too few bars
