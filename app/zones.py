"""
📦 Supply / demand zones + trend context — the "SELL / LONG at the box" shape.

Asked for 2026-08-20 from a TradingView screenshot: shaded boxes at old
reversal levels, a falling resistance line and a rising support line, and a
SELL label where price rallies back into a box from underneath.

THE RULES HERE WERE READ OFF A PICTURE, not from a specification. That is
worth stating plainly at the top of the file, because this repo has repeatedly
found that a shape which looks right on a chart is the beginning of a question
rather than the end of one — 48 mean-reversion configs at 55–78% win rate of
which 46 lost money, S1 failing walk-forward twice, every S2 exit rule
negative. What is implemented is the visible geometry:

  ZONE     a price band built from CLUSTERED swing pivots — the same pivots
           and the same clustering breakout_flip uses, so a "zone" here and a
           "level" in the 壓力翻支撐 alerts mean the same thing rather than
           being two definitions of resistance in one codebase.
  FRESH    a zone stops counting once price has CLOSED through it. An old box
           price has already eaten is a line on a chart, not a decision.
  TREND    the slope through recent swing highs (falling = resistance) and
           swing lows (rising = support). Context, not a gate — see measure().
  SIGNAL   sell = price trades up into a fresh supply zone; long = price
           trades down into a fresh demand zone.

Anti-lookahead is the same discipline as breakout_flip: a pivot at bar i is
not knowable until i+PIVOT_RIGHT, and every read takes `at` so a replay cannot
see bars that had not printed.
"""
import os

import breakout_flip as B

# Zones are built from the same pivots as the flip detector, so the two agree.
PIVOT_LEFT = int(os.getenv("ZONE_PIVOT_LEFT", str(B.PIVOT_LEFT)))
PIVOT_RIGHT = int(os.getenv("ZONE_PIVOT_RIGHT", str(B.PIVOT_RIGHT)))
ZONE_TOL_PCT = float(os.getenv("ZONE_TOL_PCT", str(B.ZONE_TOL_PCT)))
# How far either side of the clustered price the band extends, as a share of
# the cluster's own spread. A zero-width "zone" is just a level.
ZONE_PAD_PCT = float(os.getenv("ZONE_PAD_PCT", "0.15"))
# A zone needs this many touches. One high is a high; a zone is a price that
# has turned trade more than once.
MIN_TOUCHES = int(os.getenv("ZONE_MIN_TOUCHES", "2"))
# Bars back the trend line is fitted over.
TREND_BARS = int(os.getenv("ZONE_TREND_BARS", "120"))


def _pivot_lows(lows: list, known_by=None) -> list:
    """Swing lows via the SAME comparison as swing_highs, on negated lows.
    The sign comes back before anything divides by a price."""
    return [(i, -v) for i, v in
            B.swing_highs([-x for x in lows], PIVOT_LEFT, PIVOT_RIGHT, known_by=known_by)]


def build(ohlcv: list, at: int = None) -> list:
    """Every zone knowable at bar `at`, nearest-to-price first.

    Returns [{kind, top, bottom, mid, touches, last_idx, fresh}].
    """
    if not ohlcv:
        return []
    end = (len(ohlcv) - 1) if at is None else int(at)
    if end < PIVOT_LEFT + PIVOT_RIGHT + 5:
        return []
    highs = [float(c[2]) for c in ohlcv[:end + 1]]
    lows = [float(c[3]) for c in ohlcv[:end + 1]]
    closes = [float(c[4]) for c in ohlcv[:end + 1]]
    price = closes[-1]
    if price <= 0:
        return []

    out = []
    for kind, pivots in (("supply", B.swing_highs(highs, PIVOT_LEFT, PIVOT_RIGHT, known_by=end)),
                         ("demand", _pivot_lows(lows, known_by=end))):
        for z in B.cluster_zones(pivots, ZONE_TOL_PCT):
            if z["touches"] < MIN_TOUCHES:
                continue
            spread = max(z["top"] - z["bottom"], z["price"] * ZONE_PAD_PCT / 100)
            top, bottom = z["price"] + spread / 2, z["price"] - spread / 2
            # FRESH: has any bar CLOSED through the far side since the zone's
            # last touch? A supply zone is spent once price closed above it.
            after = closes[z["last_idx"] + 1:]
            spent = any(c > top for c in after) if kind == "supply" \
                else any(c < bottom for c in after)
            out.append({"kind": kind, "top": top, "bottom": bottom,
                        "mid": z["price"], "touches": z["touches"],
                        "last_idx": z["last_idx"], "fresh": not spent})
    out.sort(key=lambda z: abs(z["mid"] - price))
    return out


def trend(ohlcv: list, at: int = None, bars: int = TREND_BARS) -> dict:
    """Slope through recent swing highs and lows, in % per bar.

    {'res_slope': float|None, 'sup_slope': float|None} — None when there are
    too few pivots to fit a line, which is NOT the same as flat and must not
    be reported as zero.
    """
    if not ohlcv:
        return {"res_slope": None, "sup_slope": None}
    end = (len(ohlcv) - 1) if at is None else int(at)
    start = max(0, end - bars)
    highs = [float(c[2]) for c in ohlcv[:end + 1]]
    lows = [float(c[3]) for c in ohlcv[:end + 1]]
    price = float(ohlcv[end][4]) or 1.0

    def slope(pivots):
        pts = [(i, v) for i, v in pivots if i >= start]
        if len(pts) < 2:
            return None
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        if x2 == x1:
            return None
        return (y2 - y1) / (x2 - x1) / price * 100

    return {"res_slope": slope(B.swing_highs(highs, PIVOT_LEFT, PIVOT_RIGHT, known_by=end)),
            "sup_slope": slope(_pivot_lows(lows, known_by=end))}


def signal(ohlcv: list, at: int = None) -> dict:
    """{} unless bar `at` traded INTO a fresh zone.

    Otherwise {side, kind, zone, trend} where side is 'short' at supply and
    'long' at demand — the SELL / LONG label on the picture.
    """
    if not ohlcv:
        return {}
    end = (len(ohlcv) - 1) if at is None else int(at)
    if end < 1:
        return {}
    bar = ohlcv[end]
    hi, lo, close = float(bar[2]), float(bar[3]), float(bar[4])
    prev_close = float(ohlcv[end - 1][4])

    for z in build(ohlcv, end):
        if not z["fresh"]:
            continue
        if z["kind"] == "supply":
            # Traded up into the band from below, and did not close above it —
            # a close above is a break, which is breakout_flip's setup, not
            # this one.
            entered = hi >= z["bottom"] and prev_close < z["bottom"] and close <= z["top"]
            side = "short"
        else:
            entered = lo <= z["top"] and prev_close > z["top"] and close >= z["bottom"]
            side = "long"
        if entered:
            return {"side": side, "kind": z["kind"], "zone": z,
                    "trend": trend(ohlcv, end), "price": close, "at": end}
    return {}


def at_zone(ohlcv: list, side: str, at: int = None) -> bool:
    """Is price sitting INSIDE a fresh zone that agrees with `side`?

    Looser than signal() on purpose. signal() needs the bar that crossed IN,
    and a 5m bar rarely crosses on the same close as the 15m one — demanding
    both would reject almost everything for a reason that is about candle
    alignment rather than about the market. The confirming timeframe is asked
    the weaker, honest question: do you also think we are at supply/demand?
    """
    if not ohlcv:
        return False
    end = (len(ohlcv) - 1) if at is None else int(at)
    if end < 1:
        return False
    px = float(ohlcv[end][4])
    want = "supply" if side == "short" else "demand"
    for z in build(ohlcv, end):
        if z["fresh"] and z["kind"] == want and z["bottom"] <= px <= z["top"]:
            return True
    return False


# ── the scan-facing entry point ──────────────────────────────────────────────
COOLDOWN_SEC = float(os.getenv("ZONE_COOLDOWN_SEC", str(6 * 3600)))
STOP_BUFFER = float(os.getenv("ZONE_STOP_BUFFER_PCT", "0.15")) / 100
TP_R = float(os.getenv("ZONE_TP_R", "2"))
MIN_STOP_PCT = float(os.getenv("ZONE_MIN_STOP_PCT", "0.5")) / 100
MAX_STOP_PCT = float(os.getenv("ZONE_MAX_STOP_PCT", "6")) / 100
# Trend agreement is NOT enforced here. Measured on 3,653 replayed entries the
# with-trend half is +0.175R and the rest is -0.032R, which is a large gap —
# but the plain signal collapses when the best five symbols are dropped, and
# the early half of the window straddles zero. Recording both halves is what
# makes the cut answerable on a forward book instead of arguable; gating on it
# now would leave nothing to compare against.
REQUIRE_TREND = os.getenv("ZONE_REQUIRE_TREND", "false").strip().lower() in ("1", "true", "yes")


def plan(entry: float, zone: dict, side: str) -> dict:
    """Stop beyond the far side of the zone that produced the signal — the
    same principle as S4: the level IS the stop, so an invalid level means no
    trade rather than an invented stop distance."""
    if not entry or entry <= 0:
        return {}
    sl = zone["top"] * (1 + STOP_BUFFER) if side == "short" \
        else zone["bottom"] * (1 - STOP_BUFFER)
    risk = (sl - entry) if side == "short" else (entry - sl)
    if risk <= 0:
        return {}
    stop_pct = risk / entry
    if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
        return {}
    tp = entry - risk * TP_R if side == "short" else entry + risk * TP_R
    return {"entry": entry, "sl": sl, "tp": tp, "rr": TP_R,
            "stop_pct": round(stop_pct * 100, 2)}


# The confirming timeframe. The owner's 賽克斯 setup is read on 5m AND 15m, so
# a 15m entry that the 5m does not also place at supply/demand is a different
# thing wearing the same label.
CONFIRM_TF = os.getenv("ZONE_CONFIRM_TF", "5m")
CONFIRM_CANDLES = int(os.getenv("ZONE_CONFIRM_CANDLES", "400"))
# Off by default: the agreement is RECORDED on every signal so the forward book
# can price it, and the board sorts confirmed ones first. Making it a hard gate
# before the book has anything in it would leave nothing to compare against —
# the same reason REQUIRE_TREND is off.
REQUIRE_CONFIRM = os.getenv("ZONE_REQUIRE_CONFIRM", "false").strip().lower() in ("1", "true", "yes")


def consider(sym: str, ohlcv: list, state: dict, now: float,
             fetch_tf=None) -> dict:
    """One symbol, on candles the caller already has. {} unless it fired.

    The FORMING candle is dropped — strategy2_scanner patches a live price onto
    the last row, so a zone entry 'confirmed' on it can un-confirm itself two
    minutes later.
    """
    closed = ohlcv[:-1] if ohlcv else []
    if len(closed) < 260:
        return {}
    last = (state.get("last") or {}).get(sym)
    if last and now - last < COOLDOWN_SEC:
        return {}
    sig = signal(closed)
    if not sig:
        return {}
    pl = plan(sig["price"], sig["zone"], sig["side"])
    if not pl:
        return {}
    t = sig["trend"]
    with_trend = (t["res_slope"] is not None and
                  ((t["res_slope"] < 0) if sig["side"] == "short" else (t["res_slope"] > 0)))
    # 5m confirmation, asked ONLY now — after a 15m entry has already passed
    # every free check. Fires a handful of times a sweep instead of 300, which
    # is the same cheapest-gate-first order S4 uses.
    tf5 = "unknown"
    if fetch_tf is not None:
        try:
            rows = fetch_tf(sym, CONFIRM_TF, CONFIRM_CANDLES)
            if rows and len(rows) > 260:
                tf5 = "agree" if at_zone(rows[:-1], sig["side"]) else "no"
        except Exception as exc:  # noqa: BLE001 — confirmation must not cost the signal
            print(f"[zone] {sym} {CONFIRM_TF} check failed: {exc}")
    if REQUIRE_CONFIRM and tf5 != "agree":
        return {}

    state.setdefault("last", {})[sym] = now
    z = sig["zone"]
    return {"symbol": sym, "base": sym.split("/")[0], "side": sig["side"],
            "tf": "15m", "confirm_tf": CONFIRM_TF, "tf5": tf5,
            "kind": z["kind"], "zone_top": z["top"], "zone_bottom": z["bottom"],
            "touches": z["touches"], "with_trend": with_trend,
            "res_slope": t["res_slope"], "sup_slope": t["sup_slope"],
            "price": sig["price"], "ts": int(closed[-1][0]), "plan": pl,
            # flip_outcomes.record() keys the book on these two.
            "blue_sky": False}
