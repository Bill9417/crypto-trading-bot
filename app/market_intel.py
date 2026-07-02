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
]


def _parse_rss(source: str, xml_text: str, limit: int) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    # RSS 2.0: channel/item ; some feeds namespace things, but title/link/pubDate are plain.
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"source": source, "title": title, "link": link, "published": pub})
        if len(items) >= limit:
            break
    return items


def _news(per_feed: int = 6) -> dict:
    out = {"items": [], "errors": []}
    for source, url in NEWS_FEEDS:
        try:
            xml_text = _get_text(url, timeout=8.0)
            out["items"].extend(_parse_rss(source, xml_text, per_feed))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{source}: {e}")
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
    errors = (bf.get("errors", []) + dl.get("errors", [])
              + nw.get("errors", []) + ls.get("errors", []) + gm.get("errors", []))
    return {
        "generated_at": int(time.time()),
        "futures": bf.get("rows", []),
        "positioning": ls.get("rows", []),
        "onchain": dl,
        "global_mkt": gm,
        "news": nw.get("items", []),
        "errors": errors,
    }
