"""Stock watchlists for the /stocks page — Taiwan top-50 + US top-100.

Fully isolated from the scanner/bot: read-only public endpoints only, no keys.
  * Taiwan quotes ... TWSE MIS realtime API (mis.twse.com.tw), one batch call
  * US quotes ....... Yahoo Finance v7 spark batch endpoint (no auth needed)
  * Perp prices ..... Binance USDⓈ-M futures /fapi/v1/ticker/price (public)

The US table carries an extra comparison: Binance lists 24/7 "TradFi" stock
perps (AAPLUSDT, NVDAUSDT, …— the same 118 symbols the trading engines exclude
via EXCLUDE_TRADFI_PERPS). While the US market is closed those perps keep
trading, so `perp vs stock` is the crypto market's live estimate of where the
stock should open; while it is open the gap is the perp's premium/discount.

Every source is cached in-process (short TTL while its market is open, long
while closed) and falls back to the last good snapshot on fetch errors, so the
page never breaks and never hammers the public APIs.
"""

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ── Watchlists ────────────────────────────────────────────────────────────
# Static by design ("top 50" churns slowly): edit here to swap constituents.
# Taiwan ≈ the FTSE TWSE Taiwan 50 index as of 2026-07. `ch` is the MIS API
# channel (tse_ = listed, otc_ = TPEx). Chinese names come from the API itself.
TW50 = [
    ("2330", "TSMC"), ("2317", "Hon Hai (Foxconn)"), ("2454", "MediaTek"),
    ("2308", "Delta Electronics"), ("2382", "Quanta Computer"), ("2881", "Fubon Financial"),
    ("2882", "Cathay Financial"), ("2412", "Chunghwa Telecom"), ("2891", "CTBC Financial"),
    ("3711", "ASE Technology"), ("2886", "Mega Financial"), ("2303", "UMC"),
    ("2884", "E.Sun Financial"), ("2885", "Yuanta Financial"), ("1216", "Uni-President"),
    ("2357", "ASUS"), ("2892", "First Financial"), ("3034", "Novatek"),
    ("2379", "Realtek"), ("5880", "Taiwan Cooperative Fin"), ("2880", "Hua Nan Financial"),
    ("2890", "SinoPac Financial"), ("5871", "Chailease"), ("2327", "Yageo"),
    ("2345", "Accton"), ("2887", "Taishin FHC"), ("2883", "KGI Financial"),
    ("6669", "Wiwynn"), ("3231", "Wistron"), ("2002", "China Steel"),
    ("1301", "Formosa Plastics"), ("2207", "Hotai Motor"), ("2603", "Evergreen Marine"),
    ("2912", "President Chain Store"), ("1303", "Nan Ya Plastics"), ("3008", "Largan Precision"),
    ("3037", "Unimicron"), ("3017", "Asia Vital Components"), ("4938", "Pegatron"),
    ("2376", "Gigabyte"), ("3045", "Taiwan Mobile"), ("4904", "Far EasTone"),
    ("6505", "Formosa Petrochemical"), ("1101", "Taiwan Cement"), ("1326", "Formosa Chemicals"),
    ("2301", "Lite-On"), ("2618", "EVA Air"), ("3661", "Alchip"),
    ("2395", "Advantech"), ("6488", "GlobalWafers"),
]
_TW_OTC = {"6488"}  # TPEx-listed → otc_ channel on the MIS API

# US ≈ top 100 by market cap as of 2026-07 (Yahoo tickers).
US100 = [
    ("NVDA", "NVIDIA"), ("MSFT", "Microsoft"), ("AAPL", "Apple"),
    ("GOOGL", "Alphabet"), ("AMZN", "Amazon"), ("META", "Meta Platforms"),
    ("AVGO", "Broadcom"), ("TSLA", "Tesla"), ("BRK-B", "Berkshire Hathaway"),
    ("LLY", "Eli Lilly"), ("WMT", "Walmart"), ("JPM", "JPMorgan Chase"),
    ("V", "Visa"), ("ORCL", "Oracle"), ("MA", "Mastercard"),
    ("NFLX", "Netflix"), ("XOM", "Exxon Mobil"), ("COST", "Costco"),
    ("JNJ", "Johnson & Johnson"), ("HD", "Home Depot"), ("PLTR", "Palantir"),
    ("PG", "Procter & Gamble"), ("BAC", "Bank of America"), ("ABBV", "AbbVie"),
    ("CVX", "Chevron"), ("KO", "Coca-Cola"), ("GE", "GE Aerospace"),
    ("AMD", "AMD"), ("CSCO", "Cisco"), ("TMUS", "T-Mobile US"),
    ("WFC", "Wells Fargo"), ("CRM", "Salesforce"), ("PM", "Philip Morris"),
    ("IBM", "IBM"), ("UNH", "UnitedHealth"), ("MS", "Morgan Stanley"),
    ("ABT", "Abbott"), ("LIN", "Linde"), ("GS", "Goldman Sachs"),
    ("INTU", "Intuit"), ("MCD", "McDonald's"), ("AXP", "American Express"),
    ("DIS", "Disney"), ("MRK", "Merck"), ("RTX", "RTX"),
    ("NOW", "ServiceNow"), ("PEP", "PepsiCo"), ("CAT", "Caterpillar"),
    ("UBER", "Uber"), ("TXN", "Texas Instruments"),
    # ── ranks ≈ 51–100 ──
    ("BKNG", "Booking Holdings"), ("QCOM", "Qualcomm"), ("ADBE", "Adobe"),
    ("AMGN", "Amgen"), ("SPGI", "S&P Global"), ("MU", "Micron"),
    ("ISRG", "Intuitive Surgical"), ("NEE", "NextEra Energy"), ("PGR", "Progressive"),
    ("UNP", "Union Pacific"), ("AMAT", "Applied Materials"), ("GEV", "GE Vernova"),
    ("ETN", "Eaton"), ("BSX", "Boston Scientific"), ("HON", "Honeywell"),
    ("C", "Citigroup"), ("ANET", "Arista Networks"), ("LOW", "Lowe's"),
    ("TJX", "TJX"), ("COP", "ConocoPhillips"), ("BLK", "BlackRock"),
    ("SCHW", "Charles Schwab"), ("SYK", "Stryker"), ("VZ", "Verizon"),
    ("T", "AT&T"), ("PANW", "Palo Alto Networks"), ("CRWD", "CrowdStrike"),
    ("KLAC", "KLA"), ("LRCX", "Lam Research"), ("ADP", "ADP"),
    ("DHR", "Danaher"), ("GILD", "Gilead"), ("VRTX", "Vertex Pharma"),
    ("LMT", "Lockheed Martin"), ("MDT", "Medtronic"), ("INTC", "Intel"),
    ("CDNS", "Cadence"), ("SNPS", "Synopsys"), ("MRVL", "Marvell"),
    ("KKR", "KKR"), ("BX", "Blackstone"), ("APP", "AppLovin"),
    ("CEG", "Constellation Energy"), ("ABNB", "Airbnb"), ("DASH", "DoorDash"),
    ("COIN", "Coinbase"), ("MSTR", "Strategy (MicroStrategy)"), ("HOOD", "Robinhood"),
    ("SBUX", "Starbucks"), ("DE", "Deere"),
]

# Yahoo ticker → Binance TradFi perp. Only symbols confirmed live in
# fapi exchangeInfo (underlyingType EQUITY / contractType TRADIFI_PERPETUAL).
PERP_MAP = {t: f"{t}USDT" for t in (
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "LLY",
    "WMT", "JPM", "V", "ORCL", "NFLX", "COST", "HD", "PLTR", "AMD", "CSCO",
    "CRM", "IBM", "DIS", "NOW", "CAT", "UBER", "TXN",
    "QCOM", "ADBE", "MU", "AMAT", "CRWD", "KLAC", "LRCX", "INTC", "MRVL",
    "BX", "COIN", "MSTR", "HOOD",
)}
PERP_MAP["BRK-B"] = "BRKBUSDT"

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"}
_TIMEOUT = 12


def _f(val):
    """TWSE/Yahoo numeric fields arrive as strings, '-' or missing → float|None."""
    try:
        v = float(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── Market sessions ───────────────────────────────────────────────────────

def _in_session(tz_name, open_hm, close_hm):
    now = datetime.now(ZoneInfo(tz_name))
    o = now.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    c = now.replace(hour=close_hm[0], minute=close_hm[1], second=0, microsecond=0)
    return now.weekday() < 5 and o <= now < c


def _session(tz_name, open_hm, close_hm, hours_label, quote_ts):
    """Open/closed + countdown epochs. The clock alone can't see holidays, so a
    clock-open market whose freshest quote is >30 min old is reported closed
    with holiday=True (exactly what happens on e.g. July 4th)."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    open_dt = now.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    close_dt = now.replace(hour=close_hm[0], minute=close_hm[1], second=0, microsecond=0)
    in_session = now.weekday() < 5 and open_dt <= now < close_dt
    quote_age = (time.time() - quote_ts) if quote_ts else None
    holiday = bool(in_session and (quote_age is None or quote_age > 1800))
    state = "open" if (in_session and not holiday) else "closed"

    nxt = open_dt
    while nxt <= now or nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return {
        "state": state,
        "holiday": holiday,
        "next_open_ts": nxt.timestamp(),
        "next_close_ts": close_dt.timestamp() if state == "open" else None,
        "hours": hours_label,
        "tz": tz_name,
        "quote_age_sec": round(quote_age) if quote_age is not None else None,
    }


# ── Fetchers (network) ────────────────────────────────────────────────────

def _fetch_tw():
    """One MIS batch per 30 codes → rows in TW50 order."""
    by_code = {}
    for chunk in _chunks(TW50, 30):
        ex_ch = "|".join(
            f"{'otc' if code in _TW_OTC else 'tse'}_{code}.tw" for code, _ in chunk)
        r = requests.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
            headers={**_UA, "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
            timeout=_TIMEOUT)
        r.raise_for_status()
        for m in r.json().get("msgArray", []):
            by_code[m.get("c")] = m

    rows, latest_ts = [], 0.0
    for code, name_en in TW50:
        m = by_code.get(code, {})
        last = _f(m.get("z")) or _f(m.get("pz"))
        prev = _f(m.get("y"))
        ts = _f(m.get("tlong"))
        if ts:
            latest_ts = max(latest_ts, ts / 1000.0)
        rows.append({
            "code": code,
            "name": m.get("n") or name_en,     # Chinese short name from the API
            "name_en": name_en,
            "price": last,
            "prev_close": prev,
            "change_pct": round((last - prev) / prev * 100, 2) if last and prev else None,
            "high": _f(m.get("h")),
            "low": _f(m.get("l")),
            "volume_lots": _f(m.get("v")),     # cumulative 張 (1 lot = 1000 shares)
        })
    return {"rows": rows, "quote_ts": latest_ts}


def _fetch_us():
    """Yahoo v7 spark in batches → rows in US100 order. The endpoint hard-caps
    at 20 symbols per request (21+ → HTTP 400)."""
    meta_by_sym = {}
    for chunk in _chunks(US100, 20):
        r = requests.get(
            "https://query1.finance.yahoo.com/v7/finance/spark",
            params={"symbols": ",".join(t for t, _ in chunk),
                    "range": "1d", "interval": "15m"},
            headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        for item in (r.json().get("spark", {}).get("result") or []):
            resp = (item.get("response") or [{}])[0]
            meta_by_sym[item.get("symbol")] = resp.get("meta", {})

    rows, latest_ts = [], 0.0
    for ticker, name in US100:
        meta = meta_by_sym.get(ticker, {})
        last = _f(meta.get("regularMarketPrice"))
        prev = _f(meta.get("previousClose")) or _f(meta.get("chartPreviousClose"))
        ts = _f(meta.get("regularMarketTime"))
        if ts:
            latest_ts = max(latest_ts, ts)
        rows.append({
            "ticker": ticker,
            "name": name,
            "price": last,
            "prev_close": prev,
            "change_pct": round((last - prev) / prev * 100, 2) if last and prev else None,
            "high": _f(meta.get("regularMarketDayHigh")),
            "low": _f(meta.get("regularMarketDayLow")),
            "volume": _f(meta.get("regularMarketVolume")),
            "perp_symbol": PERP_MAP.get(ticker),
        })
    return {"rows": rows, "quote_ts": latest_ts}


def _fetch_perps():
    """Last price of every Binance USDⓈ-M symbol (1 request), filtered to ours."""
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                     headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    wanted = set(PERP_MAP.values())
    return {t["symbol"]: float(t["price"]) for t in r.json()
            if t.get("symbol") in wanted}


# ── Cache (per source, stale-on-error) ────────────────────────────────────

_lock = threading.Lock()
_cache = {
    "tw":   {"ts": 0.0, "data": None, "error": None},
    "us":   {"ts": 0.0, "data": None, "error": None},
    "perp": {"ts": 0.0, "data": None, "error": None},
}


def _cached(key, ttl, fetcher):
    """Serve from cache within ttl; on fetch failure keep the stale snapshot
    and record the error instead of raising."""
    with _lock:
        slot = _cache[key]
        if slot["data"] is not None and time.time() - slot["ts"] < ttl:
            return slot["data"], slot["error"]
        try:
            slot["data"] = fetcher()
            slot["error"] = None
        except Exception as exc:  # noqa: BLE001 — page must never 500
            slot["error"] = f"{type(exc).__name__}: {exc}"[:200]
        slot["ts"] = time.time()
        return slot["data"], slot["error"]


# ── Public API ────────────────────────────────────────────────────────────

def empty_payload(error=""):
    return {"generated_at": time.time(),
            "tw": {"market": None, "rows": [], "error": error or None},
            "us": {"market": None, "rows": [], "error": error or None},
            "perp_error": None}


def build_stocks():
    tw_open = _in_session("Asia/Taipei", (9, 0), (13, 30))
    us_open = _in_session("America/New_York", (9, 30), (16, 0))

    tw, tw_err = _cached("tw", 30 if tw_open else 600, _fetch_tw)
    us, us_err = _cached("us", 30 if us_open else 600, _fetch_us)
    perps, perp_err = _cached("perp", 30, _fetch_perps)  # perps trade 24/7
    tw, us, perps = tw or {}, us or {}, perps or {}

    us_rows = us.get("rows", [])
    for row in us_rows:
        perp_px = perps.get(row.get("perp_symbol"))
        row["perp_price"] = perp_px
        row["perp_diff_pct"] = (
            round((perp_px - row["price"]) / row["price"] * 100, 2)
            if perp_px and row.get("price") else None)

    return {
        "generated_at": time.time(),
        "tw": {
            "market": _session("Asia/Taipei", (9, 0), (13, 30),
                               "09:00–13:30 台北", tw.get("quote_ts")),
            "rows": tw.get("rows", []),
            "error": tw_err,
        },
        "us": {
            "market": _session("America/New_York", (9, 30), (16, 0),
                               "09:30–16:00 New York", us.get("quote_ts")),
            "rows": us_rows,
            "error": us_err,
        },
        "perp_error": perp_err,
    }
