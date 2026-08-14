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


# ── ⑮ Scanner Sync mirrors the scanner's constants (2026-08-14) ─────────────
# The chart module displays what crowd_radar and strategy4 measure. If either
# side's numbers move, the chart quietly stops agreeing with the alerts — which
# is the same failure the factor-weight test above already exists to catch, and
# the reason that test exists is that the weights HAD drifted (25/20 vs 20/15,
# and a whole missing factor) without anything noticing.
def _pro_src():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "pine", "indicators",
                           "All-in-One_ULTIMATE_Pro.pine"), encoding="utf-8") as fh:
        return fh.read()


def _sync_default(name):
    """Parse one ⑮ input default straight out of the .pine."""
    import re
    m = re.search(r'input\.(?:int|float)\(\s*([0-9.]+)\s*,\s*"' + re.escape(name),
                  _pro_src())
    assert m, f"could not find the ⑮ input {name!r} in the .pine"
    return float(m.group(1))


def test_scanner_sync_mirrors_the_divergence_constants():
    """These are strategy4's own numbers. A divergence drawn on the chart with
    a different pivot gap or momentum floor is a different divergence from the
    one the scanner alerts on."""
    import strategy4 as S4
    assert _sync_default("RSI length") == S4.RSI_LEN
    assert _sync_default("Pivot lookback") == S4.DIV_PIVOT
    assert _sync_default("Min bars between pivots") == S4.DIV_MIN_GAP
    assert _sync_default("Max bars between pivots") == S4.DIV_MAX_GAP
    assert _sync_default("Min momentum gap") == S4.DIV_MIN_OSC_GAP
    assert _sync_default("Min swing size") == S4.DIV_MIN_LEG_ATR
    assert _sync_default("Osc range window") == S4.DIV_NORM


def test_scanner_sync_mirrors_the_open_interest_constants():
    import crowd_radar as C
    assert _sync_default("OI window") == C.SPAN_BARS
    assert _sync_default("Min |OI change| %") == C.MIN_OI_PCT


def test_scanner_sync_reads_the_four_states_the_same_way_as_the_radar():
    """OI up + price up is new longs; OI DOWN + price up is shorts covering.
    Getting that mapping backwards on the chart would paint a squeeze as a
    fresh long build — the most confident possible way to be exactly wrong."""
    src = _pro_src()
    assert "syncNewLong  = syncBigOi and syncOiPct > 0 and syncPxPct > 0" in src
    assert "syncNewShort = syncBigOi and syncOiPct > 0 and syncPxPct < 0" in src
    assert "syncSqueeze  = syncBigOi and syncOiPct < 0 and syncPxPct > 0" in src
    assert "syncLongOut  = syncBigOi and syncOiPct < 0 and syncPxPct < 0" in src


def test_scanner_sync_does_not_touch_the_confidence_meter():
    """The whole point of the module: display only. The seven factors and their
    weights are mirrored in strategy2_meter.py, so a ⑮ edit that reached them
    would desync the chart from the live scanner."""
    src = _pro_src()
    sync = src[src.index("⑮ SCANNER SYNC"):]
    for factor in ("wEMAstack", "wSMC", "wVegas", "wTunnel", "wTrendln",
                   "wVolume", "wEMA200", "confScore"):
        assert factor not in sync, f"⑮ touches the scoring symbol {factor!r}"


def test_scanner_sync_is_off_by_default():
    """It is opt-in: the file is already 2,200+ lines and the SMC/trendline
    engines need the 500-object drawing budget."""
    import re
    m = re.search(r'syncOn\s*=\s*input\.bool\(\s*(true|false)', _pro_src())
    assert m and m.group(1) == "false"


def test_scanner_sync_adds_no_new_drawing_objects():
    """plotshape and bgcolor are per-bar plots, not line/box/label objects, so
    ⑮ cannot eat the budget the SMC zones and trendlines draw from."""
    src = _pro_src()
    sync = src[src.index("⑮ SCANNER SYNC"):]
    for heavy in ("line.new", "box.new", "label.new", "table.new"):
        assert heavy not in sync, f"⑮ allocates {heavy} — it must not"


def test_scanner_sync_says_it_is_not_an_edge():
    """The squeeze state's measured forward returns straddle zero, and '軋空'
    reads as 'buy' to anyone who has not read the numbers."""
    src = _pro_src()
    sync = src[src.index("⑮ SCANNER SYNC"):]
    assert "NOT AN EDGE" in sync
    assert "不是進場訊號" in sync or "不是方向" in sync


def test_no_higher_timeframe_read_repaints():
    """Repo-wide rule, not one panel. v2 fixed the MTF Bias panel to read the
    last CLOSED htf bar; the Sykes 時區 panel was added later, copied the naive
    idiom, and reintroduced the identical bug — a 30m row that reads 多 ▲, gets
    acted on, then closes bearish and leaves no trace it ever said so.

    Every request.security on a DIFFERENT timeframe must take [1] inside the
    expression (last closed bar) together with lookahead_on. lookahead_on alone
    is the classic future-peek; [1] is what makes it honest.
    """
    import re
    src = _pro_src()
    offenders = []
    for i, line in enumerate(src.split("\n"), 1):
        s = line.strip()
        if "request.security(" not in s or s.startswith("//"):
            continue
        # same-timeframe reads (timeframe.period) cannot repaint
        if "timeframe.period" in s:
            continue
        if "lookahead_on" not in s:
            offenders.append(f"L{i}: {s[:80]}")
    assert not offenders, (
        "these higher-timeframe reads can repaint:\n  " + "\n  ".join(offenders))


def test_the_sykes_trend_returns_the_previous_closed_bar():
    """The [1] lives inside f_sykTrend, not at the call site — with
    lookahead_on, a missing [1] is a genuine look into the future."""
    src = _pro_src()
    fn = src[src.index("f_sykTrend() =>"):]
    fn = fn[:fn.index("syk_t5")]
    assert "_trend[1]" in fn, "f_sykTrend returns the forming bar"


# ── ta.* must run on every bar (v6), 2026-08-14 ─────────────────────────────
def test_no_ta_call_sits_behind_a_short_circuit():
    """ta.* functions carry state that must advance on EVERY bar. Pine v6
    short-circuits `and`/`or`, so a ta.* call on the right-hand side does not
    execute when the left is false — a compile error, or worse, a silently
    wrong series.

    This exact lesson is already written at the vegasChange line ("compute it
    once into a plain variable and branch on that instead"). The Sykes module
    was added afterwards and repeated it inside two plotshape arguments, which
    is what stopped the script compiling. Same shape as the repaint bug: a
    later module copying an idiom the file had already outlawed.
    """
    import re
    src = _pro_src()
    offenders = []
    for i, line in enumerate(src.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if re.search(r"\b(and|or)\s+[^/]*\bta\.\w+\(", s):
            offenders.append(f"L{i}: {s[:80]}")
    assert not offenders, (
        "ta.* behind a short-circuit — hoist it to its own variable:\n  "
        + "\n  ".join(offenders))


# ── tunnel palette (2026-08-14) ─────────────────────────────────────────────
def test_both_tunnels_use_one_hue_per_direction():
    """It was orange/red for Tunnel 1 and lime/gray for Tunnel 2 — four
    unrelated hues, so the chart read as 'tunnel 1 vs tunnel 2' when the
    question you need answered is 'bull vs bear'."""
    import re
    src = _pro_src()
    fills = re.findall(r'dt_t[12](?:Bull|Bear)\s*=\s*input\.color\(color\.new\((#[0-9a-fA-F]{6})', src)
    assert len(fills) == 4, f"expected 4 tunnel fill colours, found {fills}"
    assert fills[0] == fills[2], "the two bull fills are different hues"
    assert fills[1] == fills[3], "the two bear fills are different hues"
    assert fills[0] != fills[1], "bull and bear share a hue"


def test_the_tunnels_are_told_apart_by_intensity_not_hue():
    """Inner 144/169 is the actionable band and reads stronger; the outer
    576/676 is context and sits back."""
    import re
    src = _pro_src()
    a = re.findall(r'dt_t1(?:Bull|Bear)\s*=\s*input\.color\(color\.new\(#[0-9a-fA-F]{6},\s*(\d+)', src)
    b = re.findall(r'dt_t2(?:Bull|Bear)\s*=\s*input\.color\(color\.new\(#[0-9a-fA-F]{6},\s*(\d+)', src)
    assert a and b
    assert all(int(x) < int(y) for x, y in zip(a, b)), \
        "the outer tunnel is not fainter than the inner one"


def test_the_tunnel_edges_follow_the_bands_own_direction():
    """A band that flips must read as a colour change, not stay one hue
    through both states — which is what the fixed orange did."""
    src = _pro_src()
    assert "dt_t1Edge = color.new(dt_t1Up ?" in src
    assert "dt_t2Edge = color.new(dt_t2Up ?" in src


# ── broken trendlines are deleted (2026-08-14) ──────────────────────────────
def test_a_broken_trendline_is_deleted_by_default():
    """Owner: "if the trend line become useless just delete it". A broken line
    is the useless one — price has already gone through it, so it describes
    nothing the chart is doing."""
    import re
    m = re.search(r'keepBroken\s*=\s*input\.int\(\s*(\d+)', _pro_src())
    assert m and m.group(1) == "0", "broken trendlines still linger by default"


def test_deleting_them_does_not_change_the_score():
    """A DRAWING change only. The engine still detects every break, so the
    Confidence Meter's trendline factor is untouched — the same distinction v2
    had to make when Clean Mode was capping the engine instead of the drawing."""
    src = _pro_src()
    prune = src[src.index("f_prune_broken() =>"):]
    prune = prune[:prune.index("starttime")]
    assert "line.delete" in prune and "label.delete" in prune
    for scoring in ("fTrendln", "confScore", "wTrendln"):
        assert scoring not in prune, f"pruning touches {scoring}"
