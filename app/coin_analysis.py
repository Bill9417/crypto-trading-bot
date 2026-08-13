"""
🔍 One coin, everything this system knows about it.

Search a ticker, get the same read the scanners use — funding, open interest,
taker pressure, retail positioning, relative strength, price structure — each
one measured, each one with a direction, and the S2/S4/crowd-radar/flip
verdicts alongside.

WHY THERE ARE NO POINTS HERE
The panel this was modelled on scores factors "+24 分 / −7 分" and sums them.
Those weights are the whole claim: they say a CVD-confirmed OI rise is six
times more informative than a funding reading, and nothing in that panel or
this repo has measured that. Inventing them here would manufacture the exact
thing this project keeps disproving — a number that looks like an edge and is
not. This repo has measured 48 high-win-rate setups and found 46 losing money;
a weighted score is how that happens.

So the composite is a COUNT: how many independent factors currently lean long,
how many lean short. A count claims only what it can support — "six of eight
readings are bullish right now" — and cannot be mistaken for an expected
return. If weights are ever earned by measurement, they belong here then.

Every factor is a fact plus a rule for reading it. The facts are exact; the
readings are conventional and labelled as such.

Data: Binance public endpoints only — no key, no account. Market cap comes from
openInterestHist's CMCCirculatingSupply × price, so OI/市值 costs no extra call.
"""
import os
import time

import requests

BASE = "https://fapi.binance.com"
TIMEOUT = 12
CACHE_TTL = float(os.getenv("COIN_CACHE_TTL", "60"))
_cache: dict = {}

LONG, SHORT, NEUTRAL = "long", "short", "neutral"

# Funding is judged against the symbol's OWN recent median rather than an
# absolute number, for the same reason crowd_radar ranks OI per symbol: 0.01%
# is ordinary on one perp and extreme on another.
FUNDING_HOT_MULT = 3.0
# Relative strength vs BTC over the same window. Below this the coin is simply
# a worse expression of the same move.
RS_WEAK_PCT = -5.0
RS_STRONG_PCT = 5.0


def _get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def resolve(query: str) -> str:
    """'beat', 'BEAT', 'BEATUSDT', 'BEAT/USDT:USDT' → 'BEATUSDT' or ''."""
    q = (query or "").strip().upper()
    if not q:
        return ""
    # Only the ccxt-style suffix is stripped; INTERNAL whitespace is a reject,
    # not something to squeeze out. Deleting it turned "A B/C" into "ABUSDT" —
    # a different, real ticker — so a typo silently analysed the wrong coin.
    q = q.split("/")[0].split(":")[0]
    if not q or any(ch.isspace() for ch in q):
        return ""
    if not q.isalnum():
        return ""
    return q if q.endswith("USDT") else q + "USDT"


def search(query: str, universe: list, limit: int = 8) -> list:
    """Ticker matches for the search box: exact first, then prefix, then
    substring — so typing 'BE' offers BEAT before SBEAT."""
    q = (query or "").strip().upper()
    if not q:
        return []
    bases = [s[:-4] for s in universe if s.endswith("USDT")]
    exact = [b for b in bases if b == q]
    pre = sorted(b for b in bases if b.startswith(q) and b != q)
    sub = sorted(b for b in bases if q in b and not b.startswith(q))
    return (exact + pre + sub)[:limit]


# ── the factors ──────────────────────────────────────────────────────────────
def _pct(a, b):
    return (a - b) / abs(b) * 100 if b else 0.0


def read_momentum(closes: list, bars: int) -> tuple:
    if len(closes) <= bars:
        return None, NEUTRAL
    chg = _pct(closes[-1], closes[-1 - bars])
    return chg, (LONG if chg > 0 else SHORT if chg < 0 else NEUTRAL)


def read_funding(current: float, history: list) -> dict:
    """Funding against its own recent median.

    An absolute threshold ("0.01% is high") is wrong per symbol and per regime.
    What matters is whether THIS perp is paying unusually to hold one side.
    Crowded longs pay shorts, so extreme positive funding is a crowded-long
    warning, not a bullish reading — the sign is deliberately inverted here and
    that is the conventional read, not a measured one.
    """
    if not history:
        return {"value": current, "read": NEUTRAL, "median": None,
                "note": "沒有足夠的資金費率歷史"}
    med = sorted(history)[len(history) // 2]
    hot = abs(current) > abs(med) * FUNDING_HOT_MULT and abs(current) > 0.0001
    if not hot:
        return {"value": current, "median": med, "read": NEUTRAL,
                "note": f"在常態範圍內（中位 {med * 100:+.4f}%），不做多空偏見"}
    if current > 0:
        return {"value": current, "median": med, "read": SHORT,
                "note": "多單付錢給空單且異常高 —— 擁擠的多方"}
    return {"value": current, "median": med, "read": LONG,
            "note": "空單付錢給多單且異常高 —— 擁擠的空方"}


def read_taker(buy_vol: float, sell_vol: float) -> dict:
    """Taker buy vs sell volume — who was the AGGRESSOR.

    This is the honest version of the "CVD" line: it is measured, unlike open
    interest which cannot name the aggressive side because every contract has
    both. Ratio > 1 means market buyers lifted more than sellers hit.
    """
    total = buy_vol + sell_vol
    if total <= 0:
        return {"ratio": None, "read": NEUTRAL, "note": "沒有成交量資料"}
    ratio = buy_vol / sell_vol if sell_vol else float("inf")
    share = buy_vol / total * 100
    if ratio >= 1.05:
        return {"ratio": ratio, "share": share, "read": LONG,
                "note": f"主動買盤佔 {share:.1f}% —— 買方主導"}
    if ratio <= 0.95:
        return {"ratio": ratio, "share": share, "read": SHORT,
                "note": f"主動買盤僅 {share:.1f}% —— 賣方主導"}
    return {"ratio": ratio, "share": share, "read": NEUTRAL,
            "note": f"主動買賣接近均衡（買 {share:.1f}%）"}


def read_oi(oi_pct: float, px_pct: float) -> dict:
    """The four-state read, shared with crowd_radar so the two pages cannot
    disagree about the same symbol."""
    import crowd_radar as C
    state = C.oi_read(oi_pct, px_pct)
    read = LONG if state in (C.LONGS_OPENING, C.SHORTS_CLOSING) else SHORT
    return {"oi_pct": oi_pct, "state": state, "read": read,
            "note": C.READ_ZH.get(state, "")}


def read_ls_ratio(ratio: float) -> dict:
    """Retail account long/short. Read as a CONTRARIAN tell at extremes and
    ignored otherwise, which is the conventional use — and unmeasured here."""
    if ratio is None:
        return {"ratio": None, "read": NEUTRAL, "note": "沒有多空比資料"}
    if ratio >= 2.0:
        return {"ratio": ratio, "read": SHORT,
                "note": f"散戶 {ratio:.2f} 倍偏多 —— 過度擁擠（反向解讀）"}
    if ratio <= 0.5:
        return {"ratio": ratio, "read": LONG,
                "note": f"散戶 {ratio:.2f} 倍偏空 —— 過度擁擠（反向解讀）"}
    return {"ratio": ratio, "read": NEUTRAL,
            "note": f"多空情緒均衡（多方 {ratio / (1 + ratio) * 100:.0f}%）"}


def read_rs(coin_pct: float, btc_pct: float) -> dict:
    """Relative strength over the same window. A coin lagging BTC in a rally
    carries its own selling pressure; leading it means real demand."""
    diff = coin_pct - btc_pct
    if diff <= RS_WEAK_PCT:
        return {"diff": diff, "read": SHORT,
                "note": f"相較 BTC 弱 {abs(diff):.1f}% —— 幣種本身存在額外賣壓"}
    if diff >= RS_STRONG_PCT:
        return {"diff": diff, "read": LONG,
                "note": f"相較 BTC 強 {diff:.1f}% —— 幣種本身有額外買盤"}
    return {"diff": diff, "read": NEUTRAL,
            "note": f"與 BTC 同步（差 {diff:+.1f}%）"}


def read_structure(ohlcv: list) -> dict:
    """Price structure via the flip detector's own pivots, so 'resistance' means
    the same thing on this page as it does in the 壓力翻支撐 alerts."""
    import breakout_flip as B
    if not ohlcv or len(ohlcv) < 60:
        return {"read": NEUTRAL, "note": "K 線不足"}
    closed = ohlcv[:-1]
    highs = [float(c[2]) for c in closed]
    price = float(closed[-1][4])
    at = len(closed) - 1
    room = B.overhead_room(highs, price, at)
    sig = B.detect(closed)
    if sig:
        return {"read": LONG, "flip": True,
                "note": f"壓力翻支撐成立（{sig['zone_top']:.6g} 回踩不破）"}
    if room["blue_sky"]:
        return {"read": LONG, "blue_sky": True,
                "note": "上方無前高壓力（區間新高）"}
    return {"read": NEUTRAL, "room_pct": room["room_pct"],
            "note": f"上方最近壓力還有 {room['room_pct']:.1f}%"}


# ── assembly ─────────────────────────────────────────────────────────────────
def _factor(key, label, value, r: dict):
    return {"key": key, "label": label, "value": value,
            "read": r.get("read", NEUTRAL), "note": r.get("note", "")}


def analyse(query: str, now: float = None) -> dict:
    """Everything, for one symbol. Cached briefly — the page polls."""
    sym = resolve(query)
    if not sym:
        return {"ok": False, "error": "看不懂這個代號"}
    now = now if now is not None else time.time()
    hit = _cache.get(sym)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        out = _build(sym)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", 0)
        msg = f"{sym} 在幣安永續找不到" if code == 400 else f"抓資料失敗：{e}"
        return {"ok": False, "error": msg, "symbol": sym}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"抓資料失敗：{str(e)[:110]}", "symbol": sym}
    _cache[sym] = (now, out)
    return out


def _build(sym: str) -> dict:
    base = sym[:-4]
    k1h = _get("/fapi/v1/klines", symbol=sym, interval="1h", limit=48)
    k15 = _get("/fapi/v1/klines", symbol=sym, interval="15m", limit=400)
    tick = _get("/fapi/v1/ticker/24hr", symbol=sym)
    prem = _get("/fapi/v1/premiumIndex", symbol=sym)
    frh = _get("/fapi/v1/fundingRate", symbol=sym, limit=30)
    oih = _get("/futures/data/openInterestHist", symbol=sym, period="15m", limit=100)
    try:
        lsr = _get("/futures/data/topLongShortPositionRatio",
                   symbol=sym, period="1h", limit=1)
    except Exception:  # noqa: BLE001 — a missing ratio is not a failed page
        lsr = []
    btc = _get("/fapi/v1/klines", symbol="BTCUSDT", interval="1h", limit=48)

    closes = [float(c[4]) for c in k1h]
    price = float(tick["lastPrice"])
    chg24 = float(tick["priceChangePercent"])

    # taker aggression over the last 4 closed hours
    buy = sum(float(c[9]) for c in k1h[-5:-1])
    total = sum(float(c[5]) for c in k1h[-5:-1])
    taker = read_taker(buy, max(0.0, total - buy))

    # OI: 1h change, plus notional and its share of market cap
    oi_amt = [float(r["sumOpenInterest"]) for r in oih]
    oi_val = [float(r["sumOpenInterestValue"]) for r in oih]
    oi_pct = _pct(oi_amt[-1], oi_amt[-5]) if len(oi_amt) >= 5 else 0.0
    px_1h, _ = read_momentum(closes, 1)
    oi = read_oi(oi_pct, px_1h or 0.0)
    supply = float(oih[-1].get("CMCCirculatingSupply") or 0) if oih else 0
    mcap = supply * price if supply else None
    oi_share = (oi_val[-1] / mcap * 100) if mcap else None

    fr = read_funding(float(prem["lastFundingRate"]),
                      [float(r["fundingRate"]) for r in frh])
    ls = read_ls_ratio(float(lsr[-1]["longShortRatio"]) if lsr else None)
    m1, m1r = read_momentum(closes, 1)
    m24, m24r = read_momentum(closes, 24)
    btc_closes = [float(c[4]) for c in btc]
    rs = read_rs(m24 or 0.0, _pct(btc_closes[-1], btc_closes[-25])
                 if len(btc_closes) > 24 else 0.0)
    struct = read_structure(k15)

    factors = [
        _factor("oi", "市場結構 · 未平倉", f"{oi_pct:+.2f}% (1h)", oi),
        _factor("taker", "主動買賣 · 誰在追價",
                f"買 {taker.get('share') or 0:.1f}%", taker),
        _factor("structure", "價格結構", struct.get("note", ""), struct),
        _factor("m1", "動能 1H", f"{m1:+.2f}%" if m1 is not None else "—",
                {"read": m1r, "note": "近一小時價格方向"}),
        _factor("m24", "動能 24H", f"{m24:+.2f}%" if m24 is not None else "—",
                {"read": m24r, "note": "近一日價格方向"}),
        _factor("funding", "資金費率",
                f"{fr['value'] * 100:+.4f}%", fr),
        _factor("ls", "多空比 · 散戶",
                f"{ls['ratio']:.2f}" if ls["ratio"] else "—", ls),
        _factor("rs", "相對強弱 vs BTC", f"{rs['diff']:+.1f}%", rs),
    ]

    longs = sum(1 for f in factors if f["read"] == LONG)
    shorts = sum(1 for f in factors if f["read"] == SHORT)
    lean = ("偏多" if longs > shorts else
            "偏空" if shorts > longs else "中性")

    return {
        "ok": True, "symbol": sym, "base": base, "price": price,
        "chg_24h": chg24,
        "turnover": float(tick.get("quoteVolume") or 0),
        # BINANCE ONLY, and labelled that way on the page. The panel this was
        # modelled on shows a CoinGlass AGGREGATE across every venue, so its
        # OI/市值 of 20.2% and this one's 3.6% are not a discrepancy — they are
        # different measurements. Presenting a single-venue number under an
        # aggregate's name would be the more comfortable lie.
        "oi_usd": oi_val[-1] if oi_val else None,
        "oi_venue": "Binance 永續",
        "mcap": mcap, "oi_share": oi_share,
        "funding": fr["value"], "funding_median": fr.get("median"),
        "factors": factors,
        "long_count": longs, "short_count": shorts,
        "neutral_count": len(factors) - longs - shorts,
        "lean": lean,
        "spark": [float(c[4]) for c in k15[-48:]],
        "strategies": _strategies(k15),
        "ts": time.time(),
    }


def _strategies(k15: list) -> list:
    """What this repo's own scanners say about the symbol right now, with each
    one's honest status attached — a verdict from an unvalidated strategy is
    information about the chart, not about the future."""
    out = []
    try:
        import strategy2_meter as S2
        m = S2.compute_signal(k15)
        score = m.get("score")
        out.append({"name": "S2 儀表", "value":
                    f"{score:.0f}/100" if score is not None else "—",
                    "read": LONG if (score or 50) >= 60 else
                            SHORT if (score or 50) <= 40 else NEUTRAL,
                    "note": "多空儀表分數（>60 偏多 / <40 偏空）"})
    except Exception:  # noqa: BLE001
        pass
    try:
        import breakout_flip as B
        sig = B.detect(k15[:-1])
        out.append({"name": "壓力翻支撐", "value": "成立" if sig else "無",
                    "read": LONG if sig else NEUTRAL,
                    "note": f"回測 {B.MEASURED['n']} 筆，信賴區間仍含 0 —— 未驗證"})
    except Exception:  # noqa: BLE001
        pass
    return out


DISCLAIMER = ("⚠️ 這頁是「現在的條件」，不是預測。每個因子都是實際量到的數字，"
              "但把它們加權成一個分數等於宣稱知道哪個因子比較重要 —— "
              "本專案沒有量過，所以這裡只數「幾項偏多、幾項偏空」。"
              "本專案量過 48 組高勝率設定有 46 組在賠錢。")

_universe: tuple = (0.0, [])
UNIVERSE_TTL = 600


def universe(now: float = None) -> list:
    """Every liquid USDT perp ticker, cached — the search box's corpus.

    Lives here rather than in app.py so the web layer needs no HTTP client of
    its own; the first version reached for `requests` in app.py, which does not
    import it, and the search box silently returned nothing.
    """
    global _universe
    now = now if now is not None else time.time()
    if _universe[1] and now - _universe[0] < UNIVERSE_TTL:
        return _universe[1]
    rows = _get("/fapi/v1/ticker/24hr")
    syms = sorted(r["symbol"] for r in rows
                  if r["symbol"].endswith("USDT")
                  and float(r.get("quoteVolume") or 0) > 1e6)
    _universe = (now, syms)
    return syms

