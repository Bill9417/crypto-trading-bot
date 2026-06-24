import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from config import RSI_PERIOD, RSI_UPPER_THRESHOLD, RSI_LOWER_THRESHOLD

# TSI defaults from TV/pine/TSI.pine
TSI_LONG_LENGTH = 5
TSI_SHORT_LENGTH = 12
TSI_SIGNAL_LENGTH = 10
TSI_LOOKBACK_BARS = 70
TSI_BUY_PROB = 0.12
TSI_SELL_PROB = 0.12
TSI_PIVOT_LEFT = 20
TSI_PIVOT_RIGHT = 1


def add_indicators(df):
    rsi = RSIIndicator(close=df["close"], window=RSI_PERIOD)
    df["rsi"] = rsi.rsi()
    return df


def calculate_ema(prices, period):
    """Calculate Exponential Moving Average (EMA)
    :param prices: list of prices
    :param period: EMA period
    :return: last EMA value or None if insufficient data
    """
    if len(prices) < period:
        return None
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]


def calculate_vwap(ohlcv, period=24):
    """Rolling Volume-Weighted Average Price over the last `period` bars.
    VWAP = Σ(typical_price × volume) / Σ(volume), typical = (high+low+close)/3.
    Acts as a dynamic fair-value line: price above VWAP = buyers in control.
    Returns the latest VWAP value, or None if insufficient data/volume.
    """
    if len(ohlcv) < period:
        return None
    window = ohlcv[-period:]
    pv = 0.0
    vol = 0.0
    for c in window:
        typical = (c[2] + c[3] + c[4]) / 3.0  # (high + low + close) / 3
        pv += typical * c[5]
        vol += c[5]
    if vol <= 0:
        return None
    return pv / vol


def adx_components(ohlcv, period=14):
    """Average Directional Index (Wilder) on a list of OHLCV candles.

    ADX measures trend STRENGTH, not direction: a low ADX (< ~20) means
    chop/range, a high or rising ADX means a real, persistent trend. Filtering
    EMA-cross entries on ADX is a well-documented way to cut the losing trades
    that fire in ranging markets.

    Returns a dict {"adx", "adx_prev", "plus_di", "minus_di", "rising"} using
    the latest closed-candle values, or None if there isn't enough data.
    """
    if ohlcv is None or len(ohlcv) < period * 2 + 1:
        return None
    highs = np.array([c[2] for c in ohlcv], dtype=float)
    lows = np.array([c[3] for c in ohlcv], dtype=float)
    closes = np.array([c[4] for c in ohlcv], dtype=float)

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )

    # Wilder's smoothing == an EMA with alpha = 1/period (adjust=False).
    alpha = 1.0 / period
    atr = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean()
    atr_safe = atr.replace(0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean() / atr_safe
    minus_di = 100.0 * pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean() / atr_safe
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    adx_now = adx.iloc[-1]
    if pd.isna(adx_now):
        return None
    adx_prev = adx.iloc[-2] if len(adx) >= 2 else np.nan
    pdi, mdi = plus_di.iloc[-1], minus_di.iloc[-1]
    return {
        "adx": float(adx_now),
        "adx_prev": (None if pd.isna(adx_prev) else float(adx_prev)),
        "plus_di": (None if pd.isna(pdi) else float(pdi)),
        "minus_di": (None if pd.isna(mdi) else float(mdi)),
        "rising": (not pd.isna(adx_prev)) and float(adx_now) > float(adx_prev),
    }


def calculate_adx(ohlcv, period=14):
    """Latest ADX value (trend strength) or None if insufficient data."""
    res = adx_components(ohlcv, period)
    return res["adx"] if res else None


def check_volume_gate(volumes, multiplier=1.5, lookback=20):
    """Check if signal candle has volume >= multiplier of average volume
    :param volumes: list of volume values
    :param multiplier: required volume multiplier
    :param lookback: lookback period for average
    :return: bool (True if volume gate passed)
    """
    if len(volumes) < lookback +1:
        return False
    avg_vol = np.mean(volumes[-lookback-1:-1])  # average up to previous candle
    current_vol = volumes[-1]
    return current_vol >= avg_vol * multiplier


def check_rsi_cross_after_extreme(rsi_values, is_long):
    """Check if RSI crossed back above/below 50 after extreme
    :param rsi_values: list of RSI values (most recent last)
    :param is_long: True if checking for LONG, False for SHORT
    :return: bool
    """
    if len(rsi_values) < 10:
        return False
    # First check if we had an extreme recently
    had_extreme = False
    if is_long:
        had_extreme = any(r <= RSI_LOWER_THRESHOLD for r in rsi_values[-10:])
    else:
        had_extreme = any(r >= RSI_UPPER_THRESHOLD for r in rsi_values[-10:])
    if not had_extreme:
        return False
    # Now check if RSI crossed back
    if is_long:
        return rsi_values[-1] > 50 and any(r <= 50 for r in rsi_values[-5:-1])
    else:
        return rsi_values[-1] < 50 and any(r >= 50 for r in rsi_values[-5:-1])


def check_stoch_rsi_signal(
    prices,
    rsi_period=14,
    stoch_period=14,
    k_smooth=3,
    d_smooth=3,
    oversold=20,
    overbought=80,
):
    """
    L1: Stochastic RSI — momentum-on-momentum signal tuned for altcoins.

    Fires bullish when K is in the oversold zone (<oversold) or crosses above D
    from that zone.  Fires bearish when K is in the overbought zone (>overbought)
    or crosses below D from that zone.

    Returns (is_active, detail_text, bullish_hint).
      bullish_hint: True = bullish setup, False = bearish, None = conflicted.
    """
    needed = rsi_period + stoch_period + k_smooth + d_smooth + 5
    if len(prices) < needed:
        return False, "", None

    s = pd.Series(prices, dtype=float)

    # Wilder-smoothed RSI
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=rsi_period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=rsi_period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # Stochastic of RSI
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    rng = (rsi_max - rsi_min).replace(0, np.nan)
    stoch_raw = 100 * (rsi - rsi_min) / rng

    k = stoch_raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()

    k_arr = k.to_numpy()
    d_arr = d.to_numpy()

    # Operate on the confirmed (penultimate) candle
    idx = len(k_arr) - 2
    if idx < 2:
        return False, "", None

    k_cur, d_cur = k_arr[idx], d_arr[idx]
    k_prev, d_prev = k_arr[idx - 1], d_arr[idx - 1]

    if any(np.isnan(x) for x in (k_cur, d_cur, k_prev, d_prev)):
        return False, "", None

    in_oversold = k_cur < oversold
    in_overbought = k_cur > overbought
    bull_cross = k_prev < d_prev and k_cur > d_cur and k_cur < oversold + 5
    bear_cross = k_prev > d_prev and k_cur < d_cur and k_cur > overbought - 5

    bullish = in_oversold or bull_cross
    bearish = in_overbought or bear_cross

    if not (bullish or bearish):
        return False, "", None

    parts = []
    if in_oversold and bull_cross:
        parts.append(f"StochRSI Bull Cross (K={k_cur:.1f} D={d_cur:.1f})")
    elif in_oversold:
        parts.append(f"StochRSI Oversold (K={k_cur:.1f})")
    elif bull_cross:
        parts.append(f"StochRSI K×D Up (K={k_cur:.1f})")

    if in_overbought and bear_cross:
        parts.append(f"StochRSI Bear Cross (K={k_cur:.1f} D={d_cur:.1f})")
    elif in_overbought:
        parts.append(f"StochRSI Overbought (K={k_cur:.1f})")
    elif bear_cross:
        parts.append(f"StochRSI K×D Down (K={k_cur:.1f})")

    if bullish and not bearish:
        hint = True
    elif bearish and not bullish:
        hint = False
    else:
        hint = None

    return True, ", ".join(parts), hint


def check_hidden_divergence(prices, rsi_values, is_long):
    """Check hidden bullish/bearish divergence
    Hidden bullish: higher low in RSI, lower low in price
    Hidden bearish: lower high in RSI, higher high in price
    :param prices: list of prices
    :param rsi_values: list of RSI values
    :param is_long: True for bullish, False for bearish
    :return: bool
    """
    lookback = 20
    if len(prices) < lookback or len(rsi_values) < lookback:
        return False
    # Get indices of recent swing lows/highs
    def get_swings(vals, n=3):
        swings = []
        # Iterate backwards to find the most recent swings first, stop once we have 2
        for i in range(len(vals)-n-1, n-1, -1):
            if is_long:
                if vals[i] < min(vals[i-n:i]) and vals[i] < min(vals[i+1:i+n+1]):
                    swings.insert(0, (i, vals[i]))  # Insert at beginning to keep order
                    if len(swings) == 2:
                        break
            else:
                if vals[i] > max(vals[i-n:i]) and vals[i] > max(vals[i+1:i+n+1]):
                    swings.insert(0, (i, vals[i]))
                    if len(swings) == 2:
                        break
        return swings
    price_swings = get_swings(prices)
    rsi_swings = get_swings(rsi_values)
    if len(price_swings) <2 or len(rsi_swings)<2:
        return False
    if is_long:
        price_lower_low = price_swings[-1][1] < price_swings[-2][1]
        rsi_higher_low = rsi_swings[-1][1] > rsi_swings[-2][1]
        return price_lower_low and rsi_higher_low
    else:
        price_higher_high = price_swings[-1][1] > price_swings[-2][1]
        rsi_lower_high = rsi_swings[-1][1] < rsi_swings[-2][1]
        return price_higher_high and rsi_lower_high


def _double_smooth(series: pd.Series, long_period: int, short_period: int) -> pd.Series:
    first = series.ewm(span=long_period, adjust=False).mean()
    return first.ewm(span=short_period, adjust=False).mean()


def _wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)

    def weighted_mean(window):
        return np.dot(window, weights) / weights.sum()

    return series.rolling(period).apply(weighted_mean, raw=True)


def _dzsell(src: np.ndarray, initvalue: float, lookbackbars: int) -> float:
    left, right = -10000.0, 10000.0
    eps = 0.001
    yval = (left + right) / 2.0
    delta = yval - left

    for _ in range(50):
        if not (delta > 0.005):
            break
        count = sum(1 for k in range(lookbackbars) if src[k] > yval)
        prob = count / lookbackbars
        if prob > initvalue + eps:
            left = yval
            yval = (yval + right) / 2.0
        elif prob < initvalue - eps:
            right = yval
            yval = (yval + left) / 2.0
        else:
            left = yval
            yval = (yval + right) / 2.0
        delta = yval - left
    return yval


def _dzbuy(src: np.ndarray, initvalue: float, lookbackbars: int) -> float:
    left, right = -10000.0, 10000.0
    eps = 0.001
    yval = (left + right) / 2.0
    delta = yval - left

    for _ in range(50):
        if not (delta > 0.005):
            break
        count = sum(1 for k in range(lookbackbars) if src[k] < yval)
        prob = count / lookbackbars
        if prob > initvalue + eps:
            right = yval
            yval = (yval + left) / 2.0
        elif prob < initvalue - eps:
            left = yval
            yval = (yval + right) / 2.0
        else:
            right = yval
            yval = (yval + left) / 2.0
        delta = yval - left
    return yval


def calculate_tsi(ohlcv: list) -> dict | None:
    """Calculate TSI values from OHLCV candles (matches TV/pine/TSI.pine)."""
    if len(ohlcv) < TSI_LOOKBACK_BARS + TSI_PIVOT_LEFT + 5:
        return None

    ohlc4 = np.array(
        [(c[1] + c[2] + c[3] + c[4]) / 4 for c in ohlcv],
        dtype=float,
    )
    price = pd.Series(ohlc4)
    pc = price.diff().fillna(0)

    ds_pc = _double_smooth(pc, TSI_LONG_LENGTH, TSI_SHORT_LENGTH)
    ds_abs_pc = _double_smooth(pc.abs(), TSI_LONG_LENGTH, TSI_SHORT_LENGTH)
    tsi = 100 * (ds_pc / ds_abs_pc.replace(0, np.nan))
    tsi_signal = _wma(tsi, TSI_SIGNAL_LENGTH)

    tsi_arr = tsi.to_numpy()
    signal_arr = tsi_signal.to_numpy()

    sell_zone = _dzsell(tsi_arr[::-1], TSI_SELL_PROB, TSI_LOOKBACK_BARS)
    buy_zone = _dzbuy(tsi_arr[::-1], TSI_BUY_PROB, TSI_LOOKBACK_BARS)

    return {
        "tsi": tsi_arr,
        "tsi_signal": signal_arr,
        "buy_zone": buy_zone,
        "sell_zone": sell_zone,
    }


def _pivot_low(values: np.ndarray, left: int, right: int, idx: int) -> bool:
    if idx < left or idx >= len(values) - right:
        return False
    window = values[idx - left : idx + right + 1]
    return values[idx] == np.min(window)


def _pivot_high(values: np.ndarray, left: int, right: int, idx: int) -> bool:
    if idx < left or idx >= len(values) - right:
        return False
    window = values[idx - left : idx + right + 1]
    return values[idx] == np.max(window)


def _detect_tsi_cross_signals(tsi: np.ndarray, tsi_signal: np.ndarray) -> tuple[bool, bool]:
    """Detect crossbuymaybe / crosssellmaybe from TSI.pine."""
    n = len(tsi)
    if n < 3:
        return False, False

    cross_ph_values = []
    cross_pl_values = []

    for i in range(1, n):
        prev_tsi, curr_tsi = tsi[i - 1], tsi[i]
        prev_sig, curr_sig = tsi_signal[i - 1], tsi_signal[i]

        if np.isnan(curr_tsi) or np.isnan(curr_sig) or np.isnan(prev_tsi) or np.isnan(prev_sig):
            continue

        if curr_tsi < 0 and prev_tsi >= prev_sig and curr_tsi < curr_sig:
            cross_ph_values.append(curr_sig)
        if curr_tsi > 0 and prev_tsi <= prev_sig and curr_tsi > curr_sig:
            cross_pl_values.append(curr_sig)

    cross_sell = False
    cross_buy = False

    if len(cross_ph_values) >= 2 and cross_ph_values[-1] < cross_ph_values[-2]:
        cross_sell = True
    if len(cross_pl_values) >= 2 and cross_pl_values[-1] > cross_pl_values[-2]:
        cross_buy = True

    i = n - 2
    if i >= 1:
        prev_tsi, curr_tsi = tsi[i - 1], tsi[i]
        prev_sig, curr_sig = tsi_signal[i - 1], tsi_signal[i]
        if not any(np.isnan(x) for x in (prev_tsi, curr_tsi, prev_sig, curr_sig)):
            if curr_tsi < 0 and prev_tsi >= prev_sig and curr_tsi < curr_sig:
                cross_sell = True
            if curr_tsi > 0 and prev_tsi <= prev_sig and curr_tsi > curr_sig:
                cross_buy = True

    return cross_buy, cross_sell


def check_tsi_signal(ohlcv: list) -> tuple[bool, str, bool | None]:
    """
    Returns (is_active, detail_text, bullish_hint).
    bullish_hint: True=bullish, False=bearish, None=neutral.
    """
    data = calculate_tsi(ohlcv)
    if data is None:
        return False, "", None

    tsi = data["tsi"]
    tsi_signal = data["tsi_signal"]
    idx = len(tsi) - 2

    if idx < 1 or np.isnan(tsi[idx]):
        return False, "", None

    current_tsi = tsi[idx]
    cross_buy, cross_sell = _detect_tsi_cross_signals(tsi, tsi_signal)

    pivot_low = _pivot_low(tsi, TSI_PIVOT_LEFT, TSI_PIVOT_RIGHT, idx) and current_tsi < 50
    pivot_high = _pivot_high(tsi, TSI_PIVOT_LEFT, TSI_PIVOT_RIGHT, idx) and current_tsi > 50

    in_buy_zone = current_tsi <= data["buy_zone"]
    in_sell_zone = current_tsi >= data["sell_zone"]
    oversold = current_tsi <= -75
    overbought = current_tsi >= 75

    bullish = cross_buy or pivot_low or in_buy_zone or oversold
    bearish = cross_sell or pivot_high or in_sell_zone or overbought

    if not (bullish or bearish):
        return False, "", None

    details = []
    if cross_buy:
        details.append("TSI Bull Cross")
    if cross_sell:
        details.append("TSI Bear Cross")
    if pivot_low:
        details.append("TSI Pivot Low")
    if pivot_high:
        details.append("TSI Pivot High")
    if in_buy_zone:
        details.append("TSI Buy Zone")
    if in_sell_zone:
        details.append("TSI Sell Zone")
    if oversold:
        details.append("TSI Oversold")
    if overbought:
        details.append("TSI Overbought")

    bull_score = sum([cross_buy, pivot_low, in_buy_zone, oversold])
    bear_score = sum([cross_sell, pivot_high, in_sell_zone, overbought])
    if bull_score > bear_score:
        hint = True
    elif bear_score > bull_score:
        hint = False
    else:
        hint = None
    return True, ", ".join(details), hint


def calculate_volume_profile(ohlcv, num_bins=20, lookback=50):
    """Simplified Volume Profile over the last `lookback` candles.
    Distributes each candle's volume across price bins proportional to the
    overlap between the candle's range and each bin, then finds the POC
    (highest-volume bin) and expands to the 70% Value Area (VAH/VAL).
    Returns dict with poc, vah, val, in_value_area, above_vah, below_val, poc_dist_pct.
    """
    if len(ohlcv) < lookback:
        return None
    window = ohlcv[-lookback:]
    highs  = [c[2] for c in window]
    lows   = [c[3] for c in window]
    closes = [c[4] for c in window]
    vols   = [c[5] for c in window]

    price_high = max(highs)
    price_low  = min(lows)
    if price_high <= price_low:
        return None

    bin_size = (price_high - price_low) / num_bins
    bin_vol  = [0.0] * num_bins
    for i in range(len(window)):
        h, l, v = highs[i], lows[i], vols[i]
        rng = h - l if h > l else bin_size * 0.01
        for b in range(num_bins):
            bl = price_low + b * bin_size
            bh = bl + bin_size
            overlap = max(0.0, min(h, bh) - max(l, bl))
            if overlap > 0:
                bin_vol[b] += v * overlap / rng

    poc_bin = bin_vol.index(max(bin_vol))
    poc = price_low + (poc_bin + 0.5) * bin_size

    total_vol = sum(bin_vol)
    target    = total_vol * 0.70
    va_vol    = bin_vol[poc_bin]
    lo_idx, hi_idx = poc_bin, poc_bin
    while va_vol < target and (lo_idx > 0 or hi_idx < num_bins - 1):
        lo_add = bin_vol[lo_idx - 1] if lo_idx > 0 else 0.0
        hi_add = bin_vol[hi_idx + 1] if hi_idx < num_bins - 1 else 0.0
        if lo_idx == 0:
            hi_idx += 1; va_vol += hi_add
        elif hi_idx == num_bins - 1:
            lo_idx -= 1; va_vol += lo_add
        elif lo_add >= hi_add:
            lo_idx -= 1; va_vol += lo_add
        else:
            hi_idx += 1; va_vol += hi_add

    val   = price_low + lo_idx * bin_size
    vah   = price_low + (hi_idx + 1) * bin_size
    price = closes[-1]
    poc_dist_pct = abs(price - poc) / price * 100 if price else 0.0

    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "poc_dist_pct": poc_dist_pct,
        "in_value_area": val <= price <= vah,
        "above_vah": price > vah,
        "below_val": price < val,
    }


def calculate_order_flow(ohlcv, lookback=20):
    """Order-flow proxy from OHLCV candles.
    Delta per candle = buy_vol - sell_vol, where:
      buy_vol  = (close - low) / range × volume
      sell_vol = (high - close) / range × volume
    Also detects absorption (high-vol, small-body candles) and sequential imbalance.
    Returns dict with bullish/bearish booleans and underlying metrics.
    """
    if len(ohlcv) < lookback + 1:
        return None
    window = ohlcv[-lookback:]

    deltas = []
    for c in window:
        h, l, close, v = c[2], c[3], c[4], c[5]
        rng = h - l
        if rng <= 0:
            deltas.append(0.0)
        else:
            deltas.append(((close - l) - (h - close)) / rng * v)

    cum_delta    = sum(deltas)
    recent_delta = sum(deltas[-5:])

    avg_vol = float(np.mean([c[5] for c in window]))
    absorption_bull = absorption_bear = False
    for c in window[-5:]:
        o, h, l, close, v = c[1], c[2], c[3], c[4], c[5]
        rng = h - l
        if rng <= 0 or v < avg_vol * 2:
            continue
        body = abs(close - o)
        if body / rng < 0.35:
            if close >= l + 0.6 * rng:
                absorption_bull = True
            else:
                absorption_bear = True

    imb_bull = imb_bear = False
    if len(deltas) >= 3:
        d = deltas[-3:]
        if all(x > 0 for x in d) and d[0] <= d[1] <= d[2]:
            imb_bull = True
        if all(x < 0 for x in d) and d[0] >= d[1] >= d[2]:
            imb_bear = True

    return {
        "cumulative_delta": cum_delta,
        "recent_delta": recent_delta,
        "absorption_bull": absorption_bull,
        "absorption_bear": absorption_bear,
        "imbalance_bull": imb_bull,
        "imbalance_bear": imb_bear,
        "bullish": cum_delta > 0 and (recent_delta > 0 or absorption_bull or imb_bull),
        "bearish": cum_delta < 0 and (recent_delta < 0 or absorption_bear or imb_bear),
    }


def calculate_macd(prices, fast=12, slow=26, signal=9):
    """
    Calculate MACD, signal line, and histogram.
    Returns (macd_line, signal_line, histogram) as numpy arrays, or (None, None, None)
    if insufficient data.
    """
    if len(prices) < slow + signal:
        return None, None, None

    s = pd.Series(prices, dtype=float)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return macd_line.to_numpy(), signal_line.to_numpy(), histogram.to_numpy()


def check_macd_signal(prices):
    """
    Strategy Light 5: MACD Momentum Confirmation.

    Returns (is_active, detail_text, bullish_hint).
    - Bullish: MACD histogram positive AND increasing (momentum building)
    - Bearish: MACD histogram negative AND decreasing (momentum building)
    """
    macd_line, signal_line, histogram = calculate_macd(prices)
    if histogram is None or len(histogram) < 3:
        return False, "", None

    curr_hist = histogram[-1]
    prev_hist = histogram[-2]
    prev2_hist = histogram[-3]

    # Check for NaN
    if any(np.isnan(x) for x in (curr_hist, prev_hist, prev2_hist)):
        return False, "", None

    details = []
    bullish = False
    bearish = False

    # Bullish: histogram positive and increasing for 2 bars
    if curr_hist > 0 and curr_hist > prev_hist and prev_hist > prev2_hist:
        bullish = True
        details.append("MACD Histogram Rising")
    # Also bullish: histogram crossing from negative to positive
    elif prev_hist < 0 and curr_hist > 0:
        bullish = True
        details.append("MACD Histogram Cross Up")

    # Bearish: histogram negative and decreasing for 2 bars
    if curr_hist < 0 and curr_hist < prev_hist and prev_hist < prev2_hist:
        bearish = True
        details.append("MACD Histogram Falling")
    # Also bearish: histogram crossing from positive to negative
    elif prev_hist > 0 and curr_hist < 0:
        bearish = True
        details.append("MACD Histogram Cross Down")

    if not (bullish or bearish):
        return False, "", None

    if bullish and not bearish:
        hint = True
    elif bearish and not bullish:
        hint = False
    else:
        hint = None

    return True, ", ".join(details), hint
