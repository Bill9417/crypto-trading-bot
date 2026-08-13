"""
Strategy 4 — Bybit TradFi perp scanner (股票/商品永續).

Bybit lists 168 stock perps and 4 commodity perps alongside the crypto ones.
They are the only place this account can trade AAPL, NVDA, SKHYNIX or gold with
the same futures mechanics — and until now the only way to know one had set up
was to open TradingView and look. That is the whole problem this solves: the
setup is watched for you, and you are told.

WHAT IT LOOKS FOR (long only, by design — see the note at the bottom)
────────────────────────────────────────────────────────────────────
Five gates, all on closed 15m bars, evaluated cheapest-first so 172 symbols
cost 172 kline calls plus a handful of open-interest calls:

  1. LIQUIDITY   24h turnover floor. Most of this universe listed weeks ago and
                 some of it barely trades; a signal you cannot fill at a sane
                 spread is not a signal.
  2. TRIANGLE    strategy2_meter.compute_signal() == "long" — the All-in-One's
                 green triangle, already mirrored in Python with a parity test
                 against the .pine. It carries its own conditions: confluence
                 score over threshold, close above EMA200, structure bull.
  3. EMA200 UP   the trend EMA must be RISING, not merely below price. Price
                 pops above a falling 200 constantly in a downtrend, and the
                 triangle alone cannot tell those apart.
  4. SUPPORT     a confirmed swing low below price and within reach. This is
                 not decoration: it IS the stop. No support close enough, no
                 trade — because the alternative is an invented stop distance.
  5. MOMENTUM +  a recent BULLISH divergence (MACD / KD / FISHER / CVD) and an
     OI          open interest read that is not fighting the entry.

THE DIVERGENCE GATE WAS BARELY A GATE (fixed 2026-08-09)
───────────────────────────────────────────────────────
Measured on 1,064 bar-evaluations across 45 liquid Bybit symbols, 15m:

    OLD rule — divergence present on   342 bars  (32.1% of all bars)
    NEW rule — divergence present on   287 bars  (27.0%)
      of the old hits: 267 still fire, 75 are now rejected, 20 are newly found

A gate that passes a third of every bar is not selecting much. The old rule was
the textbook definition and nothing else, which let four things through:

  · NO PIVOT-AGE LIMIT. Each swing low was compared to the previous one however
    old it was; two lows 300 bars apart are not one structure. Now 5…60 bars,
    which is what TradingView's own built-in divergence indicator has always
    used.
  · RAW CUMULATIVE CVD. A running sum drifts, and while it drifts it prints a
    higher low at every pivot for free — not because flow did anything but
    because the series is cumulative. It was the single most-firing source
    (231 hits) and carried the gate ALONE on 18.7% of old hits. Detrended
    against its own EMA it halves, to 120.
  · NO OSCILLATOR-GAP FLOOR. Higher by 1e-9 counted. MACD, whose raw units make
    this worst, collapses from 152 hits to 50 once the gap must clear 5% of the
    oscillator's own range.
  · NO SWING PROMINENCE. Chop wiggles qualified as pivots; now a pivot must
    stand 0.8 ATR clear of its own neighbourhood, measured at the pivot bar.

FISHER was added as a fourth source and is genuinely new information rather
than a relabelling: 207 hits, and the SOLE source on 35 of them. Its
mathematics is not a variation on comparing two averages — it maps price onto a
near-Gaussian distribution, where extremes are by construction rare.

A/D WAS DELIBERATELY NOT ADDED. Its money-flow multiplier is the same algebra
as the close-position estimate cvd_series() already uses, and with no intrabar
data here they would be the identical series. See divergence_scan().

The net count barely moved (−16%) because Fisher ADDS while the filters REMOVE.
That headline understates the change: what is behind each signal is different,
and 63% of surviving hits now have two or more sources agreeing versus a rule
where any single one passed. If you want the gate genuinely tight, the knob is
S4_MIN_DIV_SOURCES=2 — measured at 181 hits, 17.0% of bars, about half the old
firing rate. It is left at 1 ON PURPOSE: changing the rule and the threshold in
the same step would make the forward sample unattributable to either.

NONE OF THIS IS EVIDENCE OF PROFIT. It is evidence the gate now measures what
it claims to. The n=50 result below described the OLD engine and no longer
applies to this one; the counter starts again.

WHAT IT DOES, MEASURED (2026-08-06, 51 liquid symbols, 271 symbol-days of 15m)
─────────────────────────────────────────────────────────────────────────────
    signals          50
    win rate         44.0%   (22/50)
    expectancy       +0.052 R  ± 0.165 (1 s.e.)
    95% CI           [−0.272, +0.376] R
    firing rate      ~11 alerts/day across a 60-symbol universe
                     (the OI gate was NOT applied in this replay — deep OI
                     history is not available — so live will fire fewer)

Read that interval, not the headline. It straddles zero with room to spare:
n=50 cannot distinguish +0.05R from −0.27R, and separating an edge that small
from noise needs something like n=1000. This is NOT a positive result. It is
the absence of a result, which is the honest state of a scan whose universe is
weeks old.

The gate breakdown is worth keeping, because it says where the work happens:
of 25,992 bars examined, 25,686 died at the triangle. Of the ~306 triangles
that survived, 112 failed the divergence gate, 93 the EMA200 slope, 51 the stop
distance — the four extra gates cut the triangle's output by ~84%. They are
doing something; whether that something is profitable is exactly what is
unmeasured.

Do NOT tune these thresholds on those 50 trades. Picking the best-looking
variant out of a handful on a sample this size is the definition of the
overfitting that has already cost this repo an S1 walk-forward twice and a
previous S4. The knobs are env vars so they can be changed deliberately, not
so they can be searched.

WHAT THIS IS NOT
────────────────
This has NOT been shown to make money. Half this universe listed inside the
last 30 days; the median symbol has 33 days of history and the oldest has 149.
There is no walk-forward here, no out-of-sample fold, no rotation null — the
data to do any of that does not exist yet.

That matters more than usual in this repo, which has measured, on real candles:
48 mean-reversion configs at 55–78% win rate of which 46 lost money; S1 failing
walk-forward twice; every S2 exit rule negative; and a previous "S4" (low-vol +
oversold) dying on a single −1.87R fold. A setup that looks right on a chart is
the beginning of a question, not the end of one.

So this ships as an ALERT, never an order. It tells you a setup you described
has appeared, and it says so in the message. When these symbols have a year of
history behind them, the honest next step is to run it through walk_forward.py
and find out — the scan records every signal it fires so that test has data to
work with when the time comes.

BOTH SIDES SINCE 2026-08-11 — AND THE SHORT SIDE IS UNMEASURED
──────────────────────────────────────────────────────────────
This shipped long-only, with this note explaining why: "the mirror image for
shorts is not simply the inverse — a stock perp's borrow, its funding and its
overnight gap behave differently on the short side — so rather than ship a
symmetric rule nobody checked, this side is left out until someone measures it."

Shorts were then requested, so they are here, and that reasoning has NOT been
answered — it has been overridden. Every gate is mirrored exactly: short
triangle, EMA200 FALLING, confirmed swing high above price as the stop, bearish
divergence (price higher high / oscillator lower high, same four quality
filters), and OI showing shorts opening or longs being flushed. What is missing
is any evidence the mirror is VALID on this universe. The long side has an
inconclusive n=50 behind it; the short side has nothing at all.

S4_ENABLE_SHORT=false turns it off without touching the long side.

WHICH INDICATOR THIS TRACKS (changed 2026-08-11)
───────────────────────────────────────────────
The triangle gate reads strategy2_meter, which now mirrors
All-in-One_ULTIMATE_Pro.pine rather than All-in-One_ULTIMATE.pine. The only
scoring-relevant difference is the outer Double-Tunnel pair, 288/338 → 576/676
(Sykes' own 4x periods). That is a real change to a 10-weight factor: the
tunnel now asks a slower question, so symbols that sat outside the old tunnel
can sit inside the new one and score differently. It also raised the meter's
history requirement to ~690 bars, which is why S4_CANDLES went 450 → 750.

Both of the above reset the measurement counter AGAIN, on top of the 08-09
divergence rewrite. The n=50 below describes an engine that no longer exists in
two further ways. Treat the record as starting from zero.
"""
import json
import math
import os
import time
from datetime import datetime, timezone

TZ_NAME = os.getenv("TZ_DISPLAY", "Asia/Taipei")
STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy4_state.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy4_signals.json")

ENABLED = os.getenv("S4_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
TIMEFRAME = os.getenv("S4_TIMEFRAME", "15m")
# 750, not the old 450: All-in-One_ULTIMATE_Pro moved the outer tunnel to
# EMA676, so strategy2_meter.MIN_CANDLES rose to ~690. At 450 the tunnel factor
# would abstain on every bar and the triangle gate would quietly be scoring on
# 90 of its 100 points — the same silent-degradation failure as the 2026-07-25
# weight drift, just arriving through a period change instead.
CANDLES = int(os.getenv("S4_CANDLES", "750"))
# Both sides, independently switchable. Long has the (inconclusive) n=50 behind
# it; short has NOTHING measured — see the LONG/SHORT note in the docstring.
ENABLE_LONG = os.getenv("S4_ENABLE_LONG", "true").strip().lower() in ("1", "true", "yes", "on")
ENABLE_SHORT = os.getenv("S4_ENABLE_SHORT", "true").strip().lower() in ("1", "true", "yes", "on")
# Two pools, ranked by 24h turnover and cut separately. One combined ranking
# would be no ranking at all: BTC alone turns over 3.8 BILLION a day against
# ~50M for a busy stock perp, so a single top-N list would be crypto only and
# the TradFi side would silently vanish from the scan.
MAX_TRADFI = int(os.getenv("S4_MAX_TRADFI", "60"))
MAX_CRYPTO = int(os.getenv("S4_MAX_CRYPTO", "100"))
MIN_TURNOVER = float(os.getenv("S4_MIN_TURNOVER_USDT", "2000000"))
MIN_TURNOVER_CRYPTO = float(os.getenv("S4_MIN_TURNOVER_CRYPTO_USDT", "2000000"))
SCAN_TRADFI = os.getenv("S4_SCAN_TRADFI", "true").strip().lower() in ("1", "true", "yes")
SCAN_CRYPTO = os.getenv("S4_SCAN_CRYPTO", "true").strip().lower() in ("1", "true", "yes")
EMA_SLOPE_BARS = int(os.getenv("S4_EMA_SLOPE_BARS", "20"))
DIV_LOOKBACK = int(os.getenv("S4_DIV_LOOKBACK", "20"))    # bars since a divergence
DIV_PIVOT = int(os.getenv("S4_DIV_PIVOT", "5"))
# ── divergence quality, ported from Divergence_Radar_PRO.pine ────────────────
# Every one of these closes a hole the old rule left open. See the DIVERGENCE
# note in the module docstring for what each was letting through.
DIV_MIN_GAP = int(os.getenv("S4_DIV_MIN_GAP", "5"))       # bars between the two pivots
DIV_MAX_GAP = int(os.getenv("S4_DIV_MAX_GAP", "60"))      # ...and the ceiling
DIV_MIN_OSC_GAP = float(os.getenv("S4_DIV_MIN_OSC_GAP", "0.05"))   # × osc's own range
DIV_MIN_LEG_ATR = float(os.getenv("S4_DIV_MIN_LEG_ATR", "0.8"))    # swing prominence
DIV_NORM = int(os.getenv("S4_DIV_NORM", "200"))           # osc range window
MIN_DIV_SOURCES = int(os.getenv("S4_MIN_DIV_SOURCES", "1"))
FISHER_LEN = int(os.getenv("S4_FISHER_LEN", "9"))
FLOW_DETREND = int(os.getenv("S4_FLOW_DETREND", "200"))   # CVD baseline EMA
ATR_LEN = int(os.getenv("S4_ATR_LEN", "14"))
MAX_STOP_PCT = float(os.getenv("S4_MAX_STOP_PCT", "4")) / 100
MIN_STOP_PCT = float(os.getenv("S4_MIN_STOP_PCT", "0.4")) / 100
STOP_BUFFER = float(os.getenv("S4_STOP_BUFFER_PCT", "0.15")) / 100
TP_R = float(os.getenv("S4_TP_R", "2"))
COOLDOWN_SEC = float(os.getenv("S4_COOLDOWN_SEC", str(6 * 3600)))
REQUIRE_DIVERGENCE = os.getenv("S4_REQUIRE_DIVERGENCE", "true").strip().lower() in ("1", "true", "yes")
REQUIRE_OI = os.getenv("S4_REQUIRE_OI", "true").strip().lower() in ("1", "true", "yes")
UNIVERSE_TTL_SEC = 6 * 3600
PACE_SEC = float(os.getenv("S4_PACE_SEC", "0.12"))

# OI read, same four states as Divergence_Radar.pine
OI_LONGS_OPENING = 1
OI_SHORTS_OPENING = 2
OI_SHORTS_CLOSING = 3
OI_LONGS_CLOSING = 4
OI_TEXT = {0: "no data", 1: "開多 longs opening", 2: "開空 shorts opening",
           3: "空單回補 shorts closing", 4: "多單平倉 longs closing"}
# For a LONG: new longs entering, or shorts being squeezed out. The two states
# that mean the opposite side is being built are what disqualifies.
OI_OK_FOR_LONG = (OI_LONGS_OPENING, OI_SHORTS_CLOSING)
# The exact mirror for a SHORT: new shorts entering, or longs being flushed.
OI_OK_FOR_SHORT = (OI_SHORTS_OPENING, OI_LONGS_CLOSING)
OI_OK = {"long": OI_OK_FOR_LONG, "short": OI_OK_FOR_SHORT}


# ── state ────────────────────────────────────────────────────────────────────
def _load(path=None) -> dict:
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh
        return {}


def _save(state: dict, path=None) -> None:
    p = path or STATE_FILE
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, p)


# ── pure helpers (unit-tested; no network) ───────────────────────────────────
def ema(values, period):
    """Standard EMA, seeded with the first value."""
    if not values or period < 1:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def ema_rising(closes, period=200, bars=EMA_SLOPE_BARS) -> tuple:
    """(is_rising, slope_pct). Slope is measured over `bars`, in percent of the
    EMA's own level, so it is comparable across a 300 USDT and a 3 USDT perp."""
    if len(closes) < period + bars + 1:
        return False, None
    e = ema(closes, period)
    now, then = e[-1], e[-1 - bars]
    if not then:
        return False, None
    slope = (now - then) / abs(then) * 100
    return slope > 0, slope


def _pivots(highs, lows, left, right):
    """Confirmed pivot indices. A pivot at i is only known at i+right — the same
    lag the chart markers carry, and the reason a live scan can never see the
    most recent `right` bars' pivots."""
    ph, plw = [], []
    for i in range(left, len(highs) - right):
        win_h = highs[i - left:i + right + 1]
        win_l = lows[i - left:i + right + 1]
        if highs[i] == max(win_h) and win_h.count(highs[i]) == 1:
            ph.append(i)
        if lows[i] == min(win_l) and win_l.count(lows[i]) == 1:
            plw.append(i)
    return ph, plw


def ema_trending(closes, side, period=200, bars=EMA_SLOPE_BARS) -> tuple:
    """(ok, slope_pct) — the trend EMA must slope WITH the trade, not merely sit
    on the right side of price. Price pops above a falling 200 constantly in a
    downtrend and below a rising one in an uptrend; the triangle alone cannot
    tell those apart, which is the whole reason this gate exists."""
    _, slope = ema_rising(closes, period, bars)
    if slope is None:
        return False, None
    return (slope > 0 if side == "long" else slope < 0), slope


def support_below(highs, lows, price, left=3, right=3):
    """Most recent CONFIRMED swing low below price. The stop goes here, so an
    invented level is worse than no signal: return None and let the gate fail."""
    _, plw = _pivots(highs, lows, left, right)
    for i in reversed(plw):
        if lows[i] < price:
            return {"level": lows[i], "bars_ago": len(lows) - 1 - i}
    return None


def resistance_above(highs, lows, price, left=3, right=3):
    """Mirror of support_below for shorts: most recent CONFIRMED swing high
    ABOVE price. Same rule — this IS the stop, so no level means no signal
    rather than a stop distance nobody measured."""
    ph, _ = _pivots(highs, lows, left, right)
    for i in reversed(ph):
        if highs[i] > price:
            return {"level": highs[i], "bars_ago": len(highs) - 1 - i}
    return None


def structure_level(highs, lows, price, side, left=3, right=3):
    """The level that qualifies the setup AND carries the stop, either side."""
    return (support_below(highs, lows, price, left, right) if side == "long"
            else resistance_above(highs, lows, price, left, right))


def macd_series(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return [], []
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es, strict=False)]
    return line, ema(line, signal)


def stoch_k(highs, lows, closes, length=9, k_smooth=1):
    """RSV, then the Chinese KDJ's 2/3·1/3 recursion — the same K the
    Divergence Radar draws, so the two cannot disagree about a divergence."""
    if len(closes) < length + 1:
        return []
    rsv = []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - length + 1):i + 1])
        hi = max(highs[max(0, i - length + 1):i + 1])
        rsv.append(50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100)
    k = []
    prev = 50.0
    for v in rsv:
        prev = (2.0 / 3.0) * prev + (1.0 / 3.0) * v
        k.append(prev)
    return k


def cvd_series(ohlcv):
    """Cumulative volume delta ESTIMATED from where each bar closed in its own
    range. The chart measures this from 1-minute intrabars; that data is not
    available here, so this is the same fallback the .pine uses beyond its
    intrabar history limit — an estimate, and labelled as one wherever it is
    reported."""
    out, run = [], 0.0
    for c in ohlcv:
        _, _, h, lo, cl, v = c[0], c[1], c[2], c[3], c[4], c[5]
        run += 0.0 if h == lo else float(v) * (2 * (cl - lo) / (h - lo) - 1)
        out.append(run)
    return out


def atr_series(highs, lows, closes, period=ATR_LEN):
    """True-range EMA. Used to ask whether a pivot is a real swing or a wiggle,
    measured AT THE PIVOT BAR — the volatility at the event, not at the moment
    the scan happens to run."""
    if not closes:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return ema(trs, period)


def fisher_series(highs, lows, length=FISHER_LEN):
    """Ehlers' Fisher Transform of the normalised median price.

    Normalise hl2 into −0.5…+0.5 over `length`, then push it through
    0.5·ln((1+x)/(1−x)). That maps a roughly uniform input onto a roughly
    Gaussian one, and the point of a Gaussian is that extremes are RARE — so a
    Fisher extreme is a sharper statement than a KD extreme, which a ranging
    market prints all day. It is the one source here whose mathematics is not a
    variation on "compare two moving averages".

    The 0.999 clamp is load-bearing, not cosmetic: at x = ±1 the log term is
    infinite, and one bar of hl2 sitting exactly on the window extreme reaches
    it. Both recursions start at 0 so the opening bars are neutral rather than
    dragging a warm-up artefact into the divergence tracker.
    """
    n = min(len(highs), len(lows))
    if n < length + 1:
        return []
    mp = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    out, val, fish = [], 0.0, 0.0
    for i in range(n):
        w = mp[max(0, i - length + 1):i + 1]
        hh, ll = max(w), min(w)
        rng = hh - ll
        if rng > 0:
            val = 0.66 * ((mp[i] - ll) / rng - 0.5) + 0.67 * val
        val = max(-0.999, min(0.999, val))
        fish = 0.5 * math.log((1 + val) / (1 - val)) + 0.5 * fish
        out.append(fish)
    return out


def detrend(series, period=FLOW_DETREND):
    """Subtract a series' own EMA.

    Only meaningful for the CUMULATIVE ones. A running sum's absolute level is
    an artefact of where the candle history happens to start, and while it
    drifts one way every price extreme on that side clears the divergence test
    for free — the oscillator "made a higher low" because it makes a higher low
    every bar, not because flow did anything. Subtracting the baseline leaves
    only flow relative to the prevailing trend, and the arbitrary anchor
    cancels out of the comparison.
    """
    if not series or len(series) < 2:
        return list(series)
    base = ema(series, period)
    return [v - b for v, b in zip(series, base, strict=False)]


def bullish_divergence(highs, lows, osc, pivot=DIV_PIVOT, lookback=DIV_LOOKBACK,
                       min_gap=DIV_MIN_GAP, max_gap=DIV_MAX_GAP,
                       min_osc_gap=DIV_MIN_OSC_GAP, atr=None,
                       min_leg_atr=DIV_MIN_LEG_ATR, norm=DIV_NORM):
    """Regular bullish divergence: price prints a LOWER low while the
    oscillator prints a HIGHER low. Same rule and the same confirmation lag as
    f_divTrackPro() in Divergence_Radar_PRO.pine — if these two ever disagree
    about a bar, one of them is wrong, and this repo has already been bitten by
    a Python mirror drifting from the .pine it claimed to reproduce.

    The bare definition above is what the first version implemented, and it is
    too permissive to be worth gating on. Three things are added here, each
    closing a hole that was letting through signals about nothing:

      min_gap / max_gap  The two pivots must be this far apart IN BARS. The old
          rule compared each pivot to the previous one however old it was, so a
          swing low from 300 bars ago could still "diverge" against today's.
          Two lows that far apart are not one structure. 5…60 is what
          TradingView's own built-in divergence indicator has always used.

      min_osc_gap  The oscillator must have risen by a real amount, expressed
          as a fraction of its OWN recent range. Normalising per-source is what
          lets one number mean the same thing to MACD (price units), K (0-100)
          and detrended CVD (volume units). Without it, higher by 1e-9 counted.

      min_leg_atr  The pivot must be a real swing: its low must sit this many
          ATR below the highest high in its own confirmation window. This is
          what rejects chop, and it is measured with the ATR AT THE PIVOT, not
          at scan time.

    `atr=None` skips the prominence test — callers without an ATR series get
    the other two rather than a fabricated one.

    Returns bars-ago of the most recent qualifying divergence inside
    `lookback`, else None.
    """
    n = min(len(lows), len(osc))
    if n < pivot * 2 + 2:
        return None
    _, plw = _pivots(highs[:n], lows[:n], pivot, pivot)

    # The oscillator's own recent range, so `min_osc_gap` is scale-free.
    win = osc[max(0, n - norm):n]
    osc_rng = (max(win) - min(win)) if win else 0.0

    best = None
    for a, b in zip(plw, plw[1:], strict=False):
        if not (min_gap <= b - a <= max_gap):
            continue
        if lows[b] >= lows[a] or osc[b] <= osc[a]:
            continue
        if osc_rng > 0 and (osc[b] - osc[a]) / osc_rng < min_osc_gap:
            continue
        if atr and min_leg_atr > 0:
            a_pv = atr[b] if b < len(atr) else atr[-1]
            if a_pv and a_pv > 0:
                w0, w1 = max(0, b - pivot), min(n, b + pivot + 1)
                if (max(highs[w0:w1]) - lows[b]) / a_pv < min_leg_atr:
                    continue
        confirmed = b + pivot                  # when the chart would show it
        ago = n - 1 - confirmed
        if 0 <= ago <= lookback:
            best = ago if best is None else min(best, ago)
    return best


def bearish_divergence(highs, lows, osc, pivot=DIV_PIVOT, lookback=DIV_LOOKBACK,
                       min_gap=DIV_MIN_GAP, max_gap=DIV_MAX_GAP,
                       min_osc_gap=DIV_MIN_OSC_GAP, atr=None,
                       min_leg_atr=DIV_MIN_LEG_ATR, norm=DIV_NORM):
    """Regular BEARISH divergence: price prints a HIGHER high while the
    oscillator prints a LOWER high. The exact mirror of bullish_divergence,
    including all four quality filters added on 2026-08-09 — pivot-age window,
    scale-free oscillator gap, swing prominence, and the same confirmation lag.

    Written out rather than folded into the bullish function with a sign flag:
    the two walk DIFFERENT pivot lists (highs vs lows) and measure prominence
    against opposite extremes, so a shared body would be a chain of `if side`
    branches with no line doing the same work for both.
    """
    n = min(len(highs), len(osc))
    if n < pivot * 2 + 2:
        return None
    ph, _ = _pivots(highs[:n], lows[:n], pivot, pivot)

    win = osc[max(0, n - norm):n]
    osc_rng = (max(win) - min(win)) if win else 0.0

    best = None
    for a, b in zip(ph, ph[1:], strict=False):
        if not (min_gap <= b - a <= max_gap):
            continue
        # price higher high, oscillator lower high — the divergence itself
        if highs[b] <= highs[a] or osc[b] >= osc[a]:
            continue
        if osc_rng > 0 and (osc[a] - osc[b]) / osc_rng < min_osc_gap:
            continue
        if atr and min_leg_atr > 0:
            a_pv = atr[b] if b < len(atr) else atr[-1]
            if a_pv and a_pv > 0:
                w0, w1 = max(0, b - pivot), min(n, b + pivot + 1)
                # prominence measured DOWN from the pivot high, mirroring the
                # bullish case's measurement up from the pivot low
                if (highs[b] - min(lows[w0:w1])) / a_pv < min_leg_atr:
                    continue
        confirmed = b + pivot
        ago = n - 1 - confirmed
        if 0 <= ago <= lookback:
            best = ago if best is None else min(best, ago)
    return best


def divergence_scan(ohlcv, highs, lows, closes, side="long"):
    """Every source, measured. Returns {name: bars_ago} for those that fired.

    FOUR sources, not five. Accumulation/Distribution is deliberately absent,
    and that is a finding rather than an omission: A/D's money-flow multiplier
    and the close-position estimate cvd_series() uses are the same algebra —

        2·(c−l)/(h−l) − 1  =  (2c − 2l − h + l)/(h−l)  =  (2c − l − h)/(h−l)
        ((c−l) − (h−c))/(h−l)                          =  (2c − l − h)/(h−l)

    On the chart that collision only bites beyond TradingView's intrabar limit,
    because CVD there is built from real 1-minute deltas and only falls back to
    the estimate on older bars. Here there is no intrabar data at all — the
    REST klines are all this scan gets — so CVD is the estimate on EVERY bar
    and A/D would be the identical series under a second name. Adding it would
    inflate the agreement count without adding a measurement, which is the one
    thing a confluence count must never do.
    """
    atr = atr_series(highs, lows, closes)
    macd_line, _ = macd_series(closes)
    sources = (("MACD", macd_line),
               ("KD", stoch_k(highs, lows, closes)),
               ("FISH", fisher_series(highs, lows)),
               ("CVD", detrend(cvd_series(ohlcv))))
    detect = bullish_divergence if side == "long" else bearish_divergence
    hits = {}
    for name, s in sources:
        if not s:
            continue
        ago = detect(highs, lows, s, atr=atr)
        if ago is not None:
            hits[name] = ago
    return hits


def oi_state(oi_values, closes, smooth=3):
    """The four-state OI read. A missing series returns 0 ('no data') rather
    than a fabricated flat reading — asserting open interest did not move is a
    measurement nobody made."""
    if not oi_values or len(oi_values) < smooth + 2 or len(closes) < smooth + 2:
        return 0, None
    deltas = []
    for prev, cur in zip(oi_values, oi_values[1:], strict=False):
        deltas.append(0.0 if not prev else (cur - prev) / prev * 100)
    if not deltas:
        return 0, None
    oi_d = ema(deltas, smooth)[-1]
    px_d = ema([b - a for a, b in zip(closes, closes[1:], strict=False)], smooth)[-1]
    if oi_d > 0:
        return (OI_LONGS_OPENING if px_d >= 0 else OI_SHORTS_OPENING), oi_d
    if oi_d < 0:
        return (OI_SHORTS_CLOSING if px_d >= 0 else OI_LONGS_CLOSING), oi_d
    return 0, oi_d


def plan(entry, level, side="long"):
    """Stop beyond the structure level that qualified the setup, target at
    TP_R × risk. Returns None when the resulting stop is absurd in either
    direction — too tight to survive noise, or so wide the 2R target needs a
    move the symbol will not make.

    The buffer pushes the stop AWAY from the trade on both sides: below support
    for a long, above resistance for a short. Getting that sign wrong would put
    the stop inside the level it is meant to sit behind."""
    if not entry or not level:
        return None
    if side == "long":
        if level >= entry:
            return None
        sl = level * (1 - STOP_BUFFER)
        risk = entry - sl
        tp = entry + risk * TP_R
    else:
        if level <= entry:
            return None
        sl = level * (1 + STOP_BUFFER)
        risk = sl - entry
        tp = entry - risk * TP_R
    if risk <= 0:
        return None
    stop_pct = risk / entry
    if stop_pct < MIN_STOP_PCT or stop_pct > MAX_STOP_PCT:
        return None
    return {"entry": entry, "sl": sl, "tp": tp, "side": side,
            "stop_pct": stop_pct * 100, "tp_pct": risk * TP_R / entry * 100,
            "rr": TP_R}


QUALITY_WEIGHTS = {"div": 35, "meter": 25, "slope": 15, "oi": 15, "stop": 10}


def quality(ev: dict) -> int:
    """0-100 for a setup that has already passed every gate.

    Passing is binary; this says by how much. Six symbols can all clear the
    same five gates on the same bar and they are not the same trade.

    THE ABSTAIN RULE, which this repo has now been bitten by twice: a component
    with no reading is REMOVED FROM THE DENOMINATOR, never scored as a neutral
    middle. Scoring an absent OI feed 50 would be indistinguishable from a
    genuinely balanced one, and 0 would collapse the reading — neither is a
    thing that was measured. The returned number is therefore a percentage of
    whatever could actually be read, and `quality_basis` says how much that was.

    None of these weights is fitted. They are priorities, stated openly, on a
    strategy whose edge is unmeasured — see the header. A higher number means
    more of the things this scan looks for lined up, not a better trade.
    """
    parts, live = 0.0, 0
    # Every directional component below is read FROM THE TRADE'S POINT OF VIEW.
    # Left as-is, a short would have scored its meter, slope and OI on the long
    # scale — a perfect short (score 0, EMA falling hard, shorts opening) would
    # have graded 0/100 while the setup it describes is the strongest the scan
    # can produce.
    side = ev.get("side", "long")

    srcs = len(ev.get("div_sources") or [])
    if srcs or not REQUIRE_DIVERGENCE:
        # 4 sources available; the jump from one to two is the meaningful one.
        parts += {0: 0, 1: 10, 2: 21, 3: 30}.get(srcs, 35) / 35 * QUALITY_WEIGHTS["div"]
        live += QUALITY_WEIGHTS["div"]

    score = ev.get("score")
    if score is not None:
        # the meter only ever qualifies a trade beyond ~50 in its own direction,
        # so 50→100 (long) or 50→0 (short) is the range that carries information
        conv = (score - 50) / 50.0 if side == "long" else (50 - score) / 50.0
        parts += max(0.0, min(1.0, conv)) * QUALITY_WEIGHTS["meter"]
        live += QUALITY_WEIGHTS["meter"]

    slope = ev.get("slope")
    if slope is not None:
        # 0…2% of EMA level over the slope window; beyond that it is already
        # a trend and more does not add information. A short wants it negative.
        mag = slope if side == "long" else -slope
        parts += max(0.0, min(1.0, mag / 2.0)) * QUALITY_WEIGHTS["slope"]
        live += QUALITY_WEIGHTS["slope"]

    st = ev.get("oi_state") or 0
    if st:                                   # 0 is "no data" — abstain, not neutral
        # Full credit when the trade's own side is being OPENED, partial when
        # the opposite side is merely closing out.
        if side == "long":
            oi_frac = 1.0 if st == OI_LONGS_OPENING else 0.6 if st == OI_SHORTS_CLOSING else 0.0
        else:
            oi_frac = 1.0 if st == OI_SHORTS_OPENING else 0.6 if st == OI_LONGS_CLOSING else 0.0
        parts += oi_frac * QUALITY_WEIGHTS["oi"]
        live += QUALITY_WEIGHTS["oi"]

    p = ev.get("plan") or {}
    sp = p.get("stop_pct")
    if sp is not None and MAX_STOP_PCT > MIN_STOP_PCT:
        # a tighter stop inside the allowed band is more R per unit of risk
        frac = (sp / 100 - MIN_STOP_PCT) / (MAX_STOP_PCT - MIN_STOP_PCT)
        parts += (1.0 - max(0.0, min(1.0, frac))) * QUALITY_WEIGHTS["stop"]
        live += QUALITY_WEIGHTS["stop"]

    ev["quality_basis"] = live
    return int(round(parts / live * 100)) if live else 0


def evaluate(ohlcv, oi_values=None, side="long") -> dict:
    """All gates on one symbol's candles, for ONE side. Pure — every failure is
    NAMED, so the scan can report what it rejected instead of only what it
    passed. A filter you cannot see the effect of is a filter you cannot tune.

    `support` keeps its name on both sides for the sake of every existing
    caller, the stored signal history and the /s4 template; on a short it holds
    the resistance above price. The stop sits beyond it either way — the field
    is 'the level that qualified this setup and carries the stop'."""
    out = {"pass": False, "reason": None, "score": None, "slope": None,
           "support": None, "div_ago": None, "div_sources": [], "oi_state": 0,
           "oi_delta": None, "plan": None, "quality": None, "quality_basis": None,
           "side": side}
    # The meter needs its full window or the tunnel factor abstains silently;
    # derive the floor from the meter rather than restating it as a literal.
    import strategy2_meter
    need = strategy2_meter.SIGNAL_MIN_CANDLES + 20
    if not ohlcv or len(ohlcv) < need:
        out["reason"] = "not enough history"
        return out

    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]
    price = closes[-1]

    sig = strategy2_meter.compute_signal(ohlcv)
    out["score"] = sig.get("score")
    if sig.get("signal") != side:
        out["reason"] = f"no {side} triangle"
        return out

    trending, slope = ema_trending(closes, side)
    out["slope"] = slope
    if not trending:
        out["reason"] = "EMA200 not rising" if side == "long" else "EMA200 not falling"
        return out

    lvl = structure_level(highs, lows, price, side)
    out["support"] = lvl
    if not lvl:
        out["reason"] = "no support below" if side == "long" else "no resistance above"
        return out

    p = plan(price, lvl["level"], side)
    out["plan"] = p
    if not p:
        out["reason"] = "stop distance out of range"
        return out

    if REQUIRE_DIVERGENCE:
        hits = divergence_scan(ohlcv, highs, lows, closes, side)
        out["div_sources"] = sorted(hits)
        out["div_ago"] = min(hits.values()) if hits else None
        if len(hits) < MIN_DIV_SOURCES:
            word = "bullish" if side == "long" else "bearish"
            out["reason"] = (f"no recent {word} divergence" if not hits
                             else f"only {len(hits)} of {MIN_DIV_SOURCES} divergence sources")
            return out

    st, delta = oi_state(oi_values or [], closes)
    out["oi_state"], out["oi_delta"] = st, delta
    if REQUIRE_OI and st not in OI_OK[side]:
        out["reason"] = "OI not supportive" if st else "no OI data"
        return out

    out["quality"] = quality(out)
    out["pass"] = True
    return out


def enabled_sides() -> tuple:
    """Which directions this scan is currently allowed to fire."""
    return tuple(s for s, on in (("long", ENABLE_LONG), ("short", ENABLE_SHORT)) if on)


def evaluate_sides(ohlcv, oi_values=None, sides=None) -> dict:
    """Best passing side for one symbol, or the more informative rejection.

    A symbol cannot be both at once — compute_signal fires one triangle per bar
    — so the first side to pass wins and the other is not even evaluated. When
    neither passes, the rejection reported is the one that got FURTHEST through
    the gates, because "stop distance out of range" says something about this
    symbol while "no short triangle" says only that it was not a short."""
    order = ("no long triangle", "no short triangle", "EMA200 not rising",
             "EMA200 not falling", "no support below", "no resistance above",
             "stop distance out of range")

    def depth(reason):
        try:
            return order.index(reason or "")
        except ValueError:
            return len(order)          # divergence/OI failures are the deepest

    best = None
    for side in (sides if sides is not None else enabled_sides()):
        res = evaluate(ohlcv, oi_values, side)
        if res["pass"]:
            return res
        if best is None or depth(res["reason"]) > depth(best["reason"]):
            best = res
    return best or {"pass": False, "reason": "no side enabled", "side": None}


# ── network ──────────────────────────────────────────────────────────────────
def _client():
    import ccxt
    return ccxt.bybit({"enableRateLimit": True,
                       "options": {"defaultType": "linear"}})


def universe(client=None, state=None) -> dict:
    """{symbol: segment} — 'tradfi' for Bybit's stock/commodity perps, 'crypto'
    for everything else it settles in USDT.

    The segment comes from Bybit's own symbolType tag, never the ticker text.
    Guessing from the name would be a coin flip: this venue lists AMD the token
    and AMDSTOCK the equity side by side, and name-based mapping is exactly how
    a mirror once sized an order 344× off."""
    st = state if state is not None else _load()
    cached = st.get("universe")
    # A cache written by the previous version is a LIST of tradfi symbols. It
    # is not merely the wrong type — it is the wrong ANSWER, and serving it for
    # the next six hours would have quietly scanned no crypto at all while
    # reporting success. Shape mismatch invalidates the cache, it does not get
    # coerced.
    if isinstance(cached, dict) and cached \
            and time.time() - float(st.get("universe_ts") or 0) < UNIVERSE_TTL_SEC:
        return cached
    ex = client or _client()
    markets = ex.load_markets()
    uni = {}
    for m in markets.values():
        if not (m.get("linear") and m.get("swap") and m.get("active")):
            continue
        if m.get("quote") != "USDT":
            continue          # the USDC twins would double every base
        stype = (m.get("info") or {}).get("symbolType")
        if stype in ("stock", "commodity"):
            uni[m["symbol"]] = "tradfi"
        elif SCAN_CRYPTO and stype in ("", None, "innovation"):
            uni[m["symbol"]] = "crypto"
    if not SCAN_TRADFI:
        uni = {s: g for s, g in uni.items() if g != "tradfi"}
    if state is not None:
        state["universe"] = uni
        state["universe_ts"] = time.time()
    return uni


CAPS = {"tradfi": (MAX_TRADFI, MIN_TURNOVER), "crypto": (MAX_CRYPTO, MIN_TURNOVER_CRYPTO)}


def select(client, uni) -> list:
    """[(symbol, segment)] — each pool ranked by 24h turnover and cut to its own
    cap. One fetch_tickers call ranks everything; 600 individual ticker calls to
    learn the same thing is the rate-limit incident this repo already had."""
    if isinstance(uni, (list, tuple)):        # tolerate an old cached shape
        uni = {s: "tradfi" for s in uni}
    syms = list(uni)
    try:
        tk = client.fetch_tickers(syms)
    except Exception:  # noqa: BLE001 — no ranking is better than no scan
        tk = {}
    pools = {}
    for s in syms:
        seg = uni[s]
        turnover = float((tk.get(s) or {}).get("quoteVolume") or 0)
        _, floor = CAPS.get(seg, (0, 0))
        if tk and turnover < floor:
            continue
        pools.setdefault(seg, []).append((s, turnover))
    out = []
    for seg, rows in pools.items():
        rows.sort(key=lambda r: -r[1])
        cap = CAPS.get(seg, (MAX_TRADFI, 0))[0]
        out += [(s, seg) for s, _ in rows[:cap]]
    return out


def _oi_history(client, symbol, limit=60):
    try:
        rows = client.fetch_open_interest_history(symbol, TIMEFRAME, limit=limit)
        return [float(r.get("openInterestAmount") or 0) for r in rows]
    except Exception:  # noqa: BLE001 — OI is one gate, not the scan
        return []


def scan(client=None, limit=None) -> dict:
    """Full sweep. Returns {signals, checked, rejected, ts}. Never raises."""
    ex = client or _client()
    state = _load()
    uni = universe(ex, state)
    _save(state)
    picks = select(ex, uni)
    if limit:
        picks = picks[:limit]

    signals, rejected, checked = [], {}, {"tradfi": 0, "crypto": 0}
    for sym, seg in picks:
        try:
            ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=CANDLES)
        except Exception:  # noqa: BLE001 — one bad symbol never stops the scan
            continue
        checked[seg] = checked.get(seg, 0) + 1
        res = evaluate_sides(ohlcv)
        # OI costs a call, so it is only asked for once everything cheaper has
        # already passed. Re-run with the data now in hand — and only for the
        # side that got that far, so adding shorts did not double the OI calls.
        if res["reason"] in ("no OI data", "OI not supportive") or res["pass"]:
            oi = _oi_history(ex, sym)
            res = evaluate_sides(ohlcv, oi, sides=(res.get("side"),) if res.get("side") else None)
        if res["pass"]:
            signals.append({"symbol": sym, "base": sym.split("/")[0], "segment": seg,
                            **res, "price": ohlcv[-1][4], "bar_ts": ohlcv[-1][0]})
        else:
            rejected[res["reason"] or "?"] = rejected.get(res["reason"] or "?", 0) + 1
        time.sleep(PACE_SEC)

    # TradFi first, then by score. The stock perps are the half that cannot be
    # validated, so burying them under 100 crypto names would defeat the point
    # of scanning them at all.
    signals.sort(key=lambda s: (s.get("segment") != "tradfi", -(s.get("score") or 0)))
    return {"signals": signals, "checked": sum(checked.values()),
            "checked_by": checked, "rejected": rejected, "ts": time.time()}


# ── presentation ─────────────────────────────────────────────────────────────
def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001
        return timezone.utc


def format_signal(sig: dict) -> str:
    """One setup, in the shared house style so S4 reads like S1 and S3.

    The levels go in this table rather than through tg_format.mono_plan(),
    which needs TWO targets and returns an empty string if either is missing.
    S4 has a single target by construction, so mono_plan silently dropped the
    entire entry/stop/target block and the alert shipped with only percentages
    — a plan you cannot act on. One table, one aligned column, nothing to
    drop."""
    import tg_format as F
    p = sig.get("plan") or {}
    base = sig.get("base") or ""
    tag = "美股永續" if sig.get("segment") == "tradfi" else "加密永續"
    # The side must be in the HEADLINE, not inferred from the numbers. A short
    # whose header still said 做多 would be a plan that loses money by being
    # read correctly.
    is_long = sig.get("side", "long") == "long"
    bits = [F.headline(f"📊 S4 {tag}", base, "做多 LONG" if is_long else "做空 SHORT")]
    rows = []
    if p:
        # Stop is always AGAINST the trade and target always WITH it, so the
        # signs flip with the side rather than being hard-coded −/+.
        rows += [("進場", F.fmt_price(p["entry"])),
                 ("停損", f"{F.fmt_price(p['sl'])}  {'−' if is_long else '+'}{p['stop_pct']:.2f}%"),
                 ("目標", f"{F.fmt_price(p['tp'])}  {'+' if is_long else '−'}{p['tp_pct']:.2f}%  {p['rr']:g}R")]
    # Quality is S4's OWN read; 信心 is the S2 meter's. They answer different
    # questions and printing one as the other has bitten this repo before, so
    # both are shown with the basis quality was computed on — a 100 built from
    # 60 points of live input is a different claim from one built on all of it.
    q, qb = sig.get("quality"), sig.get("quality_basis")
    srcs = sig.get("div_sources") or []
    rows += [("品質", f"{q}/100" + (f"（基準 {qb}/100）" if qb and qb < 100 else "")
              if q is not None else "—"),
             ("信心", f"{sig['score']:.0f}/100" if sig.get("score") is not None else "—"),
             ("EMA200", (f"{'上升 +' if sig['slope'] >= 0 else '下降 '}{sig['slope']:.2f}%")
              if sig.get("slope") is not None else "—"),
             ("支撐" if is_long else "壓力",
              F.fmt_price(sig["support"]["level"]) + f"（{sig['support']['bars_ago']} 根前）"
              if sig.get("support") else "—"),
             ("背離", (f"{sig['div_ago']} 根前 · " + "+".join(srcs)) if srcs
              else (f"{sig['div_ago']} 根前" if sig.get("div_ago") is not None else "—")),
             ("未平倉", OI_TEXT.get(sig.get("oi_state"), "—"))]
    bits.append(F.pre_table(rows))
    bits.append(F.bybit_line(base, sig.get("price")))
    return "\n".join(b for b in bits if b)


DISCLAIMER = ("⚠️ S4 是「掃描通知」，不是已驗證的策略。這些美股永續大多 30 天內才上市"
              "（中位數 33 天），資料根本不夠做前向測試 —— 本專案量過 48 組高勝率設定有 46 組"
              "在賠錢，所以「圖上看起來對」不等於有優勢。自己判斷，這裡不會自動下單。")


def build_digest(result: dict, now=None) -> str:
    now = now or datetime.now(_tz())
    sigs = result.get("signals") or []
    by = result.get("checked_by") or {}
    seg = f"（美股 {by.get('tradfi', 0)} · 加密 {by.get('crypto', 0)}）" if by else ""
    head = f"📊 S4 掃描 · {now.strftime('%m-%d %H:%M')}"
    if not sigs:
        return ""
    body = "\n\n".join(format_signal(s) for s in sigs[:5])
    more = f"\n\n（另有 {len(sigs) - 5} 檔符合，見網頁）" if len(sigs) > 5 else ""
    return (f"{head}\n掃描 {result.get('checked', 0)} 檔{seg}，{len(sigs)} 檔符合"
            f"\n\n{body}{more}\n\n{DISCLAIMER}")


def due_signals(result: dict, state: dict, now_ts: float) -> list:
    """Drop anything alerted for the same symbol inside the cooldown. Without
    this a setup that stays valid for two hours pays out eight identical
    messages and the topic becomes unreadable."""
    sent = state.get("sent") or {}
    out = []
    for s in result.get("signals") or []:
        last = float(sent.get(s["symbol"]) or 0)
        if now_ts - last >= COOLDOWN_SEC:
            out.append(s)
    return out


def web_view() -> dict:
    """Last scan, for the /s4 page. Reads state only — never scans on a page
    load, or every visitor would fire 60 exchange calls."""
    payload = _load(SIGNALS_FILE)
    try:
        import strategy4_outcomes
        record = strategy4_outcomes.web_view()
    except Exception as exc:  # noqa: BLE001 — the page must render regardless
        print(f"[s4] outcome view failed: {exc}")
        record = {}
    return {"signals": payload.get("signals") or [],
            "checked": payload.get("checked"),
            "rejected": payload.get("rejected") or {},
            "ts": payload.get("ts"),
            "enabled": ENABLED,
            "universe": len(payload.get("universe") or []),
            "disclaimer": DISCLAIMER,
            "tf": TIMEFRAME, "tp_r": TP_R,
            "min_turnover": MIN_TURNOVER,
            "checked_by": payload.get("checked_by") or {},
            "max_tradfi": MAX_TRADFI, "max_crypto": MAX_CRYPTO,
            "record": record}


# ── orchestration ────────────────────────────────────────────────────────────
def _bar_key(now_ts: float) -> int:
    """Which 15m bar we are in. The scan runs once per CLOSED bar: the meter
    and every divergence here are defined on closed bars, so running more often
    would re-evaluate the same candle and change nothing but the API bill."""
    step = 15 * 60
    return int(now_ts // step)


def tick(client=None) -> bool:
    """Called once per S2 sweep. Self-paced: a no-op until a new bar closes.
    Returns True when a scan actually ran."""
    if not ENABLED:
        return False
    now_ts = time.time()
    state = _load()
    bar = _bar_key(now_ts)
    if state.get("last_bar") == bar:
        return False
    state["last_bar"] = bar
    _save(state)

    result = scan(client)
    payload = {**result, "universe": state.get("universe") or []}
    _save(payload, SIGNALS_FILE)

    fresh = due_signals(result, state, now_ts)
    if fresh:
        import telegram_utils
        text = build_digest({**result, "signals": fresh})
        if text:
            # channel="trades": the PRIVATE S1/S3/S4 feed (2026-08-08). It can
            # be pointed at a private GROUP via TELEGRAM_TRADES_CHAT_ID, so the
            # old rule still stands — nothing here may carry balances, position
            # sizes or free margin, only the setup itself.
            # parse_mode="HTML" is REQUIRED, not decoration: build_digest →
            # format_signal → tg_format.pre_table/bybit_line, which emit <pre>
            # and <a href>. Sent without it, Telegram prints the tags as
            # literal text and the whole plan arrives as unreadable markup —
            # which is exactly how this shipped until 2026-08-13.
            telegram_utils.send_message(text, parse_mode="HTML",
                                        force=True, channel="trades")
        sent = state.get("sent") or {}
        for s in fresh:
            sent[s["symbol"]] = now_ts
        state["sent"] = sent
    state["last_run"] = now_ts
    state["last_count"] = len(result.get("signals") or [])
    _save(state)

    # 📓 Outcome tracking. `fresh`, not result["signals"]: a setup that stays
    # valid for two hours reappears in eight consecutive scans, and counting it
    # eight times would inflate n eightfold. The alerted set is the honest unit
    # — one record per thing you were actually told about.
    #
    # This is also the promise the module docstring above already makes ("the
    # scan records every signal it fires so that test has data to work with"),
    # which was not true until now: SIGNALS_FILE is overwritten every scan, so
    # a setup that stopped qualifying left no trace that it had ever fired.
    tracked = {}
    try:
        import strategy4_outcomes
        tracked = strategy4_outcomes.tick(client, fresh, now_ts)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks the scan
        print(f"[s4] outcome tracking failed: {exc}")

    print(f"[s4] scanned {result.get('checked')} · {len(result.get('signals') or [])} setups"
          f" · {len(fresh)} alerted"
          + (f" · tracking {tracked.get('open', 0)} open,"
             f" settled {tracked.get('settled', 0)}" if tracked else ""))
    return True


def report_tg() -> str:
    """/s4 command — the last scan, on demand."""
    v = web_view()
    sigs = v["signals"]
    if not v["enabled"]:
        return "S4 沒有啟用（S4_ENABLED=false）。"
    if not v["ts"]:
        return "S4 還沒跑過第一次掃描。"
    age = int((time.time() - float(v["ts"])) / 60)
    if not sigs:
        rej = v.get("rejected") or {}
        top = "、".join(f"{k} {n}" for k, n in sorted(rej.items(), key=lambda r: -r[1])[:3])
        return (f"📊 S4 · {age} 分鐘前掃描 {v['checked']} 檔\n目前沒有符合的設定。\n"
                + (f"主要卡在：{top}" if top else ""))
    body = "\n\n".join(format_signal(s) for s in sigs[:5])
    return f"📊 S4 · {age} 分鐘前 · {len(sigs)} 檔符合\n\n{body}\n\n{DISCLAIMER}"
