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
_UA = "WolfScanner-MarketIntel/1.0 (+https://localhost)"


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


# ── top-level aggregator used by the web route ──────────────────────────────
def market_intel(top_n: int = 15) -> dict:
    bf = binance_futures(top_n=top_n)
    dl = defillama()
    nw = news()
    errors = bf.get("errors", []) + dl.get("errors", []) + nw.get("errors", [])
    return {
        "generated_at": int(time.time()),
        "futures": bf.get("rows", []),
        "onchain": dl,
        "news": nw.get("items", []),
        "errors": errors,
    }
