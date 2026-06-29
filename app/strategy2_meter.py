"""
Strategy 2 — live confidence meter.

A Python re-implementation of the "Confidence Meter" from the TradingView
indicator (TV.pine, "All-in-One PRO: EMA + TL + MSB + SMC"). It scores 0–100
directional agreement across six factors, weighted EXACTLY as the Pine script:

    EMA Stack ........ 25   fast>med>slow>trend stacked (or all reversed)
    Price vs EMA200 .. 20   close above / below the 200 EMA
    SMC Structure .... 25   LuxAlgo internal swing-structure bias (length 5)
    Vegas Slope ...... 15   slope of the Vegas EMA200 (SMA5 of EMA200)
    Tunnel Position .. 10   close vs the Double-Tunnel EMAs (144/169 · 288/338)
    Trendline Break ..  5   NOT reproduced server-side → always neutral (see note)

Each factor returns +1 (bull) / -1 (bear) / 0 (neutral). The weighted net is
mapped to 0–100 with 50 = neutral, identical to the Pine formula:

    confScore = clamp(50 + Σ(factor*weight) / 2, 0, 100)

This is a CONFLUENCE METER, not a prediction or a trade command — it mirrors the
on-chart indicator so the same read is available on the dashboard.

Pure-compute: callers pass OHLCV (list of [ts,o,h,l,c,v], oldest→newest); there
is no network here. A full read needs ≈345 candles (outer tunnel EMA338 + the
Vegas SMA5). With fewer, the factors that cannot be computed degrade to neutral
and the result is flagged `insufficient`.

NOTE on the trendline factor: LonesomeTheBlue's stateful pivot/touch/break engine
can't be reproduced from a single snapshot, and at weight 5 it moves the score by
at most ±2.5, so it is intentionally held neutral. The practical score range is
therefore ~3–98 rather than 0–100.

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

# Candles needed for a full read: outer tunnel EMA338 + a little headroom.
MIN_CANDLES = 345

# (key, label, weight) — weights sum to 100, matching the Pine info panel.
_FACTORS = [
    ("ema_stack",    "EMA Stack",       25),
    ("price_ema200", "Price vs EMA200", 20),
    ("smc",          "SMC Structure",   25),
    ("vegas",        "Vegas Slope",     15),
    ("tunnel",       "Tunnel Position", 10),
    ("trendline",    "Trendline Break",  5),
]


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
            states["price_ema200"] = 1 if price > e200 else -1

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
        t144 = calculate_ema(closes, 144)
        t169 = calculate_ema(closes, 169)
        t288 = calculate_ema(closes, 288)
        t338 = calculate_ema(closes, 338)
        if None not in (t144, t169, t288, t338):
            if price > t144 and price > t288:
                states["tunnel"] = 1
            elif price < t169 and price < t338:
                states["tunnel"] = -1

        # Trendline break (weight 5) — held neutral, see module note.
        states["trendline"] = 0

    weights = {k: w for k, _, w in _FACTORS}
    net = sum(states[k] * weights[k] for k in weights)
    score = int(max(0, min(100, round(50 + net / 2.0))))
    bias = ("long" if score >= LONG_THRESHOLD
            else "short" if score <= SHORT_THRESHOLD else "neutral")

    factors = [
        {"key": k, "label": label, "weight": w,
         "state": states[k], "text": _state_text(states[k])}
        for k, label, w in _FACTORS
    ]
    return {
        "score": score,
        "bias": bias,
        "price": float(price),
        "insufficient": bool(insufficient),
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
