"""
backtest.py — replay the Wolf Scanner strategy over HISTORICAL candles so you can
see win-rate / expectancy / PnL in seconds instead of waiting days for live trades.

    /Users/wolfman/miniforge3/bin/python backtest.py            # defaults: 12 symbols, 21 days
    /Users/wolfman/miniforge3/bin/python backtest.py 30 20      # 30 days, top 20 symbols
    /Users/wolfman/miniforge3/bin/python backtest.py 21 12 BTC,ETH,SOL   # specific symbols

It reuses the SAME functions the live bot uses (lights, score, SMC, trade levels,
runner-trail management) on the same 260-candle window, so the numbers track the
live dry-run. It is an approximation in a few documented ways (see NOTES at the
bottom). Change a setting in config.py, re-run, and compare instantly.
"""
import os
import sys
import time
import bisect

import numpy as np
import pandas as pd
import ccxt

import config as C
import bot
import smc as smc_mod
from indicators import (
    check_tsi_signal, check_macd_signal, check_volume_gate,
    check_stoch_rsi_signal,
    calculate_ema, calculate_vwap,
    calculate_volume_profile, calculate_order_flow,
)

WINDOW = 260          # candles the live bot sees (get_symbol_ohlcv limit)
MAX_WAIT_BARS = 6     # bars a queued entry waits to fill before it expires
MAX_HOLD_BARS = 192   # safety cap on how long a trade is tracked (~8 days on the 1h timeframe)
# Round-trip trading cost as a % of notional, deducted from each trade's return so
# results are realistic. Binance futures taker ≈ 0.04%/fill; a partial trade has
# ~3 fills (entry + TP1 + TP2) and there's always some slippage, so ~0.12% total.
FEE_PCT = 0.12

# ── realism model (off the headline number, toggleable per run) ───────────────
# The base sim fills stops at the exact price and ignores funding — optimistic.
# When realism is on we additionally charge:
#   • SLIPPAGE_PCT on BOTH the entry and the exit (stops slip against you), and
#   • FUNDING_PCT_PER_8H for every 8h the position is held (perps; longs pay in a
#     bull). These shrink the backtest toward what a live account actually keeps.
SLIPPAGE_PCT       = float(os.getenv("BT_SLIPPAGE_PCT", "0.05"))    # % per fill (×2 round-trip)
FUNDING_PCT_PER_8H = float(os.getenv("BT_FUNDING_PCT_8H", "0.01"))  # % of notional per 8h held


def apply_costs(pnl, bars_held, tf_hours, realism=True):
    """Deduct trading costs from a raw trade pnl% and return the net pnl%.

    Always charges the round-trip FEE_PCT. When `realism` is on it additionally
    charges SLIPPAGE_PCT on BOTH fills (entry + exit) and FUNDING_PCT_PER_8H for
    every 8h the position was held — so the result tracks what a live account
    actually keeps. `bars_held` is clamped at 0 (no funding "credit")."""
    pnl = float(pnl) - FEE_PCT
    if realism:
        pnl -= 2 * SLIPPAGE_PCT
        pnl -= FUNDING_PCT_PER_8H * (max(0, bars_held) * tf_hours / 8.0)
    return pnl


ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})


# ── data fetching ───────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, timeframe, days):
    """Paginate fetch_ohlcv to get `days` of history (newest last)."""
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since = now - int(days * 24 * 3600 * 1000) - tf_ms * (WINDOW + 5)
    out = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        out += batch
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:                       # no forward progress
            break
        since = nxt
        if batch[-1][0] >= now - tf_ms * 2:     # reached ~now
            break
    # dedupe by timestamp, keep order
    seen = set(); clean = []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0]); clean.append(c)
    return clean


def top_symbols(n):
    t = ex.fetch_tickers()
    perps = [(s, v.get("quoteVolume") or 0) for s, v in t.items() if s.endswith(":USDT")]
    perps.sort(key=lambda kv: kv[1], reverse=True)
    return [s for s, _ in perps[:n]]


# ── higher-timeframe context (computed as-of each bar, no look-ahead) ─────────
def ema_series_4h(ohlcv_4h, period=50):
    closes = [c[4] for c in ohlcv_4h]
    import pandas as pd
    ema = pd.Series(closes).ewm(span=period, adjust=False).mean().tolist()
    return [(ohlcv_4h[i][0], ema[i]) for i in range(len(ohlcv_4h))]


def btc_regime_series(btc_1h):
    """Per-1h-bar regime (bull/bear/neutral) using EMA50 + slope, like the live bot."""
    import pandas as pd
    closes = [c[4] for c in btc_1h]
    ema = pd.Series(closes).ewm(span=C.BTC_REGIME_EMA, adjust=False).mean().tolist()
    lb = C.BTC_REGIME_SLOPE_LOOKBACK
    out = []
    for i in range(len(btc_1h)):
        reg = "neutral"
        if i >= C.BTC_REGIME_EMA + lb:
            now, prev, price = ema[i], ema[i - lb], closes[i]
            if price > now and now > prev:
                reg = "bull"
            elif price < now and now < prev:
                reg = "bear"
        out.append((btc_1h[i][0], reg))
    return out


def as_of(series, ts, default):
    """Value of a (ts,val) series as of timestamp ts (no look-ahead)."""
    if not series:
        return default
    i = bisect.bisect_right([s[0] for s in series], ts) - 1
    return series[i][1] if i >= 0 else default


# ── the strategy decision — mirrors bot.run_bot's qualification block ────────
# ADX filter per-run override for the backtester UI: None = use the config flag
# (ENABLE_ADX_FILTER), True/False = force on/off for the current run_backtest()
# call only. Lets the UI A/B the filter without editing .env or restarting.
_ADX_OVERRIDE = None


def _adx_enabled():
    return _ADX_OVERRIDE if _ADX_OVERRIDE is not None else getattr(C, "ENABLE_ADX_FILTER", False)


# Backtest-only volatility-regime experiment (NOT wired to live/config — purely an
# A/B lever for strengthening S1). None = off; set to {"min": pct, "max": pct} via
# run_backtest(vol=...) to require last-bar ATR-as-%-of-price to sit inside that
# band (skips dead-low-vol chop and chaotic extremes). Reset in run_backtest finally.
_VOL_OVERRIDE = None


def _atr_pct(oh, period=14):
    """Last-bar ATR as a % of close (mean true range). None on short history."""
    if len(oh) < period + 1:
        return None
    trs = []
    for i in range(1, len(oh)):
        h, l, pc = oh[i][2], oh[i][3], oh[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-period:]) / period
    close = oh[-1][4]
    return (atr / close * 100.0) if close else None


def evaluate(oh, ema50_4h, btc_regime):
    """Return (qualified, is_long, entry, sl, tp1, tp2, eff_lights) for the last bar."""
    prices = [x[4] for x in oh]
    volumes = [x[5] for x in oh]
    if len(prices) < C.MIN_CANDLES_FOR_SIGNAL:
        return None
    scan_price = prices[-1]

    rsi_values = bot.calculate_rsi_full(prices, period=C.RSI_PERIOD)
    rsi_values = list(rsi_values) if rsi_values is not None else []
    if not rsi_values:
        return None
    current_rsi = rsi_values[-1]

    bull, bear, _ = bot.calculate_strategy_score(prices, volumes, rsi_values)
    display_score = max(bull, bear)
    strat_1, _, _ = check_stoch_rsi_signal(prices, rsi_period=C.STOCH_RSI_RSI_PERIOD,
        stoch_period=C.STOCH_RSI_STOCH_PERIOD, k_smooth=C.STOCH_RSI_K_SMOOTH,
        d_smooth=C.STOCH_RSI_D_SMOOTH, oversold=C.STOCH_RSI_OVERSOLD, overbought=C.STOCH_RSI_OVERBOUGHT)
    strat_2 = bull >= C.STRATEGY_SCORE_LIGHT_THRESHOLD or bear >= C.STRATEGY_SCORE_LIGHT_THRESHOLD
    ema_9 = calculate_ema(prices, 9); ema_20 = calculate_ema(prices, 20)
    ema_50 = calculate_ema(prices, 50); ema_200 = calculate_ema(prices, 200)
    avg_vol_20 = np.mean(volumes[-20:]); current_vol = volumes[-1]
    bull_mom = bear_mom = False
    if ema_9 is not None and ema_20 is not None:
        nob = current_rsi <= 70; nos = current_rsi >= 30
        bull_mom = ema_9 > ema_20 and current_vol > 1.5 * avg_vol_20 and nob
        bear_mom = ema_9 < ema_20 and current_vol > 1.5 * avg_vol_20 and nos
    strat_3 = bull_mom or bear_mom
    strat_4, _, tsi_hint = check_tsi_signal(oh)
    strat_5, _, macd_hint = check_macd_signal(prices)
    smc = smc_mod.analyze_smc(oh)

    if not (strat_1 or strat_2 or strat_3 or strat_4 or strat_5 or display_score >= 4):
        return None

    strategy_lights = [bool(strat_1), bool(strat_2), bool(strat_3), bool(strat_4), bool(strat_5)]
    is_long = bot.determine_trade_direction(current_rsi, bull, bear, bull_mom, bear_mom,
        strat_4, tsi_hint, strat_5, macd_hint, ema_9, ema_20, ema_50, scan_price, smc)

    # L6: Volume Profile — price in discount zone (long) or premium zone (short)
    vp_data = calculate_volume_profile(oh, lookback=50)
    strat_6 = (vp_data is not None and (
        (is_long and vp_data["below_val"]) or (not is_long and vp_data["above_vah"])
    ))
    # L7: Order Flow — cumulative delta + absorption/imbalance aligned with direction
    of_data = calculate_order_flow(oh, lookback=20)
    strat_7 = (of_data is not None and (
        (is_long and of_data["bullish"]) or (not is_long and of_data["bearish"])
    ))
    strategy_lights.extend([bool(strat_6), bool(strat_7)])

    volume_ok = check_volume_gate(volumes, C.VOLUME_GATE_MULTIPLIER)
    trend_4h_aligned = True
    if C.ENABLE_4H_TREND_FILTER and ema50_4h is not None:
        if is_long and scan_price < ema50_4h: trend_4h_aligned = False
        elif not is_long and scan_price > ema50_4h: trend_4h_aligned = False

    smc_bonus = 0
    if is_long and smc.get("liquidity_sweep_bullish"): smc_bonus += 2
    elif not is_long and smc.get("liquidity_sweep_bearish"): smc_bonus += 2
    if is_long and smc.get("choch_bullish"): smc_bonus += 1
    elif not is_long and smc.get("choch_bearish"): smc_bonus += 1
    smc_bonus += smc.get("ob_grade_bullish", 0) if is_long else smc.get("ob_grade_bearish", 0)
    zone = smc.get("zone_type", "neutral")
    if (is_long and zone == "support") or ((not is_long) and zone == "resistance"):
        smc_bonus += C.SMC_IDEAL_ZONE_BONUS
    smc_bonus = min(smc_bonus, C.MAX_SMC_BONUS_LIGHTS)

    smc_blocked = (is_long and zone == "resistance") or ((not is_long) and zone == "support")
    trend_aligned = ema_50 is None or (scan_price > ema_50 if is_long else scan_price < ema_50)
    macro_ok = ema_200 is None or (scan_price > ema_200 if is_long else scan_price < ema_200)
    momentum_ok = (len(rsi_values) >= 2 and
                   (rsi_values[-1] > rsi_values[-2] if is_long else rsi_values[-1] < rsi_values[-2]))

    base_lights = sum(strategy_lights)
    eff_lights = base_lights + smc_bonus
    score_edge = (bull - bear) if is_long else (bear - bull)
    base_ok = base_lights >= C.MIN_BASE_LIGHTS

    entry, sl, tp1, tp2 = bot.calculate_trade_levels(scan_price, is_long, eff_lights, oh, smc)
    required = C.MIN_LIGHTS_FOR_RECORD if is_long else C.MIN_LIGHTS_SHORT

    rsi_ok = True
    if is_long and current_rsi >= C.RSI_BLOCK_LONG_ABOVE: rsi_ok = False
    elif (not is_long) and current_rsi <= C.RSI_BLOCK_SHORT_BELOW: rsi_ok = False

    btc_ok = True
    if C.ENABLE_BTC_REGIME_FILTER:
        if is_long and btc_regime == "bear": btc_ok = False
        elif (not is_long) and btc_regime == "bull": btc_ok = False
    if not getattr(C, "ENABLE_SHORTS", True) and not is_long:
        btc_ok = False  # longs-only mode
    # VWAP confirmation (mirror of bot.py)
    if getattr(C, "ENABLE_VWAP_FILTER", False):
        vwap = calculate_vwap(oh, getattr(C, "VWAP_PERIOD", 24))
        if vwap is not None:
            if is_long and scan_price < vwap: btc_ok = False
            elif (not is_long) and scan_price > vwap: btc_ok = False

    not_extended = entry is None or tp1 is None or (
        scan_price < tp1 if is_long else scan_price > tp1)

    # Optional ADX trend-strength gate — DEFAULT OFF, driven by the SAME config
    # flags as the live bot (bot.py), so a backtest with ENABLE_ADX_FILTER set
    # faithfully previews what the live default/S1 strategy would do. (S2/S4 have
    # their own always-on ADX gates; this one only affects the default path.)
    adx_ok = True
    if _adx_enabled():
        _adx_s = _adx_series(oh, getattr(C, "ADX_PERIOD", ADX_PERIOD))
        if _adx_s is not None and len(_adx_s) and not np.isnan(_adx_s[-1]):
            adx_now = _adx_s[-1]
            strong = adx_now >= getattr(C, "ADX_MIN_THRESHOLD", ADX_MIN)
            rising = (not getattr(C, "ADX_REQUIRE_RISING", False)) or (
                len(_adx_s) >= 2 and not np.isnan(_adx_s[-2]) and adx_now > _adx_s[-2])
            adx_ok = bool(strong and rising)
        # Can't compute ADX (short history) → leave adx_ok True (don't block on
        # degraded data) — mirrors the live bot's behaviour exactly.

    # Backtest-only volatility-regime gate (experiment; off unless run_backtest(vol=...)).
    vol_ok = True
    if _VOL_OVERRIDE is not None:
        _atrp = _atr_pct(oh)
        if _atrp is not None:
            vol_ok = (_atrp >= _VOL_OVERRIDE.get("min", 0.0)) and (_atrp <= _VOL_OVERRIDE.get("max", 1e9))

    qualified = (eff_lights >= required and base_ok and trend_aligned and macro_ok and momentum_ok
                 and score_edge >= 2 and not smc_blocked and rsi_ok and btc_ok and not_extended
                 and volume_ok and trend_4h_aligned and adx_ok and vol_ok and entry is not None)
    if not qualified:
        return None
    return (is_long, entry, sl, tp1, tp2, eff_lights)


# ── Strategy 2: Trend-Momentum Breakout (Donchian + EMA200 + ADX + volume) ───
# A classic, well-documented trend-following system, deliberately DIFFERENT in
# character from Strategy 1's confluence/mean-reversion flavour:
#   • Macro trend gate — only long above the EMA200, only short below it.
#   • Entry — price CLOSES beyond the prior N-bar Donchian channel (a real
#     breakout; wicks that close back inside don't count). The resting order then
#     fills on a retest of the broken level, so it slots into the same limit-fill
#     simulator Strategy 1 uses (fair, apples-to-apples trade management).
#   • ADX ≥ threshold — only trade when a genuine trend exists (skip the chop
#     where breakouts are fakeouts).
#   • Volume confirmation — a breakout on weak volume is a likely trap.
#   • Shared risk controls — BTC-regime "don't fight Bitcoin" filter, ATR stop,
#     genuine 1R/2R targets, identical to Strategy 1 so only the SIGNAL differs.
DONCHIAN_PERIOD = 20   # lookback for the breakout channel
ADX_PERIOD = 14
ADX_MIN = 20.0         # trend-strength floor (below this = chop, skip)


def _adx_series(ohlcv, period=ADX_PERIOD):
    """Wilder's ADX series (numpy array) over the candles, or None if too short."""
    if len(ohlcv) < period * 2 + 1:
        return None
    high = pd.Series([x[2] for x in ohlcv], dtype=float)
    low = pd.Series([x[3] for x in ohlcv], dtype=float)
    close = pd.Series([x[4] for x in ohlcv], dtype=float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    # Wilder smoothing == EWM with alpha = 1/period (com = period - 1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx.to_numpy()


def calculate_adx(ohlcv, period=ADX_PERIOD):
    """Wilder's ADX on the given candles. Returns the latest ADX value or None."""
    s = _adx_series(ohlcv, period)
    if s is None or len(s) == 0:
        return None
    val = s[-1]
    return float(val) if not np.isnan(val) else None



# ── trade simulation — mirrors bot.update_pending_signals management ─────────
def simulate_trade(oh15, i, is_long, entry, sl, tp1, tp2):
    """Walk forward from bar i: resting-limit fill, then TP1/SL + runner trail.
    Returns (pnl_pct, close_bar) or None if the entry never filled."""
    n = len(oh15)
    # 1) resting entry fill
    fill = None
    for j in range(i + 1, min(i + 1 + MAX_WAIT_BARS, n)):
        hi, lo = oh15[j][2], oh15[j][3]
        if C.CANCEL_QUEUE_IF_TARGET_HIT and ((is_long and hi >= tp1) or (not is_long and lo <= tp1)):
            return None  # target printed before entry → cancelled
        if (is_long and lo <= entry) or (not is_long and hi >= entry):
            fill = j; break
    if fill is None:
        return None

    tp1_pct = ((tp1 - entry) / entry * 100) if is_long else ((entry - tp1) / entry * 100)
    partial = False
    for k in range(fill, min(fill + MAX_HOLD_BARS, n)):
        hi, lo = oh15[k][2], oh15[k][3]
        if not partial:
            tp1_hit = hi >= tp1 if is_long else lo <= tp1
            sl_hit = lo <= sl if is_long else hi >= sl
            if sl_hit:                                   # SL (incl. ambiguous bar → conservative)
                pnl = ((sl - entry) / entry * 100) if is_long else ((entry - sl) / entry * 100)
                return pnl, k
            if tp1_hit:
                partial = True
                continue
        else:
            # Runner (50%) = live bracket: hard TP2 at 2R + breakeven stop.
            tp2_hit = hi >= tp2 if is_long else lo <= tp2
            be_hit = lo <= entry if is_long else hi >= entry
            if be_hit:                                   # breakeven (incl. ambiguous → conservative)
                return tp1_pct * 0.5, k                  # only the TP1 half is booked
            if tp2_hit:
                tp2_pct = ((tp2 - entry) / entry * 100) if is_long else ((entry - tp2) / entry * 100)
                return tp1_pct * 0.5 + tp2_pct * 0.5, k
    # not closed within hold cap → mark to last close
    last = oh15[min(fill + MAX_HOLD_BARS, n) - 1][4]
    if not partial:
        return None
    runner_pct = ((last - entry) / entry * 100) if is_long else ((entry - last) / entry * 100)
    return tp1_pct * 0.5 + runner_pct * 0.5, min(fill + MAX_HOLD_BARS, n) - 1


def _group(trades, predicate):
    import statistics as st
    sub = [t for t in trades if predicate(t)]
    if not sub:
        return {"trades": 0, "win_rate": 0.0, "expectancy_r": 0.0}
    wins = sum(1 for t in sub if t["win"])
    return {
        "trades": len(sub),
        "win_rate": round(wins / len(sub) * 100, 1),
        "expectancy_r": round(st.mean([t["rr"] for t in sub]), 3),
    }


def summarize(trades, secs, days, nsym):
    """Aggregate the raw trade list into a structured result dict + equity curve."""
    import statistics as st
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    n = len(trades)

    # Equity curve in chronological order: cumulative R and cumulative %, plus
    # the underwater drawdown (in R) for a risk read.
    chrono = sorted(trades, key=lambda t: t.get("ts", 0))
    cum_r = cum_pct = peak = max_dd = 0.0
    curve = []
    for idx, t in enumerate(chrono, 1):
        cum_r += t["rr"]; cum_pct += t["pnl"]
        peak = max(peak, cum_r)
        max_dd = min(max_dd, cum_r - peak)
        curve.append({"i": idx, "cum_r": round(cum_r, 2), "cum_pct": round(cum_pct, 2)})

    # Outlier dependence: how much of the total R rides on the top 3 winners.
    # If a few trades carry most of the result, the "edge" is fragile / luck.
    all_r = sorted((t["rr"] for t in trades), reverse=True)
    total_r_val = sum(all_r)
    top3_r = sum(all_r[:3])
    top3_share = round(top3_r / total_r_val * 100, 1) if total_r_val > 0 else None
    r_ex_top3 = round(total_r_val - top3_r, 2)

    return {
        "days": days, "n_symbols": nsym, "seconds": round(secs, 1),
        "total": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_win_r": round(st.mean([t["rr"] for t in wins]), 2) if wins else 0.0,
        "avg_loss_r": round(st.mean([t["rr"] for t in losses]), 2) if losses else 0.0,
        "expectancy_r": round(st.mean([t["rr"] for t in trades]), 3) if n else 0.0,
        "total_return": round(sum(t["pnl"] for t in trades), 1),
        "max_drawdown_r": round(max_dd, 2),
        "top3_r": round(top3_r, 2),
        "top3_share_pct": top3_share,   # % of total R from the 3 best trades (None if net ≤ 0)
        "total_r_ex_top3": r_ex_top3,   # what's left without those 3
        "total_r": round(total_r_val, 2),
        "by_direction": {d: _group(trades, lambda t, d=d: t["dir"] == d) for d in ("LONG", "SHORT")},
        "by_tier": {
            "4": _group(trades, lambda t: t["lights"] == 4),
            "5": _group(trades, lambda t: t["lights"] == 5),
            "6+": _group(trades, lambda t: t["lights"] >= 6),
        },
        "curve": curve,
        "trades": sorted(trades, key=lambda t: t["pnl"], reverse=True),
    }


# Strategy registry — the backtester and CLI dispatch on these keys. Each entry
# has an `fn` (decision engine, signature (oh, ema50_4h, btc_regime)) and an
# optional `sim` (trade-management simulator; defaults to the 1R/2R bracket).
STRATEGIES = {
    "default": {
        "name": "Strategy 1 — Wolf Confluence (live bot)",
        "desc": "The current live strategy: 5-light StochRSI/score/EMA/TSI/MACD confluence + SMC + regime filter.",
        "fn": evaluate,
        "sim": simulate_trade,
    },
    # Strategies 2–5 were removed 2026-06-28. The project now keeps only S1 (this
    # live engine) plus the "Strategy 2" TradingView confidence meter
    # (app/strategy2_meter.py) — a chart indicator, NOT a backtest engine, so it is
    # not registered here. Legacy DB rows tagged with old keys still resolve to
    # 'default' in the web UI (see app._filter_records_by_strategy).
}


def run_backtest(days, symbols, timeframe="15m", strategy="default", progress=None, realism=True, adx="default", vol=None):
    """Entry point used by the CLI and web page. `adx` overrides the ADX filter
    for THIS run only: 'on'/'off' force it, 'default' uses the config flag —
    so the UI can A/B the filter without editing .env or restarting anything.
    `vol` is a backtest-only volatility experiment: pass {"min": pct, "max": pct}
    to require last-bar ATR-as-%-of-price inside that band (None = off)."""
    global _ADX_OVERRIDE, _VOL_OVERRIDE
    _ADX_OVERRIDE = {"on": True, "off": False}.get(adx, None)
    _VOL_OVERRIDE = vol
    try:
        return _run_backtest_impl(days, symbols, timeframe, strategy, progress, realism)
    finally:
        _ADX_OVERRIDE = None
        _VOL_OVERRIDE = None


def _run_backtest_impl(days, symbols, timeframe="15m", strategy="default", progress=None, realism=True):
    """Core entry point used by both the CLI and the web page.
    `timeframe` is the scan/entry timeframe (e.g. '15m', '1h', '4h'). The 4H trend
    bias and 1H BTC regime stay as higher-TF context so the comparison is clean.
    `strategy` selects which decision engine to test (see STRATEGIES).
    `realism` adds slippage + funding on top of fees (pessimistic, live-like).
    `progress(done, total, message)` is called as each symbol finishes."""
    strat = STRATEGIES.get(strategy) or STRATEGIES["default"]
    eval_fn = strat["fn"]
    sim_fn = strat.get("sim") or simulate_trade
    total = len(symbols)
    tf_hours = ex.parse_timeframe(timeframe) / 3600.0   # for funding accrual
    if progress:
        progress(0, total, "Fetching BTC regime history…")
    btc1h = fetch_ohlcv("BTC/USDT:USDT", "1h", days + 5)
    btc_reg = btc_regime_series(btc1h)

    trades = []
    t0 = time.time()
    for si, sym in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv(sym, timeframe, days)
            oh4h = fetch_ohlcv(sym, "4h", days + 10)
        except Exception:  # noqa: BLE001
            if progress:
                progress(si, total, f"{sym}: fetch failed")
            continue
        ema4h = ema_series_4h(oh4h)
        n = len(ohlcv)
        i = WINDOW
        cnt = 0
        while i < n - 1:
            oh = ohlcv[i - WINDOW + 1:i + 1]
            ts = ohlcv[i][0]
            res = eval_fn(oh, as_of(ema4h, ts, None), as_of(btc_reg, ts, "neutral"))
            if res:
                is_long, entry, sl, tp1, tp2, eff = res
                out = sim_fn(ohlcv, i, is_long, entry, sl, tp1, tp2)
                if out:
                    pnl, close_bar = out
                    # Cast to native Python types — calculate_trade_levels uses numpy
                    # (ATR), so entry/sl/pnl can be np.float64 which jsonify can't serialise.
                    pnl = apply_costs(pnl, int(close_bar) - i, tf_hours, realism)
                    R = float(abs(entry - sl) / entry * 100)
                    trades.append({"symbol": sym.split("/")[0], "dir": "LONG" if is_long else "SHORT",
                                   "lights": int(eff), "pnl": round(pnl, 2), "R": round(R, 2),
                                   "rr": round(pnl / R, 2) if R else 0.0, "win": bool(pnl > 0),
                                   "ts": int(ohlcv[close_bar][0])})
                    cnt += 1
                    i = close_bar + 1
                    continue
            i += 1
        if progress:
            progress(si, total, f"{sym.split('/')[0]}: {cnt} trades")
    res = summarize(trades, time.time() - t0, days, total)
    res["timeframe"] = timeframe
    res["strategy"] = strategy
    res["strategy_name"] = strat["name"]
    res["realism"] = bool(realism)
    res["adx_filter"] = bool(_adx_enabled())   # whether the ADX gate was active this run
    res["costs"] = {"fee_pct": FEE_PCT,
                    "slippage_pct": SLIPPAGE_PCT if realism else 0.0,
                    "funding_pct_per_8h": FUNDING_PCT_PER_8H if realism else 0.0}
    return res


def print_report(r):
    print("\n" + "=" * 60)
    print(f"  RESULTS  ({r['total']} trades over {r['days']}d × {r['n_symbols']} symbols, {r['seconds']:.0f}s)")
    print("=" * 60)
    if not r["total"]:
        print("  No trades qualified. Filters may be too strict for this window.")
        return
    def line(l, v): print(f"  {l:<22} {v}")
    line("Win rate", f"{r['win_rate']}%   ({r['wins']}W / {r['losses']}L)")
    line("Avg win", f"+{r['avg_win_r']}R")
    line("Avg loss", f"{r['avg_loss_r']}R")
    line("Expectancy / trade", f"{r['expectancy_r']:+}R   <-- the edge")
    line("Total return", f"{r['total_return']:+}%")
    if r.get("top3_share_pct") is not None:
        line("Top-3 winners", f"{r['top3_r']:+}R  ({r['top3_share_pct']}% of total R)")
        line("Total R ex top-3", f"{r['total_r_ex_top3']:+}R   <-- edge without the outliers")
    line("Realism (slip+funding)", "ON" if r.get("realism") else "OFF")
    print("  " + "-" * 56)
    for d, s in r["by_direction"].items():
        if s["trades"]: line(d, f"{s['trades']} trades, {s['win_rate']}% win, exp {s['expectancy_r']:+}R")
    for t, s in r["by_tier"].items():
        if s["trades"]: line(f"{t} lights", f"{s['trades']} trades, {s['win_rate']}% win, exp {s['expectancy_r']:+}R")
    print("=" * 60)


if __name__ == "__main__":
    # Usage: backtest.py [days] [n_symbols] [timeframe] [strategy] [tickers]
    #   strategy ∈ {default, trend_breakout}  (run with no args to list them)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    nsym = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    # Default to the live config's timeframe (currently 1h — the validated edge),
    # NOT a hard-coded 15m. Otherwise `backtest.py 90 50` silently tests the
    # abandoned 15m strategy and reports a misleading negative edge.
    tf = sys.argv[3] if len(sys.argv) > 3 else (C.TIMEFRAMES[0] if C.TIMEFRAMES else "1h")
    strategy = sys.argv[4] if len(sys.argv) > 4 else "default"
    if strategy not in STRATEGIES:
        print(f"Unknown strategy '{strategy}'. Available: {', '.join(STRATEGIES)}")
        sys.exit(1)
    if len(sys.argv) > 5:
        syms = [f"{s.strip().upper()}/USDT:USDT" for s in sys.argv[5].split(",")]
    else:
        print(f"Selecting top {nsym} symbols by volume…")
        syms = top_symbols(nsym)
    print(f"Backtest — {days}d, {len(syms)} symbols, {tf} timeframe — {STRATEGIES[strategy]['name']}\n")
    result = run_backtest(days, syms, timeframe=tf, strategy=strategy,
                          progress=lambda d, t, m: print(f"  [{d}/{t}] {m}"))
    print_report(result)
