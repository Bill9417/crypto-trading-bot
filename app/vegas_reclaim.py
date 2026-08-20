"""
🌊 Vegas 隧道翻多 — EMA200 turns up, price reclaims the tunnel, buyers show up.

Asked for 2026-08-21 off a DASH chart. On 2026-08-19 22:00 台北 all three of
these became true on the same 1h bar and DASH ran from 30.0 to 32.3:

  RECLAIM   the close crosses back ABOVE the Vegas tunnel (EMA144/EMA169)
            having been below it on the previous bar
  TURN      EMA200's slope is positive now and was negative within the last
            few bars — the "red to green" colour change on the chart
  SURGE     volume >= VOL_MULT x its own 20-bar average, on an UP bar, so the
            surge is buyers lifting offers rather than a flush

WHAT THE THREE GATES ARE ACTUALLY WORTH, measured on 197 perps x 4000 1h bars
(2026-03 → 2026-08, 1283 signals, next-bar entry, 1.5xATR stop / 2R target,
48h cap, net of fees+slippage). Read this before believing the shape:

  reclaim alone .................. -0.176R  [-0.197, -0.154]   n=16134
  reclaim + EMA200 turn .......... -0.148R  [-0.176, -0.120]   n=9597
  reclaim + volume surge ......... -0.055R  [-0.124, +0.015]   n=1620
  all three (this module) ........ -0.057R  [-0.133, +0.019]   n=1373

Two honest consequences, both of which the card repeats:

  · THE VOLUME GATE IS THE SIGNAL. It moves expectancy by +0.12R. The EMA200
    turn moves it by ~0.036R with fully overlapping intervals, and widening
    its window from 3 bars to 24 changes the result by 0.008R — a gate that
    inert is part of the SHAPE the request described, not a filter that earns
    its place. It stays because it is what was asked for and it is what makes
    the alert the thing that was seen on the chart; it is not load-bearing.
  · AFTER COSTS THIS IS BREAK-EVEN AT BEST. The interval straddles zero and
    sits slightly under it. Gross it is +0.103R; fees and slippage are all of
    it. This is why the signal goes to the OBSERVE list and ships no entry,
    stop or target.
  · THE TYPICAL SIGNAL GOES DOWN. Mean forward return is +0.47% at 6h and
    +2.05% at 48h, but the MEDIAN is -0.21% and -0.23%. More than half of
    these are lower a day later and the average is carried by a minority of
    large winners. "This coin tends to go up" is the wrong reading and it is
    the one a watchlist invites; the card prints both numbers side by side.

THE SIGNALS CLUSTER, and counting them as independent flatters every interval
above. 1373 signals landed in only 953 distinct hours — 46% share their hour
with another coin and the biggest single hour fired 27 at once, because when
the whole market turns up, every chart turns up together. One such hour is one
market event, not 27 confirmations. Scored one-observation-per-HOUR instead of
per-coin the result is:

  every firing hour ............. -0.094R  [-0.179, -0.008]   953 hours

which no longer straddles zero — it sits under it. The per-coin number is the
optimistic one and the per-hour number is the honest one.

That distinction also destroys the most tempting sub-result here. Per-coin,
signals arriving in a cluster of 5+ score +0.208R [+0.010, +0.406] on n=222,
an interval that excludes zero and looks like a rule worth trading. Those 222
coins come from 26 hours, and scored per hour the same cut reads -0.115R
[-0.473, +0.243]. The "edge" was 26 market moments counted 222 times. It is
recorded here so it does not get rediscovered and believed later.

It is not noise, though, and that distinction matters. A rotation null — the
same signal times applied to a different symbol's candles, which keeps the
coin mix and the era and destroys only the timing — reads -0.115R against the
real -0.019R, p < 0.001 over 60 trials. The bars this picks are genuinely
better than the same coins' other bars. They are better to ZERO, not to money.

Volume dose-response, monotone rather than outlier-driven (same sample):
  >=2x -0.071R (n=2431) · >=3x -0.019R (n=1283) · >=4x +0.027R (n=805) ·
  >=6x +0.094R (n=437).  Every one of those intervals still contains zero, so
  VOL_MULT is NOT set from this ladder — see the constant.

The ATR floor was checked at 0.15 / 0.30 / 0.50 / 0.67% and moved the result
by 0.054R with fully overlapping intervals, i.e. it is not a lever either. It
is set where it is because that is high enough to drop ATR artefacts — the
7 signals this scan produced on the USDC/USDT stablecoin pair, whose 0.003%
stop makes the cost model return nonsense — and low enough to keep the bar the
request was built from, DASH at 0.62%.

Anti-lookahead, same discipline as breakout_flip and zones:
  · the forming candle is dropped; strategy2_scanner patches a live price onto
    the last row and a "reclaim" confirmed on it can un-confirm itself
  · every EMA and every volume average reads bars <= i only
  · the confirming bar cannot fill its own trade — a record entered here is
    measured from the NEXT bar, which is what strategy4_outcomes.settle does
"""
import os
import time

TIMEFRAME = os.getenv("VEGAS_TIMEFRAME", "1h")

# The Vegas tunnel proper. 144/169 are the Fibonacci EMAs the indicator is
# built from; they are not tuning knobs and are not swept anywhere here.
FAST_EMA = int(os.getenv("VEGAS_FAST_EMA", "144"))
SLOW_EMA = int(os.getenv("VEGAS_SLOW_EMA", "169"))
TREND_EMA = int(os.getenv("VEGAS_TREND_EMA", "200"))

# NOT taken from the dose-response ladder above. Picking the multiple that
# maximises measured expectancy is fitting a threshold to the sample it will
# be reported on, which is how S1 got a backtest it could not reproduce twice.
# 3x is the a-priori reading of "huge volume", and the reference bar the
# request was built from — DASH 2026-08-19 22:00 — was 4.4x, comfortably clear
# of it. Every row stores its actual multiple so the live book can answer the
# question the backtest cannot.
VOL_MULT = float(os.getenv("VEGAS_VOL_MULT", "3.0"))
VOL_LOOKBACK = int(os.getenv("VEGAS_VOL_LOOKBACK", "20"))
# Louder than the entry gate, purely for display. A 5x bar and a 3x bar are
# both signals; only one of them is what the chart in the request looked like.
VOL_LOUD = float(os.getenv("VEGAS_VOL_LOUD", "5.0"))

# How recently EMA200 must have been falling for this to be a TURN rather than
# an established uptrend. Swept 3/6/12/24 with a 0.008R spread, so the value is
# a definition ("just turned"), not a fitted parameter.
FLIP_WINDOW = int(os.getenv("VEGAS_FLIP_WINDOW", "6"))

# Below this the cost model eats the whole trade, and a 0.3%-stop signal on a
# stablecoin pair is an ATR artefact rather than a setup. Same reasoning as
# S4's MIN_STOP_PCT — it is a floor on believability, not a tuning knob.
MIN_ATR_PCT = float(os.getenv("VEGAS_MIN_ATR_PCT", "0.30"))

WARMUP = TREND_EMA + 60          # EMA200 needs history before its slope means anything
COOLDOWN_SEC = float(os.getenv("VEGAS_COOLDOWN_SEC", str(6 * 3600)))

MEASURED = {
    "n": 1373, "mean_r": -0.057, "lo": -0.133, "hi": 0.019,
    "win_pct": 36.9, "gross_r": 0.103,
    # Mean vs median at the same horizon. Shipped as a pair on purpose: on
    # their own, either one is a different and equally true-sounding story.
    "fwd": [(6, 0.465, -0.214), (24, 0.842, -0.401), (48, 2.053, -0.225)],
    "per_day": 8.8,
    "span": "197 檔永續 × 4000 根 1h K（2026-03 → 2026-08）",
    "verdict": "扣掉成本後不賺錢 —— 所以只進觀察清單，不給進出場價",
    # Per-coin vs per-market-moment. The pair is the point: 46% of signals
    # share their hour with another coin, so the per-coin interval treats one
    # market turn as many independent observations.
    "hours": 953,
    "per_hour_r": -0.094, "per_hour_lo": -0.179, "per_hour_hi": -0.008,
    "cluster_trap": ("每小時只算一次的話，「5 檔以上同時觸發」從 +0.208R "
                     "變成 -0.115R —— 那個「優勢」是 26 個時刻被算成 222 次"),
    "null": "輪替對照 -0.115R，真實 -0.019R，p < 0.001（形態不是雜訊，但也不是錢）",
    "ablation": [
        ("只有回到隧道上方", -0.176, 16134),
        ("＋EMA200 翻正", -0.148, 9597),
        ("＋量能爆發", -0.055, 1620),
        ("三個條件全滿足", -0.057, 1373),
    ],
    "dose": [(2.0, -0.071, 2431), (3.0, -0.019, 1283),
             (4.0, 0.027, 805), (6.0, 0.094, 437)],
}


# ── maths ────────────────────────────────────────────────────────────────────
def ema_series(vals: list, period: int) -> list:
    """Full EMA series, seeded on the first value.

    Seeded rather than SMA-primed on purpose: every caller here discards the
    first WARMUP bars, by which point the two seedings have converged to well
    inside a tick, and a single rule is one fewer thing to disagree about with
    the chart.
    """
    k = 2.0 / (period + 1)
    out = []
    e = None
    for v in vals:
        e = float(v) if e is None else float(v) * k + e * (1 - k)
        out.append(e)
    return out


def atr_pct(ohlcv: list, period: int = 14) -> float:
    """ATR of the last `period` bars as a % of the last close."""
    if len(ohlcv) < period + 1:
        return 0.0
    trs = []
    for i in range(len(ohlcv) - period, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    close = ohlcv[-1][4]
    return (sum(trs) / len(trs)) / close * 100 if close else 0.0


def read(ohlcv: list, at: int = None) -> dict:
    """Every reading this module makes, at bar `at` (default: the last bar).

    Returned even when the bar does not fire, because "which gate said no" is
    the only thing that distinguishes a quiet market from a broken scanner —
    an ambiguity that has cost this repo three days of silence before.
    """
    n = len(ohlcv)
    i = (n - 1) if at is None else int(at)
    if n < WARMUP or i < 1 or i >= n:
        return {}
    closes = [c[4] for c in ohlcv[:i + 1]]
    fast = ema_series(closes, FAST_EMA)
    slow = ema_series(closes, SLOW_EMA)
    trend = ema_series(closes, TREND_EMA)
    tun_hi, tun_lo = max(fast[i], slow[i]), min(fast[i], slow[i])
    prev_hi = max(fast[i - 1], slow[i - 1])
    vols = [c[5] or 0.0 for c in ohlcv[:i]]
    window = vols[-VOL_LOOKBACK:] if len(vols) >= VOL_LOOKBACK else []
    vavg = (sum(window) / len(window)) if window else 0.0
    vol = ohlcv[i][5] or 0.0
    o, c = ohlcv[i][1], ohlcv[i][4]

    slope_now = trend[i] - trend[i - 1]
    lo = max(1, i - FLIP_WINDOW)
    was_falling = any(trend[j] - trend[j - 1] <= 0 for j in range(lo, i))

    return {
        "ts": int(ohlcv[i][0]),
        "close": c, "open": o,
        "tunnel_top": tun_hi, "tunnel_bottom": tun_lo, "ema200": trend[i],
        "ema_fast": fast[i], "ema_slow": slow[i],
        # None, not 0.0, when there is no volume history to compare against.
        # A missing reading written as a neutral default asserts "it did not
        # surge", which is a claim about the market made by an absent one.
        "vol_mult": (vol / vavg) if vavg > 0 else None,
        "vol": vol, "vol_avg": vavg,
        "slope": slope_now,
        "atr_pct": atr_pct(ohlcv[:i + 1]),
        # the four gates, each answerable on its own
        "reclaim": bool(c > tun_hi and ohlcv[i - 1][4] <= prev_hi),
        "turn": bool(slope_now > 0 and was_falling),
        "buyer_bar": bool(c > o),
        "surge": bool(vavg > 0 and vol >= VOL_MULT * vavg),
        # already green for a while — the thing TURN exists to exclude
        "established": bool(slope_now > 0 and not was_falling),
    }


def signal(ohlcv: list, at: int = None) -> dict:
    """The full setup, or {}. `ohlcv` must be CLOSED bars only."""
    r = read(ohlcv, at)
    if not r:
        return {}
    if not (r["reclaim"] and r["turn"] and r["buyer_bar"] and r["surge"]):
        return {}
    if r["atr_pct"] < MIN_ATR_PCT:
        return {}
    return {**r, "side": "long", "loud": bool((r["vol_mult"] or 0) >= VOL_LOUD)}


def why_not(r: dict) -> str:
    """Which gate refused, for the log and the page."""
    if not r:
        return "K 棒不足"
    if not r["reclaim"]:
        return "沒有站回隧道上方"
    if not r["turn"]:
        return "EMA200 早就是綠的" if r["established"] else "EMA200 還沒翻正"
    if not r["buyer_bar"]:
        return "收黑（不是買方）"
    if not r["surge"]:
        m = r["vol_mult"]
        return f"量能只有 {m:.1f}x" if m is not None else "沒有量能資料"
    if r["atr_pct"] < MIN_ATR_PCT:
        return f"波動太小 {r['atr_pct']:.2f}%"
    return ""


def consider(sym: str, ohlcv: list, state: dict, now: float) -> dict:
    """One symbol on 1h candles. {} unless it fired.

    The FORMING candle is dropped. On a 1h chart that bar has up to an hour
    left to move, and a reclaim confirmed on it can un-confirm itself — which
    on this signal is not a rare edge case but the normal path, since the
    reclaim is a close-versus-line comparison.
    """
    closed = ohlcv[:-1] if ohlcv else []
    if len(closed) < WARMUP:
        return {}
    last = (state.get("last") or {}).get(sym)
    # `is not None`, not truthiness: a stored timestamp of 0 is a real "it
    # fired at epoch", and `if last` silently skips the cooldown for it. Live
    # that never happens; in a replay or a test it means the guard is not
    # actually being exercised by the thing that claims to exercise it.
    if last is not None and now - last < COOLDOWN_SEC:
        return {}
    sig = signal(closed)
    if not sig:
        return {}
    state.setdefault("last", {})[sym] = now
    return {**sig, "symbol": sym, "base": sym.split("/")[0],
            "timeframe": TIMEFRAME, "fired_ts": now}
