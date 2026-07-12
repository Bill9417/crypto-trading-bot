"""
SMC (Smart Money Concepts) analysis based on TV/pine/SMC.pine (LuxAlgo).
Detects premium/discount zones, swing structure, order-block proximity, CHoCH, FVG, liquidity sweeps
on the scan timeframe (currently 1h — runs on whatever candles are passed in).
"""

import numpy as np

BULLISH = 1
BEARISH = -1
SWING_LENGTH = 50
INTERNAL_LENGTH = 5


def _leg(highs: np.ndarray, lows: np.ndarray, idx: int, size: int) -> int:
    if idx < size:
        return 0
    window_high = highs[idx - size + 1 : idx + 1]
    window_low = lows[idx - size + 1 : idx + 1]
    pivot_high = highs[idx - size]
    pivot_low = lows[idx - size]

    if pivot_high > np.max(window_high):
        return 0  # bearish leg start (swing high)
    if pivot_low < np.min(window_low):
        return 1  # bullish leg start (swing low)
    return -1  # no change


def _find_swing_pivots(highs: np.ndarray, lows: np.ndarray, size: int) -> tuple[list, list]:
    swing_highs = []
    swing_lows = []

    prev_leg = None
    for idx in range(size, len(highs)):
        leg = _leg(highs, lows, idx, size)
        if leg == -1:
            continue
        if leg != prev_leg and prev_leg is not None:
            if prev_leg == 1:  # was bullish leg → end is a swing HIGH
                swing_highs.append((idx - size, highs[idx - size]))
            else:              # was bearish leg → end is a swing LOW
                swing_lows.append((idx - size, lows[idx - size]))
        prev_leg = leg if leg in (0, 1) else prev_leg

    return swing_highs, swing_lows


def _swing_trend(closes: np.ndarray, swing_highs: list, swing_lows: list) -> int:
    if not swing_highs or not swing_lows:
        return 0

    last_high = swing_highs[-1][1]
    prev_high = swing_highs[-2][1] if len(swing_highs) > 1 else last_high
    last_low = swing_lows[-1][1]
    prev_low = swing_lows[-2][1] if len(swing_lows) > 1 else last_low

    hh = last_high > prev_high
    hl = last_low > prev_low
    lh = last_high < prev_high
    ll = last_low < prev_low

    if hh and hl:
        return BULLISH
    if lh and ll:
        return BEARISH
    if hh and ll:
        return BEARISH
    if lh and hl:
        return BULLISH
    return 0


def _premium_discount_zones(swing_high: float, swing_low: float) -> dict:
    """ICT/LuxAlgo premium/discount zone boundaries.

    Splits the swing range so the lower SMC_DISCOUNT_MAX_PCT is 'discount'
    (ideal for longs), the upper (1 - SMC_PREMIUM_MIN_PCT) is 'premium' (ideal
    for shorts), and the band between is 'equilibrium'. The old version used a
    5% sliver, which mislabelled ~90% of price as equilibrium.
    """
    try:
        from config import SMC_DISCOUNT_MAX_PCT, SMC_PREMIUM_MIN_PCT
        disc_pct, prem_pct = SMC_DISCOUNT_MAX_PCT, SMC_PREMIUM_MIN_PCT
    except Exception:
        disc_pct, prem_pct = 0.40, 0.60
    rng = swing_high - swing_low
    premium_top = swing_high
    premium_bottom = swing_low + prem_pct * rng      # top (1-prem_pct) of range
    discount_top = swing_low + disc_pct * rng        # bottom disc_pct of range
    discount_bottom = swing_low
    equilibrium = (swing_high + swing_low) / 2
    return {
        "premium_top": premium_top,
        "premium_bottom": premium_bottom,
        "discount_top": discount_top,
        "discount_bottom": discount_bottom,
        "equilibrium": equilibrium,
    }


def _find_order_blocks(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                       closes: np.ndarray, lookback: int = 30) -> tuple[list, list]:
    """Simplified order-block detection from recent structure."""
    bullish_obs = []
    bearish_obs = []

    start = max(0, len(closes) - lookback)
    for i in range(start + 1, len(closes) - 1):
        # Bullish OB: down candle before upward impulse
        if closes[i] < opens[i] and closes[i + 1] > highs[i]:
            bullish_obs.append((lows[i], highs[i]))
        # Bearish OB: up candle before downward impulse
        if closes[i] > opens[i] and closes[i + 1] < lows[i]:
            bearish_obs.append((lows[i], highs[i]))

    return bullish_obs[-3:], bearish_obs[-3:]


def _price_in_zone(price: float, low: float, high: float) -> bool:
    return low <= price <= high


def _nearest_ob_distance(price: float, obs: list) -> tuple[float | None, tuple | None]:
    best_dist = None
    best_ob = None
    for ob_low, ob_high in obs:
        if ob_low <= price <= ob_high:
            return 0.0, (ob_low, ob_high)
        dist = min(abs(price - ob_low), abs(price - ob_high))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_ob = (ob_low, ob_high)
    return best_dist, best_ob


def check_liquidity_sweep(highs, lows, swing_highs, swing_lows, closes, is_long):
    """Check if there was a recent liquidity sweep (wick outside swing, then closes back inside)
    :param highs: np.array of highs
    :param lows: np.array of lows
    :param swing_highs: list of swing high tuples (index, price)
    :param swing_lows: list of swing low tuples (index, price)
    :param closes: np.array of closes
    :param is_long: True if checking for bullish sweep, False for bearish
    :return: bool
    """
    lookback = 10
    if len(highs) < lookback:
        return False
    if is_long:
        if not swing_lows:
            return False
        last_swing_low = swing_lows[-1][1]
        # Check if any recent candle wick went below swing low, then closed back above
        for i in range(-lookback, -1):
            if lows[i] < last_swing_low and closes[i] > last_swing_low:
                return True
    else:
        if not swing_highs:
            return False
        last_swing_high = swing_highs[-1][1]
        for i in range(-lookback, -1):
            if highs[i] > last_swing_high and closes[i] < last_swing_high:
                return True
    return False


def check_choch(highs, lows, swing_highs, swing_lows, closes):
    """Check Change of Character (CHoCH): first break of swing high/low after trend
    :param highs: np.array of highs
    :param lows: np.array of lows
    :param swing_highs: list of swing high tuples
    :param swing_lows: list of swing low tuples
    :param closes: np.array of closes
    :return: tuple (bullish_choch, bearish_choch)
    """
    lookback = 20
    if len(swing_highs) <2 or len(swing_lows)<2:
        return False, False
    bullish_choch = False
    bearish_choch = False
    # Bullish CHoCH: close above last swing high after downtrend (lower lows, lower highs)
    prev_swing_high = swing_highs[-2][1]
    if closes[-1] > prev_swing_high and lows[-1] > swing_lows[-1][1]:
        bullish_choch = True
    # Bearish CHoCH: close below last swing low after uptrend
    prev_swing_low = swing_lows[-2][1]
    if closes[-1] < prev_swing_low and highs[-1] < swing_highs[-1][1]:
        bearish_choch = True
    return bullish_choch, bearish_choch


def find_fvg(highs, lows, closes):
    """Find recent Fair Value Gap
    Bullish FVG: low[i+2] > high[i]
    Bearish FVG: high[i+2] < low[i]
    :param highs: np.array of highs
    :param lows: np.array of lows
    :return: dict with 'type', 'high', 'low', 'midpoint', or None
    """
    lookback = 20
    if len(highs) < 3:
        return None
    for i in range(len(highs)-3, max(0, len(highs)-lookback-1), -1):
        # Bullish FVG: low of i+2 > high of i
        if lows[i+2] > highs[i]:
            fvg_high = lows[i+2]
            fvg_low = highs[i]
            midpoint = (fvg_high + fvg_low)/2
            return {
                "type": "bullish",
                "high": fvg_high,
                "low": fvg_low,
                "midpoint": midpoint
            }
        # Bearish FVG: high of i+2 < low of i
        if highs[i+2] < lows[i]:
            fvg_high = lows[i]
            fvg_low = highs[i+2]
            midpoint = (fvg_high + fvg_low)/2
            return {
                "type": "bearish",
                "high": fvg_high,
                "low": fvg_low,
                "midpoint": midpoint
            }
    return None


def grade_ob_proximity(price, ob, ob_type):
    """Grade order block proximity
    :param price: current price
    :param ob: tuple (ob_low, ob_high)
    :param ob_type: "bullish" or "bearish"
    :return: grade (int: +2, +1, 0, -1, -2), and details string
    """
    if not ob:
        return 0, "No relevant OB"
    ob_low, ob_high = ob
    # Check if OB is mitigated (price closed through it)
    # For simplicity, check if price is outside OB
    if (ob_type == "bullish" and price > ob_high) or (ob_type == "bearish" and price < ob_low):
        return -2, "OB mitigated"
    # Calculate distance
    dist_pct = 0.0
    if ob_low <= price <= ob_high:
        dist_pct = 0.0
    else:
        if ob_type == "bullish":
            dist_pct = abs(price - ob_low)/price *100 if price else 0
        else:
            dist_pct = abs(price - ob_high)/price *100 if price else 0
    # Grade: 0.5% or less: +2; 1% or less: +1; else 0
    if dist_pct <=0.5:
        return +2, "OB very close (<=0.5%)"
    elif dist_pct <=1.0:
        return +1, "OB close (<=1.0%)"
    else:
        return 0, "OB distant"


def analyze_smc(ohlcv: list) -> dict:
    """
    Analyze SMC context (on the scan timeframe, currently 1h) for support/resistance zones.
    Returns zone info, human-readable details, CHoCH, FVG, liquidity sweep info.
    """
    if len(ohlcv) < SWING_LENGTH + 20:
        return {
            "zone_type": "neutral",
            "zone_label": "N/A",
            "summary": "Insufficient data",
            "details": [],
            "swing_high": None,
            "swing_low": None,
            "trend": "neutral",
            "dist_to_high_pct": None,
            "dist_to_low_pct": None,
            "liquidity_sweep_bullish": False,
            "liquidity_sweep_bearish": False,
            "choch_bullish": False,
            "choch_bearish": False,
            "fvg": None,
            "ob_grade_bullish": 0,
            "ob_grade_bearish":0,
        }

    opens = np.array([c[1] for c in ohlcv], dtype=float)
    highs = np.array([c[2] for c in ohlcv], dtype=float)
    lows = np.array([c[3] for c in ohlcv], dtype=float)
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    price = closes[-1]

    swing_highs, swing_lows = _find_swing_pivots(highs, lows, SWING_LENGTH)

    if not swing_highs or not swing_lows:
        trailing_high = float(np.max(highs[-SWING_LENGTH:]))
        trailing_low = float(np.min(lows[-SWING_LENGTH:]))
    else:
        trailing_high = swing_highs[-1][1]
        trailing_low = swing_lows[-1][1]

    zones = _premium_discount_zones(trailing_high, trailing_low)
    trend = _swing_trend(closes, swing_highs, swing_lows)
    bullish_obs, bearish_obs = _find_order_blocks(opens, highs, lows, closes)

    in_premium = _price_in_zone(price, zones["premium_bottom"], zones["premium_top"])
    in_discount = _price_in_zone(price, zones["discount_bottom"], zones["discount_top"])
    in_equilibrium = _price_in_zone(price, zones["discount_top"], zones["premium_bottom"])

    bull_ob_dist, bull_ob = _nearest_ob_distance(price, bullish_obs)
    bear_ob_dist, bear_ob = _nearest_ob_distance(price, bearish_obs)

    # New SMC checks
    liquidity_sweep_bullish = check_liquidity_sweep(highs, lows, swing_highs, swing_lows, closes, is_long=True)
    liquidity_sweep_bearish = check_liquidity_sweep(highs, lows, swing_highs, swing_lows, closes, is_long=False)
    choch_bullish, choch_bearish = check_choch(highs, lows, swing_highs, swing_lows, closes)
    fvg = find_fvg(highs, lows, closes)
    ob_grade_bullish, _ = grade_ob_proximity(price, bull_ob, "bullish") if bull_ob else (0, "No bullish OB")
    ob_grade_bearish, _ = grade_ob_proximity(price, bear_ob, "bearish") if bear_ob else (0, "No bearish OB")

    details = []
    zone_type = "neutral"
    zone_label = "Mid Range"
    summary = "Price between SMC zones"

    range_size = trailing_high - trailing_low
    dist_to_high_pct = ((trailing_high - price) / price * 100) if price else 0
    dist_to_low_pct = ((price - trailing_low) / price * 100) if price else 0

    if in_discount:
        zone_type = "support"
        zone_label = "Discount Zone"
        summary = "Price in SMC Support (Discount)"
        details.append("LuxAlgo Discount Zone")
        if trend == BULLISH:
            details.append("Swing trend: Bullish")
        elif trend == BEARISH:
            details.append("Swing trend: Bearish")
        if bull_ob_dist == 0:
            details.append("Inside Bullish Order Block")
        elif bull_ob_dist is not None and bull_ob and range_size > 0:
            pct = bull_ob_dist / price * 100
            if pct < 1.5:
                details.append(f"Near Bullish OB ({pct:.1f}% away)")
    elif in_premium:
        zone_type = "resistance"
        zone_label = "Premium Zone"
        summary = "Price in SMC Resistance (Premium)"
        details.append("LuxAlgo Premium Zone")
        if trend == BULLISH:
            details.append("Swing trend: Bullish")
        elif trend == BEARISH:
            details.append("Swing trend: Bearish")
        if bear_ob_dist == 0:
            details.append("Inside Bearish Order Block")
        elif bear_ob_dist is not None and bear_ob and range_size > 0:
            pct = bear_ob_dist / price * 100
            if pct < 1.5:
                details.append(f"Near Bearish OB ({pct:.1f}% away)")
    elif in_equilibrium:
        zone_type = "equilibrium"
        zone_label = "Equilibrium"
        summary = "Price in SMC Equilibrium"
        details.append("Between premium & discount")
        details.append(f"EQ level: {zones['equilibrium']:.4f}" if price < 1 else f"EQ level: {zones['equilibrium']:.2f}")
    else:
        if price > zones["premium_top"]:
            zone_type = "resistance"
            zone_label = "Above Premium"
            summary = "Price above SMC resistance"
            details.append("Extended above premium zone")
        elif price < zones["discount_bottom"]:
            zone_type = "support"
            zone_label = "Below Discount"
            summary = "Price below SMC support"
            details.append("Extended below discount zone")

    if dist_to_low_pct < 2 and zone_type != "support":
        details.append(f"Near swing low ({dist_to_low_pct:.1f}%)")
    if dist_to_high_pct < 2 and zone_type != "resistance":
        details.append(f"Near swing high ({dist_to_high_pct:.1f}%)")

    # Add new details
    if liquidity_sweep_bullish:
        details.append("Liquidity sweep (bullish)")
    if liquidity_sweep_bearish:
        details.append("Liquidity sweep (bearish)")
    if choch_bullish:
        details.append("CHoCH (bullish)")
    if choch_bearish:
        details.append("CHoCH (bearish)")
    if fvg:
        details.append(f"{fvg['type'].capitalize()} FVG")

    trend_str = "bullish" if trend == BULLISH else "bearish" if trend == BEARISH else "neutral"

    # Convert all numpy types to native Python types for JSON serializability
    trailing_high = float(trailing_high) if trailing_high is not None else None
    trailing_low = float(trailing_low) if trailing_low is not None else None
    dist_to_high_pct = float(round(dist_to_high_pct, 2)) if dist_to_high_pct is not None else None
    dist_to_low_pct = float(round(dist_to_low_pct, 2)) if dist_to_low_pct is not None else None

    # Also convert fvg if present
    safe_fvg = None
    if fvg:
        safe_fvg = {
            "type": fvg["type"],
            "high": float(fvg["high"]),
            "low": float(fvg["low"]),
            "midpoint": float(fvg["midpoint"])
        }

    return {
        "zone_type": zone_type,
        "zone_label": zone_label,
        "summary": summary,
        "details": details,
        "swing_high": trailing_high,
        "swing_low": trailing_low,
        "trend": trend_str,
        "dist_to_high_pct": dist_to_high_pct,
        "dist_to_low_pct": dist_to_low_pct,
        "liquidity_sweep_bullish": bool(liquidity_sweep_bullish),
        "liquidity_sweep_bearish": bool(liquidity_sweep_bearish),
        "choch_bullish": bool(choch_bullish),
        "choch_bearish": bool(choch_bearish),
        "fvg": safe_fvg,
        "ob_grade_bullish": int(ob_grade_bullish) if ob_grade_bullish is not None else 0,
        "ob_grade_bearish": int(ob_grade_bearish) if ob_grade_bearish is not None else 0,
    }
