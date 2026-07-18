"""
Market Intel — self-contained data layer for the /market page.

This module is fully ISOLATED from the trading bot, scanner, executor and DB.
It only performs read-only HTTP GETs to free, public endpoints:

  • Binance Futures (USD-M) public API  — Volume, Open Interest, Funding Rate
        https://fapi.binance.com  (no API key required)
  • DefiLlama                            — On-chain TVL by chain
        https://api.llama.fi            (no API key required)
  • Crypto news RSS feeds                — Headlines (no API key required)

Every fetch is fail-soft: on any error it returns whatever it has plus an
`errors` list, so the web page degrades gracefully and never throws.
A small in-memory TTL cache keeps the page snappy and avoids rate limits.
"""

import json
import re
import time
import urllib.request
import urllib.error
from xml.etree import ElementTree as ET

import ccxt

# ── tiny in-memory TTL cache ────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, producer):
    """Return cached value if fresh, else call producer() and cache it."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (now, value)
    return value


# ── low-level HTTP helpers ──────────────────────────────────────────────────
# A real browser UA — Binance's /futures/data endpoints 403 unusual UAs
# (e.g. one mentioning "localhost"); the news/TVL feeds accept it fine too.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Binance Futures via ccxt (public, no keys — same client the bot uses) ────
_EX = None


def _exchange():
    """Lazy singleton USD-M futures client. Public endpoints only, no keys."""
    global _EX
    if _EX is None:
        _EX = ccxt.binanceusdm({"enableRateLimit": True})
    return _EX


def _binance_futures(top_n: int = 15) -> dict:
    """Volume + Funding Rate (bulk) + Open Interest (top N), via ccxt."""
    out = {"rows": [], "errors": []}
    ex = _exchange()

    # 1) tickers — volume / price / 24h change for every USDT-perp (1 call).
    try:
        tickers = ex.fetch_tickers()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"volume: {e}")
        return out

    # Keep linear USDT perpetuals only (symbol form "BTC/USDT:USDT").
    perps = [
        (sym, t) for sym, t in tickers.items()
        if sym.endswith(":USDT") and (t.get("quoteVolume") or 0) > 0
    ]
    perps.sort(key=lambda kv: float(kv[1].get("quoteVolume", 0) or 0), reverse=True)
    top = perps[:top_n]

    # 2) funding rates — one bulk call for everything, indexed by symbol.
    funding = {}
    try:
        for sym, fr in ex.fetch_funding_rates([s for s, _ in top]).items():
            funding[sym] = fr.get("fundingRate")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"funding: {e}")

    for sym, t in top:
        price = float(t.get("last") or t.get("close") or 0)

        # 3) open interest — per-symbol call (base units) → notional via price.
        oi_base = None
        oi_notional = None
        try:
            oi = ex.fetch_open_interest(sym)
            oi_base = oi.get("openInterestAmount")
            oi_notional = oi.get("openInterestValue")
            if oi_notional is None and oi_base and price:
                oi_notional = float(oi_base) * price
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"OI {sym}: {e}")

        out["rows"].append({
            "symbol": sym,
            "base": sym.split("/")[0],
            "price": price,
            "change_pct": float(t.get("percentage") or 0),
            "volume_usdt": float(t.get("quoteVolume") or 0),
            "open_interest_base": float(oi_base) if oi_base else None,
            "open_interest_usdt": float(oi_notional) if oi_notional else None,
            "funding_rate": funding.get(sym),
        })

    return out


def binance_futures(top_n: int = 15, ttl: float = 45.0) -> dict:
    return _cached(f"binance:{top_n}", ttl, lambda: _binance_futures(top_n))


def _oi_change(symbols: tuple) -> dict:
    """24h open-interest change per symbol from Binance's public futures-data API
    (hourly openInterestHist, no key needed). OI direction vs price direction is
    the crowding read: price↓+OI↑ = shorts piling in, price↑+OI↓ = short covering.
    One HTTP call per symbol → cached much longer than the ticker sweep."""
    out = {"rows": {}, "errors": []}
    for sym in symbols:
        raw = sym.split("/")[0] + "USDT"
        try:
            rows = _get_json("https://fapi.binance.com/futures/data/openInterestHist"
                             f"?symbol={raw}&period=1h&limit=24")
            if isinstance(rows, list) and len(rows) >= 2:
                first = float(rows[0]["sumOpenInterest"])
                last = float(rows[-1]["sumOpenInterest"])
                if first > 0:
                    out["rows"][sym] = round((last - first) / first * 100, 2)
        except Exception as e:  # noqa: BLE001 — one symbol must not kill the panel
            out["errors"].append(f"OIΔ {raw}: {e}")
    return out


def oi_change(symbols: tuple, ttl: float = 600.0) -> dict:
    return _cached("oi_change:" + ",".join(symbols), ttl, lambda: _oi_change(symbols))


# ── DefiLlama on-chain (public, no keys) ────────────────────────────────────
LLAMA = "https://api.llama.fi"


def _defillama(top_n: int = 12) -> dict:
    out = {"chains": [], "total_tvl": 0.0, "errors": []}
    try:
        chains = _get_json(f"{LLAMA}/v2/chains")
        chains.sort(key=lambda c: float(c.get("tvl", 0) or 0), reverse=True)
        total = sum(float(c.get("tvl", 0) or 0) for c in chains)
        out["total_tvl"] = total
        for c in chains[:top_n]:
            tvl = float(c.get("tvl", 0) or 0)
            out["chains"].append({
                "name": c.get("name", "?"),
                "symbol": c.get("tokenSymbol") or "",
                "tvl": tvl,
                "share_pct": (tvl / total * 100) if total else 0.0,
            })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"defillama: {e}")
    return out


def defillama(top_n: int = 12, ttl: float = 300.0) -> dict:
    return _cached(f"llama:{top_n}", ttl, lambda: _defillama(top_n))


# ── News (free RSS feeds, no keys) ──────────────────────────────────────────
# Swap / extend these freely; each is parsed with the stdlib XML parser.
NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    # Verified working 2026-07-05 (Bitcoin Magazine 403s bot requests — skipped).
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Defiant", "https://thedefiant.io/api/feed"),
    ("Blockworks", "https://blockworks.co/feed"),
]

# ── Headline sentiment (keyword-based, no external API) ─────────────────────
# Deliberately simple and transparent: word-boundary hits of clearly-signed
# words. A headline is 'bullish' when bull hits outnumber bear hits. This is
# a mood gauge over ~40 headlines, not per-article NLP — at that aggregation
# level, keyword polarity tracks the tape well enough to be useful.
_BULL_WORDS = (
    "surge", "soar", "rally", "rallies", "jump", "jumps", "gain", "gains",
    "record high", "all-time high", "ath", "bullish", "adoption", "approval",
    "approve", "approves", "inflow", "inflows", "breakout", "recover",
    "recovers", "recovery", "rebound", "rebounds", "upgrade", "partnership",
    "institutional", "accumulate", "accumulation", "climb", "climbs", "rise",
    "rises", "rising", "top", "buy", "buying", "green", "milestone",
    "breakthrough", "boom", "parabolic", "unlock global",
)
_BEAR_WORDS = (
    "crash", "crashes", "plunge", "plunges", "dump", "dumps", "fall", "falls",
    "drop", "drops", "tumble", "tumbles", "slump", "slumps", "sink", "sinks",
    "bearish", "sell-off", "selloff", "liquidation", "liquidated", "hack",
    "hacked", "exploit", "stolen", "scam", "fraud", "lawsuit", "sue", "sues",
    "ban", "bans", "restrict", "crackdown", "fine", "fines", "penalty",
    "outflow", "outflows", "fear", "warn", "warns", "warning", "bankrupt",
    "bankruptcy", "default", "delist", "delists", "layoff", "layoffs",
    "decline", "declines", "month low", "year low", "loss", "losses", "down",
    "risk-off", "capitulation", "delisting", "block retail", "negative",
)
_BULL_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _BULL_WORDS) + r")\b", re.I)
_BEAR_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _BEAR_WORDS) + r")\b", re.I)


def headline_sentiment(title: str) -> str:
    """'bullish' / 'bearish' / 'neutral' for one headline."""
    bull = len(_BULL_RE.findall(title or ""))
    bear = len(_BEAR_RE.findall(title or ""))
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


def _parse_rss(source: str, xml_text: str, limit: int) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    # RSS 2.0: channel/item ; some feeds namespace things, but title/link/pubDate are plain.
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            items.append({"source": source, "title": title, "link": link, "published": pub,
                          "desc": desc, "sentiment": headline_sentiment(title)})
        if len(items) >= limit:
            break
    if items:
        return items
    # Atom (e.g. Blockworks): namespaced <entry>, link as an href attribute.
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{ns}entry"):
        title = (entry.findtext(f"{ns}title") or "").strip()
        link_el = entry.find(f"{ns}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        pub = (entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or "").strip()
        desc = (entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or "").strip()
        if title and link:
            items.append({"source": source, "title": title, "link": link, "published": pub,
                          "desc": desc, "sentiment": headline_sentiment(title)})
        if len(items) >= limit:
            break
    return items


def _pub_ts(item: dict) -> float:
    """Epoch seconds from an RSS pubDate (RFC 822) or Atom date (ISO 8601),
    0 when unparseable."""
    raw = item.get("published") or ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _news(per_feed: int = 6) -> dict:
    out = {"items": [], "errors": []}
    for source, url in NEWS_FEEDS:
        try:
            xml_text = _get_text(url, timeout=8.0)
            out["items"].extend(_parse_rss(source, xml_text, per_feed))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{source}: {e}")
    # One merged stream, newest first (unparseable dates keep feed order at the
    # end) — reads like a wire feed instead of blocks grouped by source.
    out["items"].sort(key=_pub_ts, reverse=True)
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for it in out["items"]:
        counts[it["sentiment"]] += 1
    total = sum(counts.values())
    out["sentiment"] = {
        **counts, "total": total,
        # -100 (all bearish) … +100 (all bullish)
        "score": round((counts["bullish"] - counts["bearish"]) / total * 100) if total else 0,
    }
    return out


def news(per_feed: int = 6, ttl: float = 300.0) -> dict:
    return _cached(f"news:{per_feed}", ttl, lambda: _news(per_feed))


# ── Long/Short positioning (Binance free futures-data API, no keys) ──────────
# The same numbers Coinglass surfaces, straight from the source:
#   • "whales" = top traders ranked by position size  (smart-money lean)
#   • "retail" = every account weighted equally        (the crowd)
#   • "taker"  = aggressive market buy vs sell volume   (who's hitting the book)
DATA_API = "https://fapi.binance.com/futures/data"


def _ls_latest(endpoint: str, symbol: str, period: str):
    """Newest single data point for a Binance futures-data endpoint (or None)."""
    arr = _get_json(f"{DATA_API}/{endpoint}?symbol={symbol}&period={period}&limit=1")
    return arr[0] if arr else None


def _ls_one(symbol: str, period: str = "1h") -> dict:
    row = {"symbol": symbol,
           "base": symbol[:-4] if symbol.endswith("USDT") else symbol,
           "errors": []}

    # Whales — top traders by position size.
    try:
        d = _ls_latest("topLongShortPositionRatio", symbol, period)
        if d:
            row["whale_long"] = float(d["longAccount"])
            row["whale_short"] = float(d["shortAccount"])
            row["whale_ratio"] = float(d["longShortRatio"])
    except Exception as e:  # noqa: BLE001
        row["errors"].append(f"whale {symbol}: {e}")

    # Retail crowd — every account weighted equally.
    try:
        d = _ls_latest("globalLongShortAccountRatio", symbol, period)
        if d:
            row["retail_long"] = float(d["longAccount"])
            row["retail_short"] = float(d["shortAccount"])
            row["retail_ratio"] = float(d["longShortRatio"])
    except Exception as e:  # noqa: BLE001
        row["errors"].append(f"retail {symbol}: {e}")

    # Taker flow — aggressive market buys vs sells over the period.
    try:
        d = _ls_latest("takerlongshortRatio", symbol, period)
        if d:
            bv, sv = float(d["buyVol"]), float(d["sellVol"])
            tot = bv + sv
            row["taker_buy"] = (bv / tot) if tot else None
            row["taker_sell"] = (sv / tot) if tot else None
            row["taker_ratio"] = float(d["buySellRatio"])
    except Exception as e:  # noqa: BLE001
        row["errors"].append(f"taker {symbol}: {e}")

    # Smart-money divergence: whales and the crowd on opposite sides of neutral.
    wl, rl = row.get("whale_long"), row.get("retail_long")
    if wl is not None and rl is not None:
        row["divergence"] = (wl >= 0.5) != (rl >= 0.5)
    return row


def _long_short(symbols: tuple) -> dict:
    out = {"rows": [], "errors": []}
    for s in symbols:
        r = _ls_one(s)
        out["errors"].extend(r.pop("errors", []))
        if "whale_long" in r or "retail_long" in r:   # keep rows with real data
            out["rows"].append(r)
    return out


def long_short(symbols: tuple, ttl: float = 180.0) -> dict:
    return _cached("ls:" + ",".join(symbols), ttl, lambda: _long_short(symbols))


# ── Lightweight BTC snapshot (price / 24h / funding) for the briefing ────────
def _btc_snapshot() -> dict:
    out = {"price": None, "change_pct": None, "funding_rate": None, "errors": []}
    ex = _exchange()
    try:
        t = ex.fetch_ticker("BTC/USDT:USDT")
        out["price"] = float(t.get("last") or t.get("close") or 0)
        out["change_pct"] = float(t.get("percentage") or 0)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"btc price: {e}")
    try:
        out["funding_rate"] = ex.fetch_funding_rate("BTC/USDT:USDT").get("fundingRate")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"btc funding: {e}")
    return out


def btc_snapshot(ttl: float = 45.0) -> dict:
    return _cached("btc_snap", ttl, _btc_snapshot)


# ── US equity indices (CNBC quote service — free, no keys) ───────────────────
# Yahoo/Stooq throttle & anti-bot aggressively; CNBC's public quote endpoint is
# stable. One call per index, cached 10 min (indices move slowly).
CNBC_QUOTE = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
              "?requestMethod=itv&noform=1&fund=1&exthrs=0&output=json&symbols=")
STOCK_INDICES = [("S&P 500", ".SPX"), ("Nasdaq", ".IXIC"), ("Dow", ".DJI")]


def _num(s):
    """Parse CNBC's display strings like '7,358.22' or '-0.10%' to float."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _stock_one(name: str, symbol: str) -> dict:
    data = _get_json(f"{CNBC_QUOTE}{symbol}")
    q = data["FormattedQuoteResult"]["FormattedQuote"][0]
    return {"name": name, "symbol": symbol,
            "price": _num(q.get("last")), "change_pct": _num(q.get("change_pct"))}


def _stocks() -> dict:
    out = {"rows": [], "errors": []}
    for name, sym in STOCK_INDICES:
        try:
            out["rows"].append(_stock_one(name, sym))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{name}: {e}")
    return out


def stocks(ttl: float = 600.0) -> dict:
    return _cached("stocks", ttl, _stocks)


# ── Crypto Fear & Greed index (alternative.me — free, no keys) ───────────────
def _fear_greed() -> dict:
    out = {"value": None, "label": None, "week_ago": None, "history": [], "errors": []}
    try:
        # 8 points = today + 7 days back, for a "vs a week ago" read + sparkline.
        d = _get_json("https://api.alternative.me/fng/?limit=8")
        data = d.get("data") or []
        if data:
            out["value"] = int(data[0].get("value"))
            out["label"] = data[0].get("value_classification")
            vals = [int(x["value"]) for x in data if x.get("value") is not None]
            out["history"] = list(reversed(vals))           # oldest → newest
            out["week_ago"] = vals[7] if len(vals) >= 8 else (vals[-1] if vals else None)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"fng: {e}")
    return out


def fear_greed(ttl: float = 600.0) -> dict:
    # The index only updates once a day, so a long cache is plenty.
    return _cached("fng", ttl, _fear_greed)


def _global_market() -> dict:
    """Global crypto market snapshot from CoinGecko /global (free, no key):
    total market cap + 24h change, and BTC/ETH dominance. Dominance is the
    classic 'is money rotating into alts or hiding in BTC' regime read."""
    out = {"total_mcap_usd": None, "mcap_change_24h_pct": None,
           "btc_dominance": None, "eth_dominance": None, "errors": []}
    try:
        d = (_get_json("https://api.coingecko.com/api/v3/global") or {}).get("data") or {}
        mcap = (d.get("total_market_cap") or {}).get("usd")
        dom = d.get("market_cap_percentage") or {}
        out["total_mcap_usd"] = float(mcap) if mcap is not None else None
        chg = d.get("market_cap_change_percentage_24h_usd")
        out["mcap_change_24h_pct"] = float(chg) if chg is not None else None
        out["btc_dominance"] = float(dom["btc"]) if dom.get("btc") is not None else None
        out["eth_dominance"] = float(dom["eth"]) if dom.get("eth") is not None else None
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"coingecko global: {e}")
    return out


def global_market(ttl: float = 300.0) -> dict:
    return _cached("global_mkt", ttl, _global_market)


def _econ_calendar() -> dict:
    """High-impact USD macro events this week (FOMC, CPI, NFP…) from
    ForexFactory's public weekly JSON. Crypto trades straight through these
    prints; the strip exists so nobody holds a 4x position into CPI blind."""
    out = {"events": [], "errors": []}
    try:
        rows = _get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10.0)
        for r in rows or []:
            if (r.get("impact") or "").lower() != "high":
                continue
            if (r.get("country") or "").upper() != "USD":
                continue
            out["events"].append({
                "title": r.get("title"),
                "date": r.get("date"),          # ISO 8601 with offset
                "forecast": r.get("forecast") or None,
                "previous": r.get("previous") or None,
            })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"calendar: {e}")
    return out


def econ_calendar(ttl: float = 21600.0) -> dict:
    # The weekly file barely changes — 6h cache is plenty.
    return _cached("econ_cal", ttl, _econ_calendar)


# ── top-level aggregator used by the web route ──────────────────────────────
def market_intel(top_n: int = 15, pos_n: int = 6) -> dict:
    bf = binance_futures(top_n=top_n)
    rows = bf.get("rows", [])
    # Reuse the top futures coins (by volume) for the positioning panel.
    pos_symbols = tuple(f"{r['base']}USDT" for r in rows[:pos_n] if r.get("base"))
    ls = long_short(pos_symbols) if pos_symbols else {"rows": [], "errors": []}
    dl = defillama()
    nw = news()
    gm = global_market()
    oc_delta = oi_change(tuple(r["symbol"] for r in rows))
    for r in rows:
        r["oi_change_24h_pct"] = oc_delta["rows"].get(r["symbol"])
    # Fear & Greed + US stock indices: already built for the dashboard briefing
    # but never surfaced on the Market Intel page itself — cheap to include
    # since both are cached separately and don't add a new upstream call here.
    fg = fear_greed()
    st = stocks()
    errors = (bf.get("errors", []) + dl.get("errors", [])
              + nw.get("errors", []) + ls.get("errors", []) + gm.get("errors", [])
              + oc_delta.get("errors", [])[:2]     # cap: 15 symbols could spam the bar
              + fg.get("errors", []) + st.get("errors", []))
    return {
        "generated_at": int(time.time()),
        "futures": bf.get("rows", []),
        "positioning": ls.get("rows", []),
        "onchain": dl,
        "global_mkt": gm,
        "fear_greed": fg,
        "stocks": st.get("rows", []),
        # Calendar failures stay silent (events just don't render) — a dead feed
        # shouldn't paint the page's error bar red.
        "calendar": econ_calendar().get("events", []),
        "news": nw.get("items", []),
        "news_sentiment": nw.get("sentiment"),
        "errors": errors,
    }
