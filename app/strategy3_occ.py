"""
Strategy 3 — OCC ("Open Close Cross") signal engine.

Python port of pine/TV_strategy_ETH_SOL_30min.pine — JustUncleL's
"Open Close Cross Strategy R5.1" with its DEFAULT settings:

    MA type   SMMA (Wilder smoothing, SMA-seeded)      basisType = "SMMA"
    MA length 8                                        basisLen  = 8
    Alt res   chart timeframe × 3 (30m chart → 90m)    useRes/intRes
    Signal    SMMA8(close) crosses SMMA8(open) on the 90m series
                over  → LONG   ·   under → SHORT
    Trades    BOTH directions, stop-and-reverse, no TP/SL in the pine

The 90m series is resampled here from the same Binance 30m candles the
TradingView chart shows: three consecutive 30m candles per 90m bucket,
aligned to midnight UTC exactly like TradingView's intraday bars (a 24h
day is 16 × 90m, so epoch alignment == daily alignment).

FIDELITY / HONESTY NOTES
  • The TradingView original uses security(..., lookahead=barmerge.
    lookahead_on) with no delay — with default settings it REPAINTS and its
    backtest sees 90m values before they exist. This port acts ONLY on
    closed 90m buckets, which is the tradeable version of the strategy: live
    entries come up to 90 minutes later than the repainting backtest print.
    (In TradingView, "Delay Open/Close MA" ≥ 1 shows more realistic fills.)
  • SMMA is recursive with infinite memory; TradingView computes it over the
    chart's whole history while we replay ~1000 30m candles (~330 buckets).
    The difference decays as (1-1/len)^n — after 100 buckets it is already
    < 1e-5 of price — so the last-bar signal matches the chart in practice.
  • Buckets with a missing 30m candle (exchange outage) are DROPPED, not
    padded; the SMMA simply continues over the surviving buckets.

Everything here is pure (no I/O, no config reads) — the scanner passes the
candles and parameters in, tests feed synthetic series.
"""


def resample(ohlcv: list, tf_sec: int, mult: int) -> list:
    """Group closed base-timeframe candles into `mult`-candle buckets aligned
    to bucket_ms boundaries (epoch/UTC). Returns [ts, open, high, low, close,
    volume] per COMPLETE bucket — a trailing partial bucket (the 90m bar still
    forming out of already-closed 30m bars) is dropped, as is any bucket that
    isn't exactly `mult` consecutive candles."""
    tf_ms = int(tf_sec) * 1000
    bucket_ms = tf_ms * mult
    out = []
    cur_start = None
    cur = None
    for c in ohlcv:
        ts = int(c[0])
        start = ts - (ts % bucket_ms)
        if start != cur_start:
            if cur is not None and cur["n"] == mult:
                out.append([cur_start, cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]])
            cur_start = start
            cur = {"n": 0, "o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
                   "c": float(c[4]), "v": 0.0, "next_ts": start}
        if ts != cur["next_ts"]:            # gap inside the bucket → incomplete
            cur["n"] = -10**9               # poison: can never reach mult
        else:
            cur["n"] += 1
            cur["next_ts"] = ts + tf_ms
        cur["h"] = max(cur["h"], float(c[2]))
        cur["l"] = min(cur["l"], float(c[3]))
        cur["c"] = float(c[4])
        cur["v"] += float(c[5])
    if cur is not None and cur["n"] == mult:
        out.append([cur_start, cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]])
    return out


def smma(values: list, length: int) -> list:
    """Wilder's smoothed MA exactly as the pine's SMMA variant: na until
    `length` values exist, seeded with their SMA, then
    (prev*(len-1) + value) / len. Returns a list with None for the na bars."""
    out = [None] * len(values)
    if len(values) < length:
        return out
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def snapshot(ohlcv: list, tf_sec: int, mult: int = 3, length: int = 8) -> dict:
    """Signal state on the LAST closed alt-resolution bucket.

    Returns {signal, bucket_ts, price, close_ma, open_ma, trend, n_buckets,
    insufficient}. signal is 'long' on the pine's crossover (close-MA was ≤
    open-MA on the previous bucket and is > now), 'short' on the crossunder,
    else None. trend is 'up'/'down' from the current MA relationship — shown
    on the web pages so a flat "no cross this bar" still explains itself."""
    buckets = resample(ohlcv, tf_sec, mult)
    n = len(buckets)
    # length+1 buckets is the mathematical minimum for a prev/cur pair of MA
    # values; anything close to it is still SMMA warm-up, so demand headroom.
    if n < length + 10:
        return {"signal": None, "bucket_ts": 0, "price": None, "close_ma": None,
                "open_ma": None, "trend": None, "n_buckets": n, "insufficient": True}
    open_ma = smma([b[1] for b in buckets], length)
    close_ma = smma([b[4] for b in buckets], length)
    c_prev, c_cur = close_ma[-2], close_ma[-1]
    o_prev, o_cur = open_ma[-2], open_ma[-1]
    signal = None
    if c_prev is not None and o_prev is not None:
        if c_cur > o_cur and c_prev <= o_prev:
            signal = "long"
        elif c_cur < o_cur and c_prev >= o_prev:
            signal = "short"
    return {
        "signal": signal,
        "bucket_ts": buckets[-1][0],
        "price": buckets[-1][4],
        "close_ma": c_cur,
        "open_ma": o_cur,
        "trend": "up" if c_cur > o_cur else "down" if c_cur < o_cur else "flat",
        "n_buckets": n,
        "insufficient": False,
    }
