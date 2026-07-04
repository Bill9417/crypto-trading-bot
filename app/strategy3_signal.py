"""
Strategy 3 — EXACT bar-by-bar Python port of TV_strategy.pine's signal chain.

Every component is replicated 1:1 against the Pine script so the live bot and
the TradingView backtest fire the SAME flags on the same candles:

    EMAs / Vegas / Tunnels / Volume bias .. vectorised, ewm(span) == ta.ema
    SMC internal bias ..................... LuxAlgo leg(5)/leg(50) with the
                                            crossed flags + internal≠swing filter
    MSB arm ............................... EmreKb zigzag + fib-buffer 'market'
                                            flip (ta.valuewhen guard included)
    ADX regime gate ....................... Wilder RMA dmi, per bar
    Flags ................................. arm-and-fire with lastSig alternation,
                                            evaluated on CLOSED bars only

The ONLY deliberate divergence (same as the Pine strategy, documented there):
the trendline factor (weight 5) is held neutral — ±2.5 score wiggle.

Pure-compute module: no network, no config. Callers pass OHLCV (list of
[ts,o,h,l,c,v], oldest→newest, CLOSED candles only) plus the thresholds.
History depth matters for fidelity: the arm/alternation state replays from the
start of the series, so pass ~1000 candles (the scanner default), not the bare
~400 the indicators need.
"""
import numpy as np
import pandas as pd

MIN_CANDLES = 400          # EMA338 + Vegas smoothing + volume window ≈ floor
ZIGZAG_LEN = 9             # TV.pine MSB defaults
FIB_FACTOR = 0.33
INTERNAL_LEG = 5           # LuxAlgo internal / swing structure lengths
SWING_LEG = 50
VOL_BIAS_LEN = 360
ADX_LEN = 14


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def adx_series(h: pd.Series, l: pd.Series, c: pd.Series, length: int = ADX_LEN) -> pd.Series:
    """ta.dmi(length, length)[2] — Wilder smoothing throughout."""
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = _rma(tr, length)
    pdi = 100 * _rma(plus_dm, length) / atr
    mdi = 100 * _rma(minus_dm, length) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _rma(dx.fillna(0), length)


def compute(ohlcv, score_th: int = 70, adx_th: int = 20) -> dict:
    """Full-series replay. Returns per-bar arrays plus the last-bar summary:
    {flag, score, vegas, msb, price, insufficient, flags, scores}."""
    n = len(ohlcv) if ohlcv else 0
    if n < 60:                                   # not even enough to warm up
        return {"flag": None, "score": 50.0, "vegas": 0, "msb": None,
                "price": float(ohlcv[-1][4]) if n else 0.0,
                "insufficient": True, "flags": np.zeros(n, int),
                "scores": np.full(n, 50.0)}

    h = pd.Series([float(x[2]) for x in ohlcv])
    l = pd.Series([float(x[3]) for x in ohlcv])
    c = pd.Series([float(x[4]) for x in ohlcv])
    v = pd.Series([float(x[5]) for x in ohlcv])
    ha, la, ca = h.values, l.values, c.values

    # ── vectorised factors (identical formulas to the Pine script) ──
    e20, e50, e100, e200 = (_ema(c, k).values for k in (20, 50, 100, 200))
    e144, e169, e288, e338 = (_ema(c, k).values for k in (144, 169, 288, 338))

    vegas = _ema(c, 200).rolling(5).mean()
    dv = vegas.diff().values
    f_vegas = np.where(np.isnan(dv), 0, np.sign(dv)).astype(int)

    f_stack = np.where((e20 > e50) & (e50 > e100) & (e100 > e200), 1,
                       np.where((e20 < e50) & (e50 < e100) & (e100 < e200), -1, 0))
    f_e200 = np.where(ca > e200, 1, np.where(ca < e200, -1, 0))
    f_tun = np.where((ca > e144) & (ca > e288), 1,
                     np.where((ca < e169) & (ca < e338), -1, 0))

    vol_mean = ((c * v).rolling(VOL_BIAS_LEN).sum() / v.rolling(VOL_BIAS_LEN).sum()).values
    f_vol = np.where(np.isnan(vol_mean), 0,
                     np.where(ca > vol_mean, 1, np.where(ca < vol_mean, -1, 0)))

    adx = adx_series(h, l, c).values

    # rolling extremes used by the stateful loops
    rmax_zz = h.rolling(ZIGZAG_LEN, min_periods=1).max().values   # ta.highest(9)
    rmin_zz = l.rolling(ZIGZAG_LEN, min_periods=1).min().values
    to_up = ha >= rmax_zz
    to_down = la <= rmin_zz
    rmax_i = h.rolling(INTERNAL_LEG, min_periods=1).max().values  # ta.highest(5)
    rmin_i = l.rolling(INTERNAL_LEG, min_periods=1).min().values
    rmax_s = h.rolling(SWING_LEG, min_periods=1).max().values
    rmin_s = l.rolling(SWING_LEG, min_periods=1).min().values

    # ── loop 1: SMC internal bias + EmreKb zigzag 'market' ──
    f_smc = np.zeros(n, dtype=int)
    msb_up = np.zeros(n, dtype=bool)
    msb_dn = np.zeros(n, dtype=bool)
    market_arr = np.ones(n, dtype=int)

    leg_i, leg_s = 0, 0
    i_high = i_low = s_high = s_low = None
    i_high_x = i_low_x = True
    i_high_hist = np.full(n, np.nan)              # level series for crossover
    i_low_hist = np.full(n, np.nan)
    bias = 0

    trend_l = 1
    h0 = h1 = l0 = l1 = None
    last_up_bar = last_dn_bar = None              # for ta.barssince(to_up[1])
    mkt = 1
    flip_l0 = flip_h0 = None                      # ta.valuewhen(change, l0/h0, 0)

    for i in range(n):
        # SMC legs: leg(size) — new pivot when high[size] > highest(size)
        if i >= INTERNAL_LEG:
            if ha[i - INTERNAL_LEG] > rmax_i[i]:
                if leg_i != 0:
                    leg_i = 0
                    i_high, i_high_x = ha[i - INTERNAL_LEG], False
            elif la[i - INTERNAL_LEG] < rmin_i[i]:
                if leg_i != 1:
                    leg_i = 1
                    i_low, i_low_x = la[i - INTERNAL_LEG], False
        if i >= SWING_LEG:
            if ha[i - SWING_LEG] > rmax_s[i]:
                if leg_s != 0:
                    leg_s = 0
                    s_high = ha[i - SWING_LEG]
            elif la[i - SWING_LEG] < rmin_s[i]:
                if leg_s != 1:
                    leg_s = 1
                    s_low = la[i - SWING_LEG]

        i_high_hist[i] = i_high if i_high is not None else np.nan
        i_low_hist[i] = i_low if i_low is not None else np.nan

        # ta.crossover(close, level) with the crossed + internal≠swing filters
        if i >= 1 and i_high is not None and not i_high_x and i_high != s_high:
            prev_lvl = i_high_hist[i - 1]
            if ca[i] > i_high and not np.isnan(prev_lvl) and ca[i - 1] <= prev_lvl:
                i_high_x, bias = True, 1
        if i >= 1 and i_low is not None and not i_low_x and i_low != s_low:
            prev_lvl = i_low_hist[i - 1]
            if ca[i] < i_low and not np.isnan(prev_lvl) and ca[i - 1] >= prev_lvl:
                i_low_x, bias = True, -1
        f_smc[i] = bias

        # EmreKb zigzag trend leg
        prev_trend = trend_l
        if trend_l == 1 and to_down[i]:
            trend_l = -1
        elif trend_l == -1 and to_up[i]:
            trend_l = 1
        if trend_l != prev_trend:
            if trend_l == 1:      # uptrend starts → record the preceding LOW
                k = (i - 1) - last_up_bar if (last_up_bar is not None and last_up_bar <= i - 1) else 0
                win = k if k > 0 else 1
                l1, l0 = l0, float(np.min(la[max(0, i - win + 1):i + 1]))
            else:                 # downtrend starts → record the preceding HIGH
                k = (i - 1) - last_dn_bar if (last_dn_bar is not None and last_dn_bar <= i - 1) else 0
                win = k if k > 0 else 1
                h1, h0 = h0, float(np.max(ha[max(0, i - win + 1):i + 1]))

        # market flip with the fib buffer + same-swing guard
        prev_mkt = mkt
        if None not in (h0, h1, l0, l1):
            same_swing = (flip_l0 is not None and flip_l0 == l0) or \
                         (flip_h0 is not None and flip_h0 == h0)
            if not same_swing:
                if mkt == 1 and l0 < l1 and l0 < l1 - abs(h0 - l1) * FIB_FACTOR:
                    mkt = -1
                elif mkt == -1 and h0 > h1 and h0 > h1 + abs(h1 - l0) * FIB_FACTOR:
                    mkt = 1
        if mkt != prev_mkt:
            flip_l0, flip_h0 = l0, h0
            (msb_up if mkt == 1 else msb_dn)[i] = True
        market_arr[i] = mkt

        # barssince bookkeeping AFTER use (Pine reads the [1]-shifted series)
        if to_up[i]:
            last_up_bar = i
        if to_down[i]:
            last_dn_bar = i

    # ── score (float, unrounded — Pine compares the float) ──
    net = (f_stack * 20 + f_e200 * 15 + f_smc * 25 + f_vegas * 15
           + f_tun * 10 + f_vol * 10)                 # trendline (w5) neutral
    scores = np.clip(50 + net / 2.0, 0, 100)

    # ── loop 2: arm-and-fire flags with lastSig alternation ──
    flags = np.zeros(n, dtype=int)
    long_armed = short_armed = False
    last_sig = 0
    for i in range(n):
        if msb_up[i]:
            long_armed, short_armed = True, False
        if msb_dn[i]:
            short_armed, long_armed = True, False
        regime = adx_th <= 0 or (not np.isnan(adx[i]) and adx[i] >= adx_th)
        if long_armed and ca[i] > e200[i] and regime and scores[i] >= score_th and last_sig != 1:
            flags[i], last_sig, long_armed = 1, 1, False
        elif short_armed and ca[i] < e200[i] and regime and scores[i] <= 100 - score_th and last_sig != -1:
            flags[i], last_sig, short_armed = -1, -1, False

    last_flag = "long" if flags[-1] == 1 else "short" if flags[-1] == -1 else None
    return {
        "flag": last_flag,
        "score": float(scores[-1]),
        "vegas": int(f_vegas[-1]),
        "msb": "bull" if market_arr[-1] == 1 else "bear",
        "price": float(ca[-1]),
        "insufficient": n < MIN_CANDLES,
        "flags": flags,
        "scores": scores,
    }


def last_bar(ohlcv, score_th: int = 70, adx_th: int = 20) -> dict:
    """Scanner entry point — last-closed-bar summary only."""
    res = compute(ohlcv, score_th, adx_th)
    return {k: res[k] for k in ("flag", "score", "vegas", "msb", "price", "insufficient")}
