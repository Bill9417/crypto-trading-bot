"""
Strategy 2 — live confidence meter.

A Python re-implementation of the "Confidence Meter" from the TradingView
indicator (pine/indicators/All-in-One_ULTIMATE.pine). It scores 0–100
directional agreement across seven factors, weighted as the Pine script does:

    EMA Stack ........ 20   fast>med>slow>trend stacked (or all reversed)
    Price vs EMA200 .. 15   close above / below the 200 EMA
    SMC Structure .... 25   LuxAlgo internal swing-structure bias (length 5)
    Vegas Slope ...... 15   slope of the Vegas EMA200 (SMA5 of EMA200)
    Tunnel Position .. 10   close vs the Double-Tunnel EMAs (144/169 · 288/338)
    Trendline Break ..  5   NOT reproducible server-side → abstains (see note)
    Volume Bias ...... 10   close vs the volume-weighted mean price (360 bars)

Each factor returns +1 (bull) / -1 (bear) / 0 (neutral), and the weighted net is
mapped to 0–100 with 50 = neutral:

    confScore = clamp(50 + 50 * Σ(factor*weight) / activeWeight, 0, 100)

`activeWeight` is the weight of the factors that actually have an OPINION this
bar. A factor that abstains (cannot be computed) is removed from the
denominator; a factor voting 0 because it is genuinely undecided keeps its
weight. This matters — "no trendline has broken" is silence, whereas "the EMAs
are not stacked" is a real neutral reading. It also matches the Pine, whose
trendline factor abstains on roughly 999 bars out of 1000.

This is a CONFLUENCE METER, not a prediction or a trade command — it mirrors the
on-chart indicator so the same read is available on the dashboard.

Pure-compute: callers pass OHLCV (list of [ts,o,h,l,c,v], oldest→newest); there
is no network here. A full read needs ≈365 candles (the 360-bar volume window,
which is longer than the outer tunnel EMA338 + Vegas SMA5). With fewer, the
volume factor abstains, the rest degrade to neutral, and the result is flagged
`insufficient`. The web callers fetch 450, so this is not a practical limit.

──────────────────────────────────────────────────────────────────────────────
2026-07-25 — THIS FILE HAD DRIFTED OUT OF SYNC WITH THE CHART IT MIRRORS.
The docstring claimed the factors were "weighted EXACTLY as the Pine script".
They were not. It used 25/20 for the EMA factors where both .pine files use
20/15, and it omitted the 10-weight Volume factor entirely. On the same
candles the /strategy2 page and the chart could land 10 points apart, and
they disagreed on the long/short/neutral verdict in 11% of factor
combinations — so the page could read "long" while the chart read "neutral".
Two further defects were imported from the pre-v2 Pine and are fixed here too:

  · TUNNEL ASYMMETRY. Bull required price above t144 — the tunnel's UPPER
    line in an uptrend. Bear required price below t169, which in a downtrend
    is ALSO the upper line. Price sitting INSIDE the tunnel scored a full -1
    where the mirror-image bull case scored 0: 5 free bearish points.
  · UNREACHABLE SCORE ENDS. The trendline factor can never be computed here,
    yet its weight stayed in the denominator, capping the score at ~3–98.
    It now abstains, so 0 and 100 mean what they say.

NOTE on the trendline factor: LonesomeTheBlue's stateful pivot/touch/break
engine can't be reproduced from a single snapshot, so it abstains permanently
server-side. The Pine abstains too whenever no break is recent, which is
almost always — so the two denominators agree on the overwhelming majority of
bars.

NOTE on SMC divergence: this meter uses the LuxAlgo *internal* structure bias
(length 5), matching the Pine `fSMC` factor. The scanner/FUNNEL pages use
`smc.analyze_smc` swing structure (length 50), so the two reads can legitimately
differ.
"""

import numpy as np
import pandas as pd

from indicators import calculate_ema
import smc

# Pine thresholds: long bias ≥70, short bias ≤30 (i.e. 100 - 70).
LONG_THRESHOLD = 70
SHORT_THRESHOLD = 30

# Pine's volBiasLen: window for the volume-weighted mean price.
VOL_BIAS_LEN = 360

# Candles needed for a full read. The volume window (360) is now the binding
# constraint, not the outer tunnel EMA338 — hence 365, not the old 345.
MIN_CANDLES = VOL_BIAS_LEN + 5

# (key, label, weight) — weights sum to 100, matching the Pine info panel.
_FACTORS = [
    ("ema_stack",    "EMA Stack",       20),
    ("price_ema200", "Price vs EMA200", 15),
    ("smc",          "SMC Structure",   25),
    ("vegas",        "Vegas Slope",     15),
    ("tunnel",       "Tunnel Position", 10),
    ("trendline",    "Trendline Break",  5),
    ("volume",       "Volume Bias",     10),
]

# Factors that can ABSTAIN — their weight leaves the denominator rather than
# counting as a neutral vote. `trendline` always abstains here (the stateful
# Pine engine has no server-side equivalent); `volume` abstains only until the
# 360-bar window has filled. Everything else votes 0 when undecided, exactly
# as the Pine does.
_ALWAYS_ABSTAINS = ("trendline",)


def _state_text(state: int) -> str:
    return "bull" if state > 0 else "bear" if state < 0 else "neutral"


def compute_meter(ohlcv) -> dict:
    """Compute the confidence meter for one symbol's OHLCV series.

    Returns {score, bias, price, insufficient, factors:[{key,label,weight,state,text}]}.
    `state` is +1/-1/0, `text` is 'bull'/'bear'/'neutral', and `bias` is
    'long'/'short'/'neutral' derived from the score thresholds.
    """
    n = len(ohlcv) if ohlcv else 0
    insufficient = n < MIN_CANDLES

    closes = [float(c[4]) for c in ohlcv] if n else []
    highs = [float(c[2]) for c in ohlcv] if n else []
    lows = [float(c[3]) for c in ohlcv] if n else []
    price = closes[-1] if closes else 0.0

    states = {k: 0 for k, _, _ in _FACTORS}
    speaks = {k: k not in _ALWAYS_ABSTAINS for k, _, _ in _FACTORS}
    speaks["volume"] = False        # flipped on once the 360-bar window fills

    if n:
        # EMA stack (20/50/100/200) + price vs EMA200.
        e20 = calculate_ema(closes, 20)
        e50 = calculate_ema(closes, 50)
        e100 = calculate_ema(closes, 100)
        e200 = calculate_ema(closes, 200)
        if None not in (e20, e50, e100, e200):
            if e20 > e50 > e100 > e200:
                states["ema_stack"] = 1
            elif e20 < e50 < e100 < e200:
                states["ema_stack"] = -1
        if e200 is not None:
            # Pine: `close > emaTrend ? 1 : close < emaTrend ? -1 : 0` — exact
            # equality is 0, not bearish.
            states["price_ema200"] = 1 if price > e200 else -1 if price < e200 else 0

        # SMC internal structure bias (LuxAlgo internal length = 5).
        try:
            sh, sl = smc._find_swing_pivots(np.asarray(highs), np.asarray(lows), smc.INTERNAL_LENGTH)
            states["smc"] = int(smc._swing_trend(np.asarray(closes), sh, sl))
        except Exception:  # noqa: BLE001 — never let SMC break the meter
            states["smc"] = 0

        # Vegas EMA200 slope = sign of d/dt (SMA5 of EMA200).
        vegas = pd.Series(closes).ewm(span=200, adjust=False).mean().rolling(5).mean()
        if len(vegas) >= 2 and pd.notna(vegas.iloc[-1]) and pd.notna(vegas.iloc[-2]):
            diff = float(vegas.iloc[-1] - vegas.iloc[-2])
            states["vegas"] = 1 if diff > 0 else -1 if diff < 0 else 0

        # Double Tunnel position: inner 144/169, outer 288/338.
        # Both sides require price fully OUTSIDE both tunnels. The old test
        # (`price > t144` for bull, `price < t169` for bear) was asymmetric:
        # in a downtrend t169 is the tunnel's UPPER line, so price inside the
        # tunnel scored a full -1 while the mirror bull case scored 0.
        t144 = calculate_ema(closes, 144)
        t169 = calculate_ema(closes, 169)
        t288 = calculate_ema(closes, 288)
        t338 = calculate_ema(closes, 338)
        if None not in (t144, t169, t288, t338):
            if price > max(t144, t169) and price > max(t288, t338):
                states["tunnel"] = 1
            elif price < min(t144, t169) and price < min(t288, t338):
                states["tunnel"] = -1

        # Volume Bias: price vs the volume-weighted mean price of the last
        # VOL_BIAS_LEN bars — the Pine's POC stand-in, O(1) and identical on
        # history and realtime (unlike the drawn Volume Profile, which only
        # exists on the last bar).
        if n >= VOL_BIAS_LEN:
            window = ohlcv[-VOL_BIAS_LEN:]
            vols = [float(c[5]) for c in window]
            vsum = sum(vols)
            if vsum > 0:
                vmean = sum(float(c[4]) * v
                            for c, v in zip(window, vols, strict=True)) / vsum
                states["volume"] = 1 if price > vmean else -1 if price < vmean else 0
                speaks["volume"] = True

        # Trendline break — abstains permanently, see module note.

    weights = {k: w for k, _, w in _FACTORS}
    active_w = sum(w for k, _, w in _FACTORS if speaks[k])
    net = sum(states[k] * weights[k] for k in weights)
    score = (int(max(0, min(100, round(50 + 50.0 * net / active_w))))
             if active_w else 50)
    bias = ("long" if score >= LONG_THRESHOLD
            else "short" if score <= SHORT_THRESHOLD else "neutral")

    factors = [
        {"key": k, "label": label, "weight": w,
         "state": states[k], "text": _state_text(states[k]),
         "abstain": not speaks[k]}
        for k, label, w in _FACTORS
    ]
    return {
        "score": score,
        "bias": bias,
        "price": float(price),
        "insufficient": bool(insufficient),
        "active_weight": active_w,
        "factors": factors,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Signal Layer — reproduces the green/red triangles from TV.pine on the last
#  CLOSED bar. The Pine "longSig" is an arm-and-fire: an MSB (market-structure)
#  flip UP arms a long, which then FIRES on the first bar where price is above
#  EMA200 AND the confidence meter ≥ 70. Shorts are the mirror (flip down, below
#  EMA200, meter ≤ 30). We detect the firing as a rising EDGE of that full
#  condition (true now, not true on the prior bar), which matches the indicator's
#  once-per-setup behaviour without per-bar score recomputation.
#
#  Fidelity note: the MSB here is a Break-of-Structure on confirmed swing pivots
#  (close breaks the most recent swing high/low), which is the standard, robust
#  form of EmreKb's fib-buffered zigzag "market" flip — same setup, not a
#  pixel-identical port.
# ════════════════════════════════════════════════════════════════════════════

MSB_PIVOT = 5                 # bars each side that confirm a swing pivot (15m)
SIGNAL_MIN_CANDLES = MIN_CANDLES + 2   # need 'now' and 'prev' bars for the edge


def _market_state(ohlcv, pivot: int = MSB_PIVOT):
    """Current market-structure state via Break-of-Structure (no lookahead).

    market flips 'bull' when a close breaks above the most recent CONFIRMED swing
    high, 'bear' when it breaks below the most recent swing low. Returns
    (state, bars_since_flip) with state in {'bull','bear',None}.
    """
    n = len(ohlcv) if ohlcv else 0
    if n < pivot * 2 + 2:
        return None, None
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]
    closes = [float(c[4]) for c in ohlcv]

    is_sh = [False] * n
    is_sl = [False] * n
    for i in range(pivot, n - pivot):
        if highs[i] == max(highs[i - pivot:i + pivot + 1]):
            is_sh[i] = True
        if lows[i] == min(lows[i - pivot:i + pivot + 1]):
            is_sl[i] = True

    state = None
    last_flip = None
    last_sh = None
    last_sl = None
    for i in range(n):
        j = i - pivot                       # a pivot at j is only confirmed pivot bars later
        if j >= 0:
            if is_sh[j]:
                last_sh = highs[j]
            if is_sl[j]:
                last_sl = lows[j]
        c = closes[i]
        if last_sh is not None and c > last_sh and state != "bull":
            state, last_flip, last_sh = "bull", i, None   # consume → require a fresh swing to re-flip
        elif last_sl is not None and c < last_sl and state != "bear":
            state, last_flip, last_sl = "bear", i, None
    bars_since = (n - 1 - last_flip) if last_flip is not None else None
    return state, bars_since


def _factor_state(meter: dict, key: str) -> int:
    for f in meter["factors"]:
        if f["key"] == key:
            return f["state"]
    return 0


def compute_signal(ohlcv) -> dict:
    """Detect whether a long/short signal FIRED on the last closed bar (the green
    /red triangle). Returns the meter snapshot plus {signal, msb, msb_age}.
    `signal` is 'long'/'short'/None."""
    n = len(ohlcv) if ohlcv else 0
    if n < SIGNAL_MIN_CANDLES:
        m = compute_meter(ohlcv)
        return {**m, "signal": None, "msb": None, "msb_age": None, "insufficient": True}

    m_now = compute_meter(ohlcv)
    m_prev = compute_meter(ohlcv[:-1])
    mkt_now, age = _market_state(ohlcv)
    mkt_prev, _ = _market_state(ohlcv[:-1])

    def long_ok(m, mkt):
        return m["score"] >= LONG_THRESHOLD and _factor_state(m, "price_ema200") > 0 and mkt == "bull"

    def short_ok(m, mkt):
        return m["score"] <= SHORT_THRESHOLD and _factor_state(m, "price_ema200") < 0 and mkt == "bear"

    signal = None
    if long_ok(m_now, mkt_now) and not long_ok(m_prev, mkt_prev):
        signal = "long"
    elif short_ok(m_now, mkt_now) and not short_ok(m_prev, mkt_prev):
        signal = "short"

    return {**m_now, "signal": signal, "msb": mkt_now, "msb_age": age}
