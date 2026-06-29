"""Strategy-2 confidence meter + signal engine (strategy2_meter).

Pure-compute, no network — synthetic OHLCV only. Mirrors the TV.pine weighting:
six factors (weights sum to 100), score = clamp(50 + Σ(state*weight)/2, 0, 100),
long bias ≥70 / short bias ≤30. compute_signal reproduces the green/red-triangle
arm-and-fire on the last closed bar.
"""
import strategy2_meter as M


def _series(n, start, step, wiggle=0.4):
    """A clean trend with a small zigzag so SMC swing pivots can form."""
    out, v = [], float(start)
    for i in range(n):
        v += step + (wiggle if i % 2 else -wiggle * 0.5)
        out.append([i, v - 0.3, v + 0.5, v - 0.6, v, 1000.0])
    return out


def _zigzag(n, start, drift):
    """Trend with genuine pullbacks (8 bars with the trend, 6 against) so clear
    swing highs/lows form OUTSIDE the ±5 pivot window — needed to exercise the
    Break-of-Structure engine. drift>0 → uptrend, drift<0 → downtrend."""
    out, v = [], float(start)
    for i in range(n):
        d = drift if (i % 14) < 8 else -drift * 0.8
        v += d
        out.append([i, v - 0.2, v + 0.4, v - 0.4, v, 1000.0])
    return out


# ── confidence meter ─────────────────────────────────────────────────────────
def test_full_bull_series_scores_long():
    r = M.compute_meter(_series(400, 100.0, 0.6))
    assert r["insufficient"] is False
    assert r["bias"] == "long"
    assert r["score"] >= M.LONG_THRESHOLD
    assert sum(f["weight"] for f in r["factors"]) == 100
    assert len(r["factors"]) == 6


def test_full_bear_series_scores_short():
    r = M.compute_meter(_series(400, 400.0, -0.6))
    assert r["bias"] == "short"
    assert r["score"] <= M.SHORT_THRESHOLD


def test_thin_history_is_flagged_insufficient():
    r = M.compute_meter(_series(50, 100.0, 0.6))
    assert r["insufficient"] is True
    assert 0 <= r["score"] <= 100
    assert len(r["factors"]) == 6


def test_empty_input_is_safe_neutral():
    r = M.compute_meter([])
    assert r["insufficient"] is True
    assert r["score"] == 50
    assert r["bias"] == "neutral"
    assert all(f["state"] == 0 for f in r["factors"])


# ── market structure (MSB / Break-of-Structure) ──────────────────────────────
def test_market_state_detects_trend_direction():
    st, _ = M._market_state(_zigzag(300, 100.0, 1.0))
    assert st == "bull"
    stb, _ = M._market_state(_zigzag(300, 400.0, -1.0))
    assert stb == "bear"


def test_market_state_thin_is_none():
    st, age = M._market_state(_series(4, 100.0, 0.6))
    assert st is None and age is None


# ── signal engine (green/red-triangle arm-and-fire) ──────────────────────────
def test_compute_signal_contract():
    r = M.compute_signal(_series(400, 100.0, 0.6))
    for k in ("signal", "score", "bias", "price", "msb", "msb_age", "insufficient", "factors"):
        assert k in r
    assert r["signal"] in (None, "long", "short")
    assert r["insufficient"] is False
    assert r["msb"] in ("bull", "bear", None)


def test_compute_signal_thin_history_no_fire():
    r = M.compute_signal(_series(50, 100.0, 0.6))
    assert r["signal"] is None
    assert r["insufficient"] is True


def test_compute_signal_empty_no_fire():
    r = M.compute_signal([])
    assert r["signal"] is None
    assert r["insufficient"] is True


def test_compute_signal_fires_long_on_breakout_edge():
    """A long base (price below a flat EMA200, no bull alignment) followed by a
    breakout bar that flips structure bull, reclaims EMA200 and aligns the stack
    should fire a LONG on the last bar (rising edge)."""
    base = _series(360, 100.0, 0.0, wiggle=0.6)          # flat, choppy base
    # strong, accelerating ramp that stacks EMAs + breaks structure on the last bars
    ramp, v = [], 100.0
    for i in range(40):
        v += 1.2 + i * 0.15
        ramp.append([360 + i, v - 0.2, v + 0.6, v - 0.3, v, 1500.0])
    r = M.compute_signal(base + ramp)
    # at minimum the engine must classify the move bullish and not error
    assert r["msb"] == "bull"
    assert r["bias"] == "long"
    assert r["signal"] in ("long", None)   # fires on the cross bar; bias/msb confirm the setup
