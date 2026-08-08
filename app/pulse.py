"""
Market Pulse — one 0-100 read on the whole crypto tape, plus the parts it is
made of.

2026-08-09: the dashboard led with per-coin RSI numbers and a row of seven
indicator "lights". That is the engine's internal scoring shown raw — it tells
you what S1 thinks about one symbol, never what the market is doing. This
module answers the other question: is money flowing in or out of crypto right
now, how broad is the move, and how much of that read can I actually trust.

HONESTY RULES (the whole reason this file exists rather than a few lines of
template JS):

  • A component that cannot be read is EXCLUDED from the average, never scored
    0 and never scored 50. Scoring a dead feed 50 would quietly drag every
    reading toward neutral and look identical to a genuinely neutral tape.
    See the fabricated-zeros lesson: nz() on a missing reading asserts a fact.
  • Because components drop out, the denominator moves — so the payload always
    carries `confidence`: how much of the intended weight was actually live.
    A 78 built from 3 of 5 inputs is not the same number as a 78 built from 5,
    and the UI must be able to say so.
  • Every component reports the raw reading it was derived from (`value`), so
    a suspicious score can be checked against the source instead of trusted.

Everything here is public read-only market data (Binance USD-M via ccxt +
alternative.me), disk-cached so the four processes that import it (web, bot,
S2 scanner, S3 scanner) share one fetch instead of each paying for their own.
"""
import json
import os
import statistics
import time

import market_intel

SCAN_FILE = os.path.join(os.path.dirname(__file__), "scan_results.json")

# ── sector map ───────────────────────────────────────────────────────────────
# Crypto's answer to a stock dashboard's sector heat map. Curated by hand: there is no
# free, reliable category feed for perp tickers, and a wrong sector is worse
# than no sector. Anything not listed lands in "Other" rather than being guessed.
SECTORS = {
    "L1 Chains": ["BTC", "ETH", "SOL", "BNB", "ADA", "AVAX", "DOT", "ATOM", "NEAR",
                  "APT", "SUI", "SEI", "TIA", "INJ", "TON", "ALGO", "EGLD", "FTM",
                  "KAS", "ICP", "HBAR", "XLM", "TRX", "ETC", "LTC", "BCH", "XRP",
                  "MOVE", "BERA", "S", "CORE", "KAIA"],
    "L2 Scaling": ["ARB", "OP", "MATIC", "POL", "STRK", "MANTA", "METIS", "ZK",
                   "BLAST", "TAIKO", "SCR", "LRC", "IMX", "MODE"],
    "DeFi": ["UNI", "AAVE", "MKR", "SKY", "CRV", "LDO", "SNX", "COMP", "SUSHI",
             "1INCH", "DYDX", "GMX", "PENDLE", "ENA", "ETHFI", "JTO", "JUP",
             "RAY", "CAKE", "MORPHO", "EIGEN", "HYPE", "AERO", "VELO"],
    "AI / Agents": ["FET", "AGIX", "OCEAN", "RNDR", "RENDER", "TAO", "WLD", "ARKM",
                    "AI", "PHB", "NMR", "GRT", "AKT", "IO", "AIXBT", "VIRTUAL", "GRASS",
                    "ARC", "AI16Z", "SWARMS"],
    "Memes": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME", "BOME",
              "POPCAT", "MEW", "NEIRO", "TURBO", "BRETT", "PNUT", "GOAT", "ACT",
              "CHILLGUY", "MOODENG", "FARTCOIN", "SPX", "TRUMP", "MELANIA"],
    "Gaming / Metaverse": ["SAND", "MANA", "AXS", "GALA", "ENJ", "APE", "ILV", "MAGIC",
                           "PIXEL", "PORTAL", "BIGTIME", "NOT", "CATI", "HMSTR", "DOGS",
                           "GMT", "YGG", "ACE"],
    "Infrastructure": ["LINK", "PYTH", "BAND", "API3", "FIL", "AR", "STORJ", "HNT",
                       "IOTX", "ROSE", "CELO", "QNT", "RUNE", "AXL", "W", "ZRO",
                       "OMNI", "ALT", "DYM", "SAGA", "ETHW"],
    "Exchange": ["OKB", "CRO", "KCS", "HT", "LEO", "GT", "BGB", "MX"],
    "RWA / Payments": ["ONDO", "POLYX", "OM", "CFG", "TRU", "RSR", "PAXG", "XAUT",
                       "USUAL", "USDC", "FDUSD"],
    "Privacy": ["XMR", "ZEC", "DASH", "SCRT", "ROSE", "ARRR"],
}
# base → sector, first listing wins (a coin appears once).
_SECTOR_OF = {}
for _name, _bases in SECTORS.items():
    for _b in _bases:
        _SECTOR_OF.setdefault(_b, _name)


def sector_of(base: str) -> str:
    return _SECTOR_OF.get((base or "").upper(), "Other")


# ── raw feeds (disk-cached: shared by web + 3 scanner processes) ─────────────
def _perp_tickers() -> dict:
    """24h stats for every USDT perpetual in ONE exchange call.

    This single response powers breadth, momentum, the sector heat map and the
    movers list. Fetching it once and sharing it on disk is the difference
    between 1 request per 90s and 4 processes × 4 panels hammering the API.
    """
    out = {"rows": [], "errors": []}
    try:
        tickers = market_intel._exchange().fetch_tickers()
    except Exception as e:  # noqa: BLE001 — a dead feed drops its components
        out["errors"].append(f"tickers: {e}")
        return out
    for sym, t in (tickers or {}).items():
        if not sym.endswith(":USDT"):
            continue
        pct, vol = t.get("percentage"), t.get("quoteVolume")
        last = t.get("last") or t.get("close")
        if pct is None or last is None:
            continue          # a ticker with no move is not a ticker at 0%
        try:
            out["rows"].append({
                "symbol": sym, "base": sym.split("/")[0],
                "price": float(last), "pct": float(pct),
                "volume": float(vol or 0.0),
            })
        except (TypeError, ValueError):
            continue
    return out


def perp_tickers(ttl: float = 90.0) -> dict:
    return market_intel._disk_cached("pulse_tickers", ttl, _perp_tickers,
                                     is_good=lambda d: bool(d.get("rows")))


def _funding_all() -> dict:
    """Funding rate for every perp — one bulk premium-index call."""
    out = {"rows": {}, "errors": []}
    try:
        for sym, fr in (market_intel._exchange().fetch_funding_rates() or {}).items():
            r = fr.get("fundingRate")
            if r is not None:
                out["rows"][sym] = float(r)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"funding: {e}")
    return out


def funding_all(ttl: float = 300.0) -> dict:
    return market_intel._disk_cached("pulse_funding", ttl, _funding_all,
                                     is_good=lambda d: bool(d.get("rows")))


def _btc_daily() -> dict:
    """30 daily BTC closes — the trend line, and the EMA structure read."""
    out = {"candles": [], "errors": []}
    try:
        raw = market_intel._exchange().fetch_ohlcv("BTC/USDT:USDT", "1d", None, 220)
        out["candles"] = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                          for c in (raw or [])]
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"btc daily: {e}")
    return out


def btc_daily(ttl: float = 900.0) -> dict:
    return market_intel._disk_cached("pulse_btc_daily", ttl, _btc_daily,
                                     is_good=lambda d: bool(d.get("candles")))


def _ema(values: list, span: int):
    if len(values) < span:
        return None
    k = 2.0 / (span + 1.0)
    e = sum(values[:span]) / span
    for v in values[span:]:
        e = v * k + e * (1 - k)
    return e


# ── scoring helpers ─────────────────────────────────────────────────────────
def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _linear(value, low, high):
    """value at `low` → 0, at `high` → 100, clamped. The mapping is stated in
    each component's `note` so a reader can disagree with it knowingly."""
    if high == low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100.0)


# Weights are a judgement call, so they are visible in the payload and in the
# UI rather than buried: breadth and trend dominate because a tape that is
# rising on 20% of coins is not a rising tape, whatever BTC prints.
WEIGHTS = {"trend": 25, "breadth": 25, "momentum": 20, "funding": 15, "sentiment": 15}

LABELS = ((78, "STRONG RISK-ON", "bull"), (62, "RISK-ON", "bull"),
          (45, "NEUTRAL", "neutral"), (30, "RISK-OFF", "bear"),
          (0, "STRONG RISK-OFF", "bear"))


def label_for(score):
    for floor, text, tone in LABELS:
        if score >= floor:
            return text, tone
    return "NEUTRAL", "neutral"


def _btc_regime() -> str:
    """The regime the SCANNER is acting on — read from its own output file, not
    recomputed here. Two different answers to "is BTC bullish" on one screen is
    worse than one answer that is occasionally a sweep old."""
    try:
        with open(SCAN_FILE, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("btc_regime") or "").upper()
    except Exception:  # noqa: BLE001
        return ""


# ── the composite ───────────────────────────────────────────────────────────
def compute(min_volume: float = 3_000_000.0) -> dict:
    """The whole dashboard payload. `min_volume` filters the breadth/momentum
    universe to perps with real turnover — a 200k-a-day ghost listing moving
    40% is noise, and letting it vote makes breadth jumpy for no reason."""
    tk = perp_tickers()
    fund = funding_all()
    daily = btc_daily()
    try:
        fg = market_intel.fear_greed()
    except Exception as e:  # noqa: BLE001
        fg = {"value": None, "errors": [str(e)]}

    rows = [r for r in tk.get("rows", []) if r["volume"] >= min_volume]
    comps = []

    # 1) TREND — BTC's own daily structure. Uses the scanner's regime when it
    #    has one so the page and the engine never disagree; falls back to the
    #    EMA read from the daily candles.
    closes = [c[4] for c in daily.get("candles") or []]
    regime, trend_note, trend_score = _btc_regime(), "", None
    if regime in ("BULL", "BEAR", "NEUTRAL"):
        trend_score = {"BULL": 85.0, "NEUTRAL": 50.0, "BEAR": 15.0}[regime]
        trend_note = f"scanner reads BTC as {regime}"
    elif len(closes) >= 200:
        e50, e200 = _ema(closes, 50), _ema(closes, 200)
        px = closes[-1]
        above = (px > e50) + (px > e200) + (e50 > e200)
        trend_score = {0: 10.0, 1: 35.0, 2: 65.0, 3: 90.0}[above]
        regime = "BULL" if above >= 2 else "BEAR" if above <= 1 else "NEUTRAL"
        trend_note = f"BTC clears {above}/3 moving-average tests"
    comps.append({
        "key": "trend", "label": "BTC trend structure", "weight": WEIGHTS["trend"],
        "score": trend_score, "value": regime or None, "unit": "",
        "note": trend_note or "BTC trend could not be determined",
    })

    # 2) BREADTH — the share of the liquid universe that is green. This is the
    #    number that separates "the market is up" from "BTC is up".
    up = sum(1 for r in rows if r["pct"] > 0)
    down = sum(1 for r in rows if r["pct"] < 0)
    flat = len(rows) - up - down
    breadth_pct = (up / len(rows) * 100.0) if rows else None
    comps.append({
        "key": "breadth", "label": "Market breadth", "weight": WEIGHTS["breadth"],
        "score": breadth_pct, "value": round(breadth_pct, 1) if breadth_pct is not None else None,
        "unit": "%",
        "note": (f"{up} up / {down} down of {len(rows)} liquid contracts" if rows
                 else "no contract quotes available"),
    })

    # 3) MOMENTUM — the MEDIAN 24h move, not the mean. One 300% listing pump
    #    would own a mean; the median describes the coin in the middle.
    med = statistics.median([r["pct"] for r in rows]) if rows else None
    comps.append({
        "key": "momentum", "label": "Median momentum", "weight": WEIGHTS["momentum"],
        "score": _linear(med, -4.0, 4.0) if med is not None else None,
        "value": round(med, 2) if med is not None else None, "unit": "%",
        "note": ("median 24h move across the universe; the scale runs ±4%" if med is not None
                 else "no contract quotes available"),
    })

    # 4) FUNDING — what the crowd is paying to hold its side. Baseline is
    #    +0.01% per 8h (the exchange's neutral clamp), so that maps to 50.
    frs = [fund["rows"].get(r["symbol"]) for r in rows]
    frs = [f for f in frs if f is not None]
    mean_fr = statistics.mean(frs) if frs else None
    comps.append({
        "key": "funding", "label": "Funding rate", "weight": WEIGHTS["funding"],
        # 0.01% is neutral; -0.03% → 0, +0.05% → 100.
        "score": _linear(mean_fr, -0.0003, 0.0005) if mean_fr is not None else None,
        "value": round(mean_fr * 100, 4) if mean_fr is not None else None, "unit": "%",
        "note": (f"mean funding across {len(frs)} contracts; positive = longs pay" if frs
                 else "funding rates unavailable"),
    })

    # 5) SENTIMENT — Fear & Greed, already a 0-100 scale by construction.
    fgv = fg.get("value")
    comps.append({
        "key": "sentiment", "label": "Fear & Greed", "weight": WEIGHTS["sentiment"],
        "score": float(fgv) if fgv is not None else None,
        "value": fgv, "unit": "",
        "note": (fg.get("label") or "") if fgv is not None else "index unavailable",
    })

    live = [c for c in comps if c["score"] is not None]
    wsum = sum(c["weight"] for c in live)
    score = round(sum(c["score"] * c["weight"] for c in live) / wsum, 1) if wsum else None
    text, tone = label_for(score) if score is not None else ("NO READING", "off")

    for c in comps:
        c["ok"] = c["score"] is not None
        if c["score"] is not None:
            c["score"] = round(c["score"], 1)

    errors = (tk.get("errors") or []) + (fund.get("errors") or []) + \
             (daily.get("errors") or []) + (fg.get("errors") or [])

    return {
        "generated_at": int(time.time()),
        "score": score, "label": text, "tone": tone,
        "components": comps,
        "confidence": {
            "pct": round(wsum / sum(WEIGHTS.values()) * 100, 1),
            "ok": len(live), "total": len(comps),
            "missing": [c["label"] for c in comps if c["score"] is None],
        },
        "breadth": {"up": up, "down": down, "flat": flat, "total": len(rows),
                    "ratio": round(up / down, 2) if down else None},
        "sectors": sectors(rows),
        "movers": movers(rows),
        "btc": btc_block(daily.get("candles") or []),
        "stale": bool(tk.get("stale") or fund.get("stale")),
        "errors": errors[:6],
    }


def sectors(rows: list, min_members: int = 3) -> list:
    """Per-sector median 24h move. Median again — one meme going vertical must
    not paint its whole sector green. Sectors with fewer than `min_members`
    live tickers are dropped: an "average" of two coins is two coins."""
    buckets = {}
    for r in rows:
        buckets.setdefault(sector_of(r["base"]), []).append(r)
    out = []
    for name, members in buckets.items():
        if name == "Other" or len(members) < min_members:
            continue
        members.sort(key=lambda m: m["pct"], reverse=True)
        pcts = [m["pct"] for m in members]
        out.append({
            "name": name,
            "median": round(statistics.median(pcts), 2),
            "n": len(members),
            "up": sum(1 for p in pcts if p > 0),
            "leader": members[0]["base"], "leader_pct": round(members[0]["pct"], 2),
            "laggard": members[-1]["base"], "laggard_pct": round(members[-1]["pct"], 2),
        })
    out.sort(key=lambda s: s["median"], reverse=True)
    return out


def movers(rows: list, n: int = 8) -> dict:
    ranked = sorted(rows, key=lambda r: r["pct"], reverse=True)
    trim = lambda r: {"base": r["base"], "pct": round(r["pct"], 2),      # noqa: E731
                      "price": r["price"], "volume": round(r["volume"])}
    return {"up": [trim(r) for r in ranked[:n]],
            "down": [trim(r) for r in ranked[-n:][::-1]]}


def btc_block(candles: list, days: int = 30) -> dict:
    """The headline trend card: BTC's last `days` daily closes plus the
    high/low/open/close of that window."""
    tail = candles[-days:]
    if not tail:
        return {"series": [], "ok": False}
    closes = [c[4] for c in tail]
    return {
        "ok": True,
        "series": [{"t": c[0], "c": c[4]} for c in tail],
        "open": tail[0][1], "close": closes[-1],
        "high": max(c[2] for c in tail), "low": min(c[3] for c in tail),
        "change_pct": round((closes[-1] - tail[0][1]) / tail[0][1] * 100, 2)
        if tail[0][1] else None,
        "days": len(tail),
    }
