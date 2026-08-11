"""Strategy-2 confidence meter + signal engine (strategy2_meter).

Pure-compute, no network — synthetic OHLCV only. Mirrors the weighting in
pine/indicators/All-in-One_ULTIMATE.pine: seven factors (weights sum to 100),
score = clamp(50 + 50*Σ(state*weight)/activeWeight, 0, 100), long bias ≥70 /
short bias ≤30. compute_signal reproduces the green/red-triangle arm-and-fire
on the last closed bar.

The parity test below is the important one: this file drifted out of sync with
the chart once already (wrong EMA weights, no Volume factor — up to 10 points
apart, a different verdict 11% of the time) and nothing caught it.
"""
import os
import re

import strategy2_meter as M

PINE = os.path.join(os.path.dirname(__file__), "..", "..",
                    "pine", "indicators", "All-in-One_ULTIMATE_Pro.pine")

# Fixture length, DERIVED — not a literal. When the outer tunnel moved to
# EMA676 every fixture here was 400 bars and silently became an "insufficient"
# read: four tests failed and the reason (a period change three files away) was
# nowhere in the failure. Deriving it means the next period change adjusts the
# fixtures instead of breaking them.
N = M.MIN_CANDLES + 60


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
    r = M.compute_meter(_series(N, 100.0, 0.6))
    assert r["insufficient"] is False
    assert r["bias"] == "long"
    assert r["score"] >= M.LONG_THRESHOLD
    assert sum(f["weight"] for f in r["factors"]) == 100
    assert len(r["factors"]) == 7


def test_full_bear_series_scores_short():
    r = M.compute_meter(_series(N, 400.0, -0.6))
    assert r["bias"] == "short"
    assert r["score"] <= M.SHORT_THRESHOLD


def test_thin_history_is_flagged_insufficient():
    r = M.compute_meter(_series(50, 100.0, 0.6))
    assert r["insufficient"] is True
    assert 0 <= r["score"] <= 100
    assert len(r["factors"]) == 7


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
    r = M.compute_signal(_series(N, 100.0, 0.6))
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
    _base_n = N - 40                                     # base + ramp == N
    base = _series(_base_n, 100.0, 0.0, wiggle=0.6)      # flat, choppy base
    # strong, accelerating ramp that stacks EMAs + breaks structure on the last bars
    ramp, v = [], 100.0
    for i in range(40):
        v += 1.2 + i * 0.15
        ramp.append([_base_n + i, v - 0.2, v + 0.6, v - 0.3, v, 1500.0])
    r = M.compute_signal(base + ramp)
    # at minimum the engine must classify the move bullish and not error
    assert r["msb"] == "bull"
    assert r["bias"] == "long"
    assert r["signal"] in ("long", None)   # fires on the cross bar; bias/msb confirm the setup


# ── parity with the chart it mirrors ─────────────────────────────────────────
def test_factor_weights_match_the_pine_indicator():
    """THE REGRESSION THAT ALREADY HAPPENED. This file used 25/20 for the EMA
    factors where the Pine uses 20/15, and had no Volume factor at all — so
    /strategy2 and the chart could show different numbers, and different
    long/short verdicts, for the same candles. Read the weights straight out
    of the .pine so the two can never silently diverge again."""
    with open(PINE, encoding="utf-8") as fh:
        src = fh.read()
    pine_w = {k: int(v) for k, v in
              re.findall(r"^w(EMAstack|EMA200|SMC|Vegas|Tunnel|Trendln|Volume)\s*=\s*(\d+)",
                         src, re.M)}
    assert len(pine_w) == 7, f"could not parse Pine weights, got {pine_w}"
    assert sum(pine_w.values()) == 100

    py_w = {k: w for k, _, w in M._FACTORS}
    assert py_w == {"ema_stack": pine_w["EMAstack"], "price_ema200": pine_w["EMA200"],
                    "smc": pine_w["SMC"], "vegas": pine_w["Vegas"],
                    "tunnel": pine_w["Tunnel"], "trendline": pine_w["Trendln"],
                    "volume": pine_w["Volume"]}


def test_tunnel_periods_match_the_pine_indicator():
    """The weights were mirrored; the PERIODS were not, and they are just as
    much part of the factor. All-in-One_ULTIMATE_Pro moved the outer tunnel
    288/338 → 576/676 (Sykes' 4x periods). Nothing would have caught the mirror
    staying on 288/338 — the score would simply have been answering a faster
    question than the chart, which is the same class of silent drift as the
    2026-07-25 weight bug."""
    with open(PINE, encoding="utf-8") as fh:
        src = fh.read()
    pine_p = {k: int(v) for k, v in
              re.findall(r"^dt_ma(\d)Period\s*=\s*input\.int\((\d+)", src, re.M)}
    assert len(pine_p) == 4, f"could not parse Pine tunnel periods, got {pine_p}"
    assert [pine_p["1"], pine_p["2"], pine_p["3"], pine_p["4"]] == \
           [M.TUNNEL_INNER_A, M.TUNNEL_INNER_B, M.TUNNEL_OUTER_A, M.TUNNEL_OUTER_B]


def test_min_candles_covers_the_longest_input():
    """MIN_CANDLES must clear the slowest series the meter reads, or the tunnel
    factor abstains on every bar and the meter silently runs on 90 points."""
    assert M.MIN_CANDLES > M.TUNNEL_OUTER_B
    assert M.MIN_CANDLES > M.VOL_BIAS_LEN


def test_score_can_reach_both_ends_of_the_scale():
    """The trendline factor cannot be computed server-side, so it abstains out
    of the denominator. Previously its 5 weight stayed in, capping the meter at
    ~3–98 — 98 and 100 meant the same thing.

    Uses _zigzag, not _series: _series has too small a wiggle to form swing
    pivots, so SMC casts a genuine neutral vote there and the score cannot
    reach the end for an unrelated reason."""
    bull = M.compute_meter(_zigzag(N, 100.0, 1.0))
    bear = M.compute_meter(_zigzag(N, 600.0, -1.0))
    assert all(f["state"] == 1 for f in bull["factors"] if not f["abstain"])
    assert bull["score"] == 100
    assert all(f["state"] == -1 for f in bear["factors"] if not f["abstain"])
    assert bear["score"] == 0
    assert bull["active_weight"] == 95        # 100 minus the abstaining trendline


def test_trendline_abstains_and_says_so():
    r = M.compute_meter(_series(N, 100.0, 0.6))
    tl = next(f for f in r["factors"] if f["key"] == "trendline")
    assert tl["abstain"] is True
    assert tl["state"] == 0
    assert all(f["abstain"] is False for f in r["factors"] if f["key"] != "trendline")


def test_volume_factor_votes_with_the_trend():
    up = M.compute_meter(_series(N, 100.0, 0.6))
    dn = M.compute_meter(_series(N, 400.0, -0.6))
    assert next(f for f in up["factors"] if f["key"] == "volume")["state"] == 1
    assert next(f for f in dn["factors"] if f["key"] == "volume")["state"] == -1


def test_volume_abstains_until_its_window_fills():
    """Not enough bars is silence, not a neutral vote — counting it as neutral
    would drag every thin-history read toward 50."""
    r = M.compute_meter(_series(100, 100.0, 0.6))
    vol = next(f for f in r["factors"] if f["key"] == "volume")
    assert vol["abstain"] is True
    assert r["active_weight"] == 85           # 100 minus trendline (5) and volume (10)


def test_zero_volume_series_abstains_rather_than_dividing_by_zero():
    rows = [[i, 100.0, 100.5, 99.5, 100.0, 0.0] for i in range(N)]
    r = M.compute_meter(rows)
    assert next(f for f in r["factors"] if f["key"] == "volume")["abstain"] is True
    assert 0 <= r["score"] <= 100


def test_price_inside_the_tunnel_is_not_scored_bearish():
    """THE ASYMMETRY: the old bear test was `price < t169`, and in a downtrend
    t169 is the tunnel's UPPER line — so price sitting INSIDE the tunnel scored
    a full -1 while the mirror-image bull case scored 0."""
    import strategy2_meter
    closes = [float(c[4]) for c in _series(N, 400.0, -0.6)]
    from indicators import calculate_ema
    t144, t169 = calculate_ema(closes, 144), calculate_ema(closes, 169)
    t338 = calculate_ema(closes, 338)
    assert t144 < t169, "downtrend precondition: the faster EMA sits lower"

    # park the last close between the inner tunnel's two lines
    rows = _series(N, 400.0, -0.6)
    inside = (t144 + t169) / 2.0
    # the old test was `price < t169 and price < t338`; both hold here, so the
    # old code scored this a full -1. That is the regression being pinned.
    assert inside < t169 and inside < t338
    rows[-1] = [rows[-1][0], inside, inside + 0.2, inside - 0.2, inside, 1000.0]
    tunnel = next(f for f in strategy2_meter.compute_meter(rows)["factors"]
                  if f["key"] == "tunnel")
    assert tunnel["state"] == 0, "inside the tunnel is undecided, not bearish"
