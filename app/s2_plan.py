"""
Strategy 2 — the Entry / SL / TP plan attached to every fired signal.

Pure arithmetic, no network, no config writes. These helpers used to live in
strategy2_live.py next to the code that could turn a signal into a real
order. That module is gone: the S2 triangle measures −0.081R ± 0.018 over
22,631 scored signals — an interval that EXCLUDES zero — and every one of the
six exit rules the outcome tracker replays is negative. A signal that has
been measured to lose money is a thing to publish with its record beside it,
not a thing to wire to an account. The plan maths stayed because the alert,
the /strategy2 page and the outcome tracker all still need the same levels.
"""
import config

ATR_PERIOD = 14


def atr(ohlcv, period: int = ATR_PERIOD):
    """Simple-average True Range over the closed candles (same maths as
    bot.calculate_atr). Returns None when there is not enough history."""
    if not ohlcv or len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def trade_levels(price, is_long: bool, atr, *, sl_mult=None, tp1_r=None, tp2_r=None):
    """entry / sl / tp1 / tp2 for a signal at `price`. ATR stop
    (config.ATR_SL_MULTIPLIER) capped at config.MAX_SL_PCT, targets at 1R / 2R —
    the same geometry as the S1 trade plan so the risk:reward is genuine per
    coin. The keyword overrides exist for premium_trade_levels below.

    `atr` is the value (see atr() above for the calculation); the parameter
    keeps that name because callers pass it by keyword."""
    entry = float(price)
    sl_mult = sl_mult if sl_mult is not None else config.ATR_SL_MULTIPLIER
    tp1_r = tp1_r if tp1_r is not None else config.ATR_TP1_MULTIPLIER
    tp2_r = tp2_r if tp2_r is not None else config.ATR_TP_MULTIPLIER
    if atr:
        sl_distance = atr * sl_mult
        sl = entry - sl_distance if is_long else entry + sl_distance
    else:
        sl = entry * (1 - config.FIXED_SL_PCT) if is_long else entry * (1 + config.FIXED_SL_PCT)

    # Cap the stop distance so one bad coin can't risk more than MAX_SL_PCT.
    if is_long and (entry - sl) / entry > config.MAX_SL_PCT:
        sl = entry * (1 - config.MAX_SL_PCT)
    elif (not is_long) and (sl - entry) / entry > config.MAX_SL_PCT:
        sl = entry * (1 + config.MAX_SL_PCT)

    # Guard against a degenerate stop on the wrong side of entry.
    if is_long and sl >= entry:
        sl = entry * (1 - config.MAX_SL_PCT)
    elif (not is_long) and sl <= entry:
        sl = entry * (1 + config.MAX_SL_PCT)

    risk = abs(entry - sl)
    if is_long:
        tp1 = entry + risk * tp1_r
        tp2 = entry + risk * tp2_r
    else:
        tp1 = entry - risk * tp1_r
        tp2 = entry - risk * tp2_r
    return entry, sl, tp1, tp2


def premium_trade_levels(price, is_long: bool, atr):
    """The ⭐ premium plan published to the Signals topic — SL 2×ATR, TP1 at
    0.75R, TP2 2R runner (config-tunable). Chosen from the 60d replay grid:
    58.7% of gate-passing signals reached this TP1 before the stop, vs ~50%
    for the old 1R-on-1.5×ATR plan. Alert / page / outcome-tracker only."""
    return trade_levels(price, is_long, atr,
                        sl_mult=config.STRATEGY2_PREMIUM_SL_MULT,
                        tp1_r=config.STRATEGY2_PREMIUM_TP1_R,
                        tp2_r=config.STRATEGY2_PREMIUM_TP2_R)
