"""Strategy-2 trade plan (s2_plan) — the Entry/SL/TP maths behind every alert.

These moved out of strategy2_live.py when that module was retired: the plan
survives because the alert, the /strategy2 page and the outcome tracker all
publish it; the order path did not, because the signal it would have traded
measures −0.081R over 22,631 outcomes with the interval clear of zero.
"""
import config
import s2_plan as L


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


def test_trade_levels_without_atr_falls_back_to_fixed_stop(monkeypatch):
    monkeypatch.setattr(config, "FIXED_SL_PCT", 0.03)
    monkeypatch.setattr(config, "MAX_SL_PCT", 0.04)
    entry, sl, tp1, tp2 = L.trade_levels(100.0, is_long=True, atr=None)
    assert round(sl, 4) == 97.0
    assert sl < entry < tp1 < tp2


# ── atr sanity ───────────────────────────────────────────────────────────────
def test_atr_none_without_history():
    assert L.atr(None) is None
    assert L.atr([[0, 1, 2, 0.5, 1, 9]] * 3, period=14) is None  # too few bars


def test_atr_is_the_mean_true_range():
    # 15 identical bars with a 2-point range → ATR is exactly 2.
    bars = [[i, 10, 11, 9, 10, 1] for i in range(15)]
    assert L.atr(bars, period=14) == 2.0


# ── the retirement itself ────────────────────────────────────────────────────
def test_no_module_can_turn_an_s2_signal_into_an_order():
    """The whole point of the retirement: nothing imports an S2 order path,
    and the scanner never touches the executor."""
    import importlib
    import inspect
    import strategy2_scanner
    assert importlib.util.find_spec("strategy2_live") is None
    src = inspect.getsource(strategy2_scanner)
    assert "import executor" not in src and "executor." not in src
    assert "STRATEGY2_LIVE" not in src
    assert not hasattr(config, "STRATEGY2_LIVE")
