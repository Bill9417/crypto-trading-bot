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

import config
import strategy2_meter as S2
from indicators import adx_components, atr_pct, btc_regime_from_closes, calculate_ema

# DERIVED from the meter, never a bare literal. _strategies() runs
# S2.compute_signal on these candles; at the old 400 it took the
# `n < SIGNAL_MIN_CANDLES` early return on every coin, so the S2 row read
# "no signal" for reasons that had nothing to do with the market.
# See the same note on strategy2_scanner.CANDLES. +21 rather than +20: the
# S4 gates and the zone read run on the CLOSED slice (one bar fewer), and at
# +20 S4 answered "not enough history" on every coin for the same non-reason.
K15_CANDLES = max(400, S2.SIGNAL_MIN_CANDLES + 21)

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
# Volume: the last hour against the previous 24h, on the 15m bars already
# fetched. 2× is "a loud hour". The vegas gate that measured +0.12R asks for
# ≥3× on 1h bars against a 20-bar mean — a different base, which the note on
# the flag says rather than borrowing the number.
VOL_SURGE_MULT = float(os.getenv("COIN_VOL_SURGE_MULT", "2.0"))


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


# ── precision context: the factors this repo has actually MEASURED ──────────
# 2026-09-27. The eight readings above are conventional: exact numbers, read
# by convention. These four vote too, but each one carries a number this repo
# measured on its own signals, and the note says which — so "more precise"
# means "more of the things that were shown to matter", not more dials.
def read_regime(btc_closes: list) -> dict:
    """BTC's 1h regime — the SAME rule the S1 bot, the S2 scanner and the
    backtester read. Measured on real S2 fires: signals aligned with it reach
    TP1 first 57% of the time, counter-regime ones 41% — the single biggest
    win-rate lever found, and the reason the ⭐ tier requires alignment."""
    regime = btc_regime_from_closes(btc_closes)
    if regime == "bull":
        return {"regime": regime, "read": LONG,
                "note": "BTC 1h 站在上升的 EMA50 之上 —— 順勢做多。實測：與大盤同向的訊號 57% 先到 TP1，逆勢只有 41%"}
    if regime == "bear":
        return {"regime": regime, "read": SHORT,
                "note": "BTC 1h 跌破下降的 EMA50 —— 順勢做空。實測：與大盤同向的訊號 57% 先到 TP1，逆勢只有 41%"}
    return {"regime": regime, "read": NEUTRAL,
            "note": "BTC 盤整 —— 沒有方向濾網；⭐ 精選要求與大盤同向，所以此刻不會發"}


def read_frames(price, ema50_4h, ema200_1h) -> dict:
    """The two higher-timeframe gates the engines already use — S1's 4H EMA50
    filter and S2/S4's 1H EMA200 side — asked whether they agree."""
    if price is None or ema50_4h is None or ema200_1h is None:
        return {"read": NEUTRAL, "note": "K 線不足以算 4H EMA50 / 1H EMA200"}
    above = (float(price) > float(ema50_4h), float(price) > float(ema200_1h))
    if all(above):
        return {"read": LONG, "note": "4H EMA50 與 1H EMA200 之上 —— S1 的 4H 濾網和 S2/S4 的 EMA200 門檻都同意做多"}
    if not any(above):
        return {"read": SHORT, "note": "4H EMA50 與 1H EMA200 之下 —— 兩個時框都同意做空"}
    return {"read": NEUTRAL,
            "note": f"時框不一致（4H 在{'上' if above[0] else '下'}、1H 在{'上' if above[1] else '下'}）—— S1 的 4H 濾網會擋掉其中一邊"}


def read_adx(k1h_closed: list) -> dict:
    """Trend strength on 1h. Below the ⭐ tier's bar the reading is 'chop' and
    takes no side — a confluence signal in chop is where this repo's signals
    have measured worst."""
    comp = adx_components(k1h_closed, 14) if k1h_closed else None
    if not comp or comp.get("adx") is None:
        return {"adx": None, "read": NEUTRAL, "note": "K 線不足以算 ADX"}
    adx, th = float(comp["adx"]), float(config.STRATEGY2_PREMIUM_MIN_ADX)
    if adx < th:
        return {"adx": adx, "read": NEUTRAL,
                "note": f"ADX {adx:.1f} < {th:g} —— 盤整；共振訊號在這裡最常失準（⭐ 精選要求 ≥{th:g}）"}
    bull = float(comp.get("plus_di") or 0) > float(comp.get("minus_di") or 0)
    return {"adx": adx, "read": LONG if bull else SHORT,
            "note": f"ADX {adx:.1f} 有趨勢（≥{th:g}）· {'+DI 領先' if bull else '−DI 領先'}"
                    f"{'、還在增強' if comp.get('rising') else ''}"}


def read_zone(k15_closed: list) -> dict:
    """Is price INSIDE a fresh supply / demand zone — the 供需區 engine's own
    definition, same pivots, same freshness rule — and if not, how far away
    the nearest fresh ones sit."""
    import zones as Z
    if not k15_closed or len(k15_closed) < 60:
        return {"read": NEUTRAL, "note": "K 線不足"}
    rec = "實測順勢 +0.175R / 逆勢 −0.032R（3,653 筆重播），帳本 2026-08-20 重算中"
    if Z.at_zone(k15_closed, "long"):
        return {"read": LONG, "in_zone": "demand", "note": f"價格正在新鮮的需求區內 —— 供需區引擎的 LONG 條件。{rec}"}
    if Z.at_zone(k15_closed, "short"):
        return {"read": SHORT, "in_zone": "supply", "note": f"價格正在新鮮的供給區內 —— 供需區引擎的 SELL 條件。{rec}"}
    price = float(k15_closed[-1][4])
    fresh = [z for z in Z.build(k15_closed) if z.get("fresh")]
    above = [z for z in fresh if z["kind"] == "supply" and z["bottom"] > price]
    below = [z for z in fresh if z["kind"] == "demand" and z["top"] < price]
    parts = []
    if above:
        parts.append(f"上方供給區 +{_pct(above[0]['bottom'], price):.1f}%")
    if below:
        parts.append(f"下方需求區 −{_pct(price, below[0]['top']):.1f}%")
    return {"read": NEUTRAL, "in_zone": None,
            "note": ("不在任何新鮮區間內 · " + " · ".join(parts)) if parts else "附近沒有新鮮的供需區"}


# Quality flags: they do NOT vote. A volatile coin is not bearish and a loud
# hour is not bullish; they say whether the conditions the engines were
# measured under hold right now.
def read_volatility(k1h_closed: list) -> dict:
    """The symbol's own 1h ATR as a % of price, against the ceiling
    s1_regime_lab found (config.LOWVOL_ATR_PCT — the same number paper_tracker
    and the S2 outcome cohort use)."""
    pct = atr_pct(k1h_closed) if k1h_closed else None
    ceiling = float(config.LOWVOL_ATR_PCT)
    if pct is None:
        return {"ok": None, "value": "—", "atr_pct": None, "note": "K 線不足以算 ATR"}
    if pct <= ceiling:
        return {"ok": True, "value": f"{pct:.2f}%", "atr_pct": pct,
                "note": f"1h ATR 是價格的 {pct:.2f}%，在 {ceiling:g}% 天花板之下 —— S1 實驗室唯一一致的改善："
                        f"天花板越緊期望值越高（31 檔、多空皆轉正）"}
    return {"ok": False, "value": f"{pct:.2f}%", "atr_pct": pct,
            "note": f"1h ATR 是價格的 {pct:.2f}%，高於 {ceiling:g}% —— S1 的訊號在高波動幣種上歷史上是虧的"}


def read_volume(k15_closed: list) -> dict:
    """The last hour's volume against the previous 24h, per hour, on closed
    15m bars — the same arithmetic as the Pump Radar."""
    vols = [float(c[5]) for c in (k15_closed or [])]
    if len(vols) < 100:
        return {"ok": None, "value": "—", "mult": None, "note": "K 線不足以比較量能"}
    last = sum(vols[-4:])
    prior = vols[-100:-4]
    avg = sum(prior) / len(prior) * 4
    mult = (last / avg) if avg > 0 else 0.0
    if mult >= VOL_SURGE_MULT:
        return {"ok": True, "value": f"{mult:.1f}×", "mult": mult,
                "note": f"最近一小時成交量是過去 24h 平均的 {mult:.1f} 倍 —— 量能是隧道翻多唯一有效的濾網（+0.12R，以 1h ≥3× 量）；"
                        f"但單獨追量能實測 −0.054R，它是確認、不是理由"}
    return {"ok": False, "value": f"{mult:.1f}×", "mult": mult,
            "note": f"最近一小時成交量是過去 24h 平均的 {mult:.1f} 倍 —— 沒有量能確認"}


def premium_gate(score, regime: str, adx) -> dict:
    """The ⭐ premium tier's three gates on this coin right now — the best
    combination the 60-day replay found (conviction ≥85 + BTC-aligned + ADX
    ≥20 → 58.7% first to TP1 at the ⭐ geometry, against ~49% for the raw
    feed). Not a prediction: it says whether the conditions that were
    measured best are present."""
    if score is None:
        return {"ok": False, "direction": None, "conviction": None, "gates": [],
                "note": "儀表沒有讀數"}
    direction = LONG if float(score) >= 50 else SHORT
    conv = float(score) if direction == LONG else 100 - float(score)
    min_conv = int(config.STRATEGY2_PREMIUM_MIN_SCORE)
    min_adx = float(config.STRATEGY2_PREMIUM_MIN_ADX)
    aligned = (direction == LONG and regime == "bull") or (direction == SHORT and regime == "bear")
    gates = [
        {"key": "conv", "label": f"信心 ≥ {min_conv}", "ok": conv >= min_conv, "value": f"{conv:.0f}"},
        {"key": "aligned", "label": "與 BTC 同向", "ok": bool(aligned),
         "value": {"bull": "牛", "bear": "熊"}.get(regime, "盤整")},
        {"key": "adx", "label": f"ADX ≥ {min_adx:g}", "ok": adx is not None and float(adx) >= min_adx,
         "value": f"{float(adx):.1f}" if adx is not None else "—"},
    ]
    ok = all(g["ok"] for g in gates)
    return {"ok": ok, "direction": direction, "conviction": conv, "gates": gates,
            "note": ("三關全過 —— 本專案 60 天回放量到最好的組合：58.7% 先到 TP1（原始訊號約 49%）" if ok else
                     "沒過的關：" + "、".join(g["label"] for g in gates if not g["ok"]) + " —— 不到 ⭐ 精選等級")}


# The S4 scan names its rejections in English for the logs; the page reads
# in 中文. Unknown keys pass through unchanged so a new gate shows as itself.
S4_REJ_ZH = {
    "no long triangle": "沒有做多三角", "no short triangle": "沒有做空三角",
    "EMA200 not rising": "EMA200 沒上升", "EMA200 not falling": "EMA200 沒下降",
    "no support below": "下方沒有支撐", "no resistance above": "上方沒有壓力",
    "stop distance out of range": "停損距離不合格", "not enough history": "K 線不足",
    "no OI data": "前四關通過 · 未平倉未查", "OI not supportive": "未平倉方向相反",
    "no side enabled": "多空都關閉",
}


def s4_reason_zh(reason) -> str:
    import re
    r = str(reason or "")
    if r in S4_REJ_ZH:
        return S4_REJ_ZH[r]
    m = re.match(r"^no recent (bullish|bearish) divergence$", r)
    if m:
        return "沒有多頭背離" if m.group(1) == "bullish" else "沒有空頭背離"
    m = re.match(r"^only (\d+) of (\d+) divergence sources$", r)
    if m:
        return f"背離只有 {m.group(1)}/{m.group(2)} 個"
    m = re.match(r"^stop ([\d.]+)% under the ([\d.]+)% fee floor$", r)
    if m:
        return f"停損 {m.group(1)}% 低於手續費下限"
    return r


def _meter_score(k15: list):
    try:
        return S2.compute_signal(k15).get("score")
    except Exception:  # noqa: BLE001
        return None


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
    # 1h deep enough for EMA200 + ADX warm-up; 4h for S1's EMA50 filter.
    k1h = _get("/fapi/v1/klines", symbol=sym, interval="1h", limit=260)
    k4h = _get("/fapi/v1/klines", symbol=sym, interval="4h", limit=120)
    k15 = _get("/fapi/v1/klines", symbol=sym, interval="15m", limit=K15_CANDLES)
    tick = _get("/fapi/v1/ticker/24hr", symbol=sym)
    prem = _get("/fapi/v1/premiumIndex", symbol=sym)
    frh = _get("/fapi/v1/fundingRate", symbol=sym, limit=30)
    oih = _get("/futures/data/openInterestHist", symbol=sym, period="15m", limit=100)
    try:
        lsr = _get("/futures/data/topLongShortPositionRatio",
                   symbol=sym, period="1h", limit=1)
    except Exception:  # noqa: BLE001 — a missing ratio is not a failed page
        lsr = []
    btc = _get("/fapi/v1/klines", symbol="BTCUSDT", interval="1h", limit=120)

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

    # The measured context. Closed bars only — the forming candle still moves.
    k1h_closed, k4h_closed, k15_closed = k1h[:-1], k4h[:-1], k15[:-1]
    closes_1h = [float(c[4]) for c in k1h_closed]
    closes_4h = [float(c[4]) for c in k4h_closed]
    ema200_1h = calculate_ema(closes_1h, 200) if len(closes_1h) >= 200 else None
    ema50_4h = calculate_ema(closes_4h, 50) if len(closes_4h) >= 50 else None
    regime = read_regime(btc_closes)
    frames = read_frames(price, ema50_4h, ema200_1h)
    adx = read_adx(k1h_closed)
    zone = read_zone(k15_closed)
    vol = read_volatility(k1h_closed)
    volume = read_volume(k15_closed)
    gate = premium_gate(_meter_score(k15), regime["regime"], adx["adx"])

    factors = [
        _factor("regime", "大盤 · BTC 趨勢",
                {"bull": "牛", "bear": "熊", "neutral": "盤整"}.get(regime["regime"], "—"), regime),
        _factor("frames", "時框一致性 · 4H/1H",
                (f"4H {'上' if ema50_4h is not None and price > ema50_4h else '下'} · "
                 f"1H {'上' if ema200_1h is not None and price > ema200_1h else '下'}")
                if ema50_4h is not None and ema200_1h is not None else "—", frames),
        _factor("adx", "趨勢強度 · ADX",
                f"{adx['adx']:.1f}" if adx.get("adx") is not None else "—", adx),
        _factor("oi", "市場結構 · 未平倉", f"{oi_pct:+.2f}% (1h)", oi),
        _factor("taker", "主動買賣 · 誰在追價",
                f"買 {taker.get('share') or 0:.1f}%", taker),
        _factor("structure", "價格結構", struct.get("note", ""), struct),
        _factor("zone", "供需區", {"demand": "需求區內", "supply": "供給區內"}.get(zone.get("in_zone"), "區間外"), zone),
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
        "factor_count": len(factors),
        "long_count": longs, "short_count": shorts,
        "neutral_count": len(factors) - longs - shorts,
        "lean": lean,
        "regime": regime["regime"],
        # Quality flags do NOT vote — they say whether the conditions the
        # engines were measured under hold right now.
        "quality": [
            {"key": "volatility", "label": "波動 · 1h ATR / 價格", **vol},
            {"key": "volume", "label": "量能 · 最近一小時 vs 24h", **volume},
        ],
        "premium_gate": gate,
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
    # S4's five gates on Binance candles: the open-interest gate needs Bybit's
    # OI history, which this page does not fetch, so "front four passed" is
    # the most it can honestly say. Every rejection is named, like the scan.
    try:
        import strategy4 as S4
        res = S4.evaluate_sides(k15[:-1]) or {}
        side = res.get("side")
        if res.get("pass"):
            out.append({"name": "S4 五道關卡", "value": "通過 · " + ("做多" if side == "long" else "做空"),
                        "read": LONG if side == "long" else SHORT,
                        "note": "流動性→三角→EMA200 斜率→結構位→背離 全過；紀錄自 2026-08-19 重新累積，尚無把握"})
        else:
            out.append({"name": "S4 五道關卡", "value": "卡在 · " + s4_reason_zh(res.get("reason")),
                        "read": NEUTRAL,
                        "note": "五道關卡由便宜到昂貴依序檢查，第一道沒過的就是答案（未平倉那關需 Bybit 資料，此處未查）"})
    except Exception:  # noqa: BLE001
        pass
    return out


DISCLAIMER = ("⚠️ 這頁是「現在的條件」，不是預測。每個因子都是實際量到的數字，"
              "但把它們加權成一個分數等於宣稱知道哪個因子比較重要 —— "
              "本專案沒有量過，所以這裡只數「幾項偏多、幾項偏空」。"
              "前三項（大盤趨勢、時框一致、ADX）和品質旗標是本專案量過確實有差的條件；"
              "其餘是慣例讀法。本專案量過 48 組高勝率設定有 46 組在賠錢。")

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



# ── the default view ─────────────────────────────────────────────────────────
# Opening /coin to an empty search box wastes the page: the interesting coins
# right now are the ones the OI radar just flagged, and BTC/ETH are the two
# everything else is read against. Both are already on disk or one bulk call
# away, so the default costs ~1 request rather than 7 per coin.
PINNED = ("BTCUSDT", "ETHUSDT")
DEFAULT_MAX = int(os.getenv("COIN_DEFAULT_MAX", "10"))


def default_symbols(limit: int = DEFAULT_MAX) -> list:
    """BTC and ETH first, then the most recent OI anomalies, newest first.

    Deduped by symbol: crowd_radar records every fresh alert, so a coin that
    keeps building appears several times (EDEN was in there three times) and an
    undeduped list would be one coin wearing three rows.
    """
    out = list(PINNED)
    seen = set(out)
    try:
        import crowd_radar as C
        rows = sorted(C.web_view(limit=60).get("recent") or [],
                      key=lambda r: -(r.get("ts") or 0))
        for r in rows:
            s = r.get("symbol")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    except Exception as exc:  # noqa: BLE001 — an empty radar still leaves BTC/ETH
        print(f"[coin] default list: {exc}")
    return out[:limit]


def _oi_brief(sym: str) -> dict:
    """A cheap OI read for a coin the radar has not flagged (BTC/ETH usually).
    One call; failure just means the row shows price only."""
    try:
        import crowd_radar as C
        oi, px = C.oi_history(sym, period="15m", limit=120)
        if len(oi) < C.SPAN_BARS + 2:
            return {}
        a = C.assess(oi, px)
        return {"oi_pct": a.get("oi_pct"), "pctile": a.get("pctile"),
                "state": a.get("state")}
    except Exception:  # noqa: BLE001
        return {}


def overview(limit: int = DEFAULT_MAX, now: float = None) -> dict:
    """Compact rows for the default view — NOT the full eight-factor analysis.

    Deliberately cheap: one bulk ticker call for every price, the radar's own
    stored numbers for the flagged coins, and one extra call each for the
    pinned two. Running analyse() across ten coins would be ~70 requests and a
    page that takes half a minute to open.
    """
    now = now if now is not None else time.time()
    syms = default_symbols(limit)
    try:
        tick = {t["symbol"]: t for t in _get("/fapi/v1/ticker/24hr")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"抓行情失敗：{str(exc)[:90]}", "rows": []}

    flagged = {}
    try:
        import crowd_radar as C
        for r in sorted(C.web_view(limit=60).get("recent") or [],
                        key=lambda x: x.get("ts") or 0):
            flagged[r["symbol"]] = r          # later rows win = most recent
    except Exception:  # noqa: BLE001
        pass

    rows = []
    for s in syms:
        t = tick.get(s)
        if not t:
            continue
        row = {"symbol": s, "base": s[:-4],
               "price": float(t["lastPrice"]),
               "chg_24h": float(t["priceChangePercent"]),
               "turnover": float(t.get("quoteVolume") or 0),
               "pinned": s in PINNED}
        f = flagged.get(s)
        if f:
            row.update({"oi_pct": f.get("oi_pct"), "pctile": f.get("pctile"),
                        "state": f.get("state"), "tier": f.get("tier"),
                        "notional": f.get("notional"), "flagged_ts": f.get("ts"),
                        "flagged": True})
        elif s in PINNED:
            row.update({**_oi_brief(s), "flagged": False})
        rows.append(row)
    return {"ok": True, "rows": rows, "ts": now,
            "read_zh": __import__("crowd_radar").READ_ZH}
