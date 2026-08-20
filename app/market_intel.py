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

import concurrent.futures as _cf
import datetime as _dt
import json
import os
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


# ── disk cache with stale-if-error ──────────────────────────────────────────
# The in-memory cache above is PER PROCESS, and four of them run (web, bot, S2,
# S3) plus every /market visitor. For a provider that rate-limits by IP that
# multiplies into a ban: 2026-08-08 the ForexFactory weekly calendar had been
# answering 429 for long enough that the daily report printed "無 — 平靜的總經日"
# every single day. An empty result rendered as "nothing scheduled" is a
# fabricated fact, not a missing one.
#
# So the calendar caches to DISK (shared by all four processes, survives
# restarts) and, when the fetch fails, keeps serving the last good copy with
# `stale: True` rather than an empty list. Callers must render staleness.
_DISK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _disk_cached(key: str, ttl: float, producer, is_good=None):
    """Fresh → serve. Stale but producer succeeds → refresh. Producer fails →
    serve the old copy tagged {'stale': True, 'stale_age': seconds}; only when
    there is nothing at all does the caller see the empty producer result."""
    path = os.path.join(_DISK_CACHE_DIR, f"{key}.json")
    now = time.time()
    old, old_ts = None, 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        old, old_ts = blob.get("data"), float(blob.get("ts") or 0)
    except Exception:  # noqa: BLE001 — missing/corrupt cache = cold start
        pass
    if old is not None and (now - old_ts) < ttl:
        return {**old, "stale": False}

    try:
        fresh = producer()
    except Exception as exc:  # noqa: BLE001 — a dead feed must not raise here
        fresh = {"errors": [f"{key}: {exc}"]}
    good = is_good(fresh) if is_good else not fresh.get("errors")
    if good:
        try:
            os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"      # four processes share this file
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ts": now, "data": fresh}, fh)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 — caching is an optimisation, not a duty
            pass
        return {**fresh, "stale": False}
    if old is not None:
        return {**old, "stale": True, "stale_age": now - old_ts,
                "errors": fresh.get("errors") or []}
    return {**fresh, "stale": False}


# ── low-level HTTP helpers ──────────────────────────────────────────────────
# A real browser UA — Binance's /futures/data endpoints 403 unusual UAs
# (e.g. one mentioning "localhost"); the news/TVL feeds accept it fine too.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _get_json(url: str, timeout: float = 8.0, headers: dict = None):
    hdrs = {"User-Agent": _UA, "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
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


# ── Per-feed memory ──────────────────────────────────────────────────────────
# One publisher rate-limiting us used to cost its headlines entirely AND put a
# permanent-looking warning on the page: CryptoSlate 429s, the source vanishes
# from the merged stream, and the reader is told something is broken when the
# only thing that happened is "not so fast".
#
# Two behaviours fix that, and they are the same two this codebase applies to
# every other flaky feed:
#   · LAST GOOD — a fetch that fails serves the previous items, aged, rather
#     than nothing. A missing reading must not silently become an absence.
#   · BACK OFF — after a 429, stop asking for a while. Retrying a rate limit on
#     the next 5-minute tick is what earns the next 429; the backoff doubles to
#     a ceiling and resets on the first success.
# Disk-backed, so the web process and the scanner share one memory instead of
# each holding the publisher to its own limit.
FEED_BACKOFF_SEC = float(os.getenv("NEWS_BACKOFF_SEC", "1800"))
FEED_BACKOFF_MAX = float(os.getenv("NEWS_BACKOFF_MAX_SEC", "21600"))
# How old a feed's cached items may be before its absence is worth reporting.
FEED_STALE_REPORT_SEC = float(os.getenv("NEWS_STALE_REPORT_SEC", "7200"))
_FEED_STATE_FILE = os.path.join(_DISK_CACHE_DIR, "news_feeds.json")


def _feed_state() -> dict:
    try:
        with open(_FEED_STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = cold start
        return {}


def _save_feed_state(state: dict) -> None:
    try:
        os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
        tmp = _FEED_STATE_FILE + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _FEED_STATE_FILE)
    except Exception:  # noqa: BLE001 — news must not fail on a cache write
        pass


def _is_rate_limited(exc: Exception) -> bool:
    return "429" in str(exc) or "too many requests" in str(exc).lower()


def _news(per_feed: int = 6, now: float = None) -> dict:
    now = time.time() if now is None else now
    state = _feed_state()
    out = {"items": [], "errors": [], "stale_sources": []}
    for source, url in NEWS_FEEDS:
        rec = state.get(source) or {}
        until = float(rec.get("until") or 0)
        fetched = None
        if until > now:
            pass                       # in backoff — do not ask, serve memory
        else:
            try:
                fetched = _parse_rss(source, _get_text(url, timeout=8.0), per_feed)
                rec = {"items": fetched, "ts": now, "until": 0, "wait": 0}
            except Exception as e:  # noqa: BLE001
                if _is_rate_limited(e):
                    wait = min(max(float(rec.get("wait") or 0) * 2, FEED_BACKOFF_SEC),
                               FEED_BACKOFF_MAX)
                    rec = {**rec, "until": now + wait, "wait": wait}
                else:
                    rec = {**rec, "err": str(e)[:120]}
        state[source] = rec

        items = fetched if fetched is not None else (rec.get("items") or [])
        out["items"].extend(items)
        if fetched is None:
            age = now - float(rec.get("ts") or 0)
            if items and age < FEED_STALE_REPORT_SEC:
                # Serving memory that is still recent enough to be worth
                # showing. Not an error — nothing is missing from the page.
                out["stale_sources"].append(source)
            else:
                why = ("rate limited, backing off" if until > now or rec.get("until")
                       else rec.get("err") or "unavailable")
                out["errors"].append(f"{source}: {why}")
    _save_feed_state(state)
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


def _tv_calendar(days: int = 21) -> dict:
    """High-importance US macro from TradingView's public calendar endpoint —
    the same data the site's own widget reads, and it carries forecast vs
    previous, which is what makes an event readable at a glance.

    It 403s without an Origin header (the ForexFactory feed needed none, which
    is why this wasn't noticed until that one started answering 429)."""
    out = {"events": [], "errors": []}
    now = _dt.datetime.now(_dt.timezone.utc)
    fmt = "%Y-%m-%dT00:00:00.000Z"
    url = ("https://economic-calendar.tradingview.com/events"
           f"?from={now.strftime(fmt)}"
           f"&to={(now + _dt.timedelta(days=days)).strftime(fmt)}"
           "&countries=US&minImportance=1")
    try:
        data = _get_json(url, timeout=15.0,
                         headers={"Origin": "https://www.tradingview.com",
                                  "Referer": "https://www.tradingview.com/"})
        if (data or {}).get("status") != "ok":
            raise ValueError(f"status={(data or {}).get('status')}")
        for r in data.get("result") or []:
            out["events"].append({
                "title": r.get("title"),
                "date": r.get("date"),               # ISO 8601, Zulu
                "forecast": r.get("forecast"),
                "previous": r.get("previous"),
                "source": "tradingview",
            })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"tvcal: {e}")
    return out


def _fomc_dates() -> dict:
    """FOMC meeting dates from the Fed's own calendar page — an INDEPENDENT
    source, because the ForexFactory feed above is a single point of failure
    that rate-limits by IP, and FOMC is the one macro event that reliably
    moves crypto. The page publishes years ahead, so a weekly refresh is
    generous. Rate decisions land on the SECOND day of a two-day meeting.
    """
    out = {"events": [], "errors": []}
    try:
        html = _get_text("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                         timeout=15.0)
        for ym in re.finditer(r"(\d{4})\s+FOMC\s+Meetings", html):
            year = int(ym.group(1))
            seg = html[ym.end():]
            nxt = re.search(r"\d{4}\s+FOMC\s+Meetings", seg)
            seg = seg[:nxt.start()] if nxt else seg
            months = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', seg)
            days = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', seg)
            for month, day in zip(months, days, strict=False):
                # "27-28", "17-18*" (* = press conference), "9/10" across months
                nums = re.findall(r"\d+", day)
                if not nums:
                    continue
                mon = month.strip().split("/")[-1].strip()[:3]
                try:
                    when = _dt.datetime.strptime(f"{mon} {nums[-1]} {year}", "%b %d %Y")
                except ValueError:
                    continue
                out["events"].append({
                    "title": "FOMC 利率決議 (FOMC rate decision)",
                    # 14:00 ET is the statement; stored with the offset the
                    # ForexFactory rows use so both sort together.
                    "date": when.strftime("%Y-%m-%dT14:00:00-05:00"),
                    "forecast": None, "previous": None, "source": "fed",
                })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"fomc: {e}")
    return out


def econ_calendar(ttl: float = 21600.0) -> dict:
    """High-impact US macro from THREE independent sources, disk-cached so one
    provider's IP-level rate limit cannot silently empty the calendar.

      tradingview  primary — carries forecast vs previous
      forexfactory backup  — used only when TradingView returns nothing, since
                             the two word the same print differently ("CPI m/m"
                             vs "Inflation Rate MoM") and cannot be deduped
      fed          FOMC decision days, always merged — the one date that
                   reliably moves crypto, from the Fed's own calendar page

    Adds `stale` (serving the last good copy) and `ok` so callers can say the
    calendar is unreadable instead of claiming a quiet week."""
    tv = _disk_cached("tv_cal", ttl, _tv_calendar,
                      is_good=lambda d: bool(d.get("events")))
    fomc = _disk_cached("fomc_cal", 7 * 86400.0, _fomc_dates,
                        is_good=lambda d: bool(d.get("events")))
    primary, errors = tv, list(tv.get("errors") or [])
    if not primary.get("events"):
        ff = _disk_cached("econ_cal", ttl, _econ_calendar,
                          is_good=lambda d: bool(d.get("events")))
        errors += list(ff.get("errors") or [])
        primary = ff if ff.get("events") else primary

    merged = list(primary.get("events") or [])
    have = {(e.get("date") or "")[:10] for e in merged
            if "FOMC" in (e.get("title") or "").upper()}
    merged += [e for e in (fomc.get("events") or [])
               if (e.get("date") or "")[:10] not in have]
    # The Fed page publishes years of history; only what is still ahead is
    # useful, and shipping 70 dead rows to /market on every poll is not free.
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
    merged = [e for e in merged if (e.get("date") or "") >= cutoff[:10]]
    merged.sort(key=lambda e: e.get("date") or "")
    return {"events": merged,
            "errors": errors + list(fomc.get("errors") or []),
            # FOMC dates a week old are still correct, so staleness tracks the
            # source that actually carries this week's prints
            "stale": bool(primary.get("stale")),
            "stale_age": primary.get("stale_age"),
            "sources": {"tradingview": bool(tv.get("events")) and not tv.get("stale"),
                        "fed": bool(fomc.get("events"))}}


def upcoming_macro(now=None, days: int = 7, limit: int = 8) -> dict:
    """High-impact USD prints from `now` to now+days, soonest first.

    Returns {events, stale, ok}. `ok` is False when the calendar could not be
    read at all — the caller MUST then say so rather than print "no events
    scheduled", which is a claim the data does not support.

    Each event: {when (aware datetime), title, forecast, previous, source}.
    """
    cal = econ_calendar()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    horizon = now + _dt.timedelta(days=days)
    rows = []
    for e in cal.get("events") or []:
        try:
            when = _dt.datetime.fromisoformat(e["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        if now <= when <= horizon:
            rows.append({"when": when, "title": e.get("title") or "",
                         "forecast": e.get("forecast"), "previous": e.get("previous"),
                         "source": e.get("source") or "forexfactory"})
    rows.sort(key=lambda r: r["when"])
    return {"events": rows[:limit], "stale": bool(cal.get("stale")),
            "ok": bool(cal.get("events"))}


# ── top-level aggregator used by the web route ──────────────────────────────
def _gather(jobs: dict, fallback: dict) -> dict:
    """Run independent upstream fetches concurrently, fail-soft per job.

    These are all read-only HTTP GETs to *different* providers, so running
    them serially just adds their latencies together — a cold /market took
    ~8.9s that way. Each producer already handles its own errors; this only
    has to catch a hard raise so one dead feed can't take the page with it.
    """
    out = dict(fallback)
    with _cf.ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="mktintel") as pool:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        for fut in _cf.as_completed(futures):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001 — one dead feed must not kill the page
                out[name] = dict(fallback[name])
                out[name]["errors"] = [f"{name}: {e}"]
    return out


def market_intel(top_n: int = 15, pos_n: int = 6) -> dict:
    # Stage 1 — everything that needs no input from another feed.
    first = _gather(
        {
            "bf": lambda: binance_futures(top_n=top_n),
            "dl": defillama,
            "nw": news,
            "gm": global_market,
            "fg": fear_greed,
            "st": stocks,
            "cal": econ_calendar,
        },
        {
            "bf": {"rows": [], "errors": []}, "dl": {"chains": [], "total_tvl": 0.0, "errors": []},
            "nw": {"items": [], "errors": []}, "gm": {"errors": []},
            "fg": {"errors": []}, "st": {"rows": [], "errors": []}, "cal": {"events": []},
        },
    )
    bf, dl, nw = first["bf"], first["dl"], first["nw"]
    gm, fg, st, cal = first["gm"], first["fg"], first["st"], first["cal"]

    # Stage 2 — the two panels that need the futures rows to know what to ask for.
    rows = bf.get("rows", [])
    # Reuse the top futures coins (by volume) for the positioning panel.
    pos_symbols = tuple(f"{r['base']}USDT" for r in rows[:pos_n] if r.get("base"))
    second = _gather(
        {
            "ls": (lambda: long_short(pos_symbols)) if pos_symbols else (lambda: {"rows": [], "errors": []}),
            "oc": lambda: oi_change(tuple(r["symbol"] for r in rows)),
        },
        {"ls": {"rows": [], "errors": []}, "oc": {"rows": {}, "errors": []}},
    )
    ls, oc_delta = second["ls"], second["oc"]
    for r in rows:
        r["oi_change_24h_pct"] = oc_delta.get("rows", {}).get(r["symbol"])
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
        "calendar": cal.get("events", []),
        "news": nw.get("items", []),
        "news_sentiment": nw.get("sentiment"),
        # Sources being served from memory after a rate limit. NOT an error —
        # their headlines are on the page — but the reader is entitled to know
        # a name is not live rather than being told nothing at all.
        "news_stale_sources": nw.get("stale_sources") or [],
        "errors": errors,
    }
