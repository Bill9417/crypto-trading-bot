"""
Strategy 4 — Bybit TradFi perp scanner (股票/商品永續).

Bybit lists 168 stock perps and 4 commodity perps alongside the crypto ones.
They are the only place this account can trade AAPL, NVDA, SKHYNIX or gold with
the same futures mechanics — and until now the only way to know one had set up
was to open TradingView and look. That is the whole problem this solves: the
setup is watched for you, and you are told.

WHAT IT LOOKS FOR (long only, by design — see the note at the bottom)
────────────────────────────────────────────────────────────────────
Five gates, all on closed 15m bars, evaluated cheapest-first so 172 symbols
cost 172 kline calls plus a handful of open-interest calls:

  1. LIQUIDITY   24h turnover floor. Most of this universe listed weeks ago and
                 some of it barely trades; a signal you cannot fill at a sane
                 spread is not a signal.
  2. TRIANGLE    strategy2_meter.compute_signal() == "long" — the All-in-One's
                 green triangle, already mirrored in Python with a parity test
                 against the .pine. It carries its own conditions: confluence
                 score over threshold, close above EMA200, structure bull.
  3. EMA200 UP   the trend EMA must be RISING, not merely below price. Price
                 pops above a falling 200 constantly in a downtrend, and the
                 triangle alone cannot tell those apart.
  4. SUPPORT     a confirmed swing low below price and within reach. This is
                 not decoration: it IS the stop. No support close enough, no
                 trade — because the alternative is an invented stop distance.
  5. MOMENTUM +  a recent BULLISH divergence (MACD / KD / CVD) and an open
     OI          interest read that is not fighting the entry.

WHAT IT DOES, MEASURED (2026-08-06, 51 liquid symbols, 271 symbol-days of 15m)
─────────────────────────────────────────────────────────────────────────────
    signals          50
    win rate         44.0%   (22/50)
    expectancy       +0.052 R  ± 0.165 (1 s.e.)
    95% CI           [−0.272, +0.376] R
    firing rate      ~11 alerts/day across a 60-symbol universe
                     (the OI gate was NOT applied in this replay — deep OI
                     history is not available — so live will fire fewer)

Read that interval, not the headline. It straddles zero with room to spare:
n=50 cannot distinguish +0.05R from −0.27R, and separating an edge that small
from noise needs something like n=1000. This is NOT a positive result. It is
the absence of a result, which is the honest state of a scan whose universe is
weeks old.

The gate breakdown is worth keeping, because it says where the work happens:
of 25,992 bars examined, 25,686 died at the triangle. Of the ~306 triangles
that survived, 112 failed the divergence gate, 93 the EMA200 slope, 51 the stop
distance — the four extra gates cut the triangle's output by ~84%. They are
doing something; whether that something is profitable is exactly what is
unmeasured.

Do NOT tune these thresholds on those 50 trades. Picking the best-looking
variant out of a handful on a sample this size is the definition of the
overfitting that has already cost this repo an S1 walk-forward twice and a
previous S4. The knobs are env vars so they can be changed deliberately, not
so they can be searched.

WHAT THIS IS NOT
────────────────
This has NOT been shown to make money. Half this universe listed inside the
last 30 days; the median symbol has 33 days of history and the oldest has 149.
There is no walk-forward here, no out-of-sample fold, no rotation null — the
data to do any of that does not exist yet.

That matters more than usual in this repo, which has measured, on real candles:
48 mean-reversion configs at 55–78% win rate of which 46 lost money; S1 failing
walk-forward twice; every S2 exit rule negative; and a previous "S4" (low-vol +
oversold) dying on a single −1.87R fold. A setup that looks right on a chart is
the beginning of a question, not the end of one.

So this ships as an ALERT, never an order. It tells you a setup you described
has appeared, and it says so in the message. When these symbols have a year of
history behind them, the honest next step is to run it through walk_forward.py
and find out — the scan records every signal it fires so that test has data to
work with when the time comes.

LONG ONLY: the gates above describe an uptrend pullback. The mirror image for
shorts is not simply the inverse — a stock perp's borrow, its funding and its
overnight gap behave differently on the short side — so rather than ship a
symmetric rule nobody checked, this side is left out until someone measures it.
"""
import json
import os
import time
from datetime import datetime, timezone

TZ_NAME = os.getenv("TZ_DISPLAY", "Asia/Taipei")
STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy4_state.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy4_signals.json")

ENABLED = os.getenv("S4_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
TIMEFRAME = os.getenv("S4_TIMEFRAME", "15m")
CANDLES = int(os.getenv("S4_CANDLES", "450"))       # meter needs ≥365
# Two pools, ranked by 24h turnover and cut separately. One combined ranking
# would be no ranking at all: BTC alone turns over 3.8 BILLION a day against
# ~50M for a busy stock perp, so a single top-N list would be crypto only and
# the TradFi side would silently vanish from the scan.
MAX_TRADFI = int(os.getenv("S4_MAX_TRADFI", "60"))
MAX_CRYPTO = int(os.getenv("S4_MAX_CRYPTO", "100"))
MIN_TURNOVER = float(os.getenv("S4_MIN_TURNOVER_USDT", "2000000"))
MIN_TURNOVER_CRYPTO = float(os.getenv("S4_MIN_TURNOVER_CRYPTO_USDT", "2000000"))
SCAN_TRADFI = os.getenv("S4_SCAN_TRADFI", "true").strip().lower() in ("1", "true", "yes")
SCAN_CRYPTO = os.getenv("S4_SCAN_CRYPTO", "true").strip().lower() in ("1", "true", "yes")
EMA_SLOPE_BARS = int(os.getenv("S4_EMA_SLOPE_BARS", "20"))
DIV_LOOKBACK = int(os.getenv("S4_DIV_LOOKBACK", "20"))    # bars since a divergence
DIV_PIVOT = int(os.getenv("S4_DIV_PIVOT", "5"))
MAX_STOP_PCT = float(os.getenv("S4_MAX_STOP_PCT", "4")) / 100
MIN_STOP_PCT = float(os.getenv("S4_MIN_STOP_PCT", "0.4")) / 100
STOP_BUFFER = float(os.getenv("S4_STOP_BUFFER_PCT", "0.15")) / 100
TP_R = float(os.getenv("S4_TP_R", "2"))
COOLDOWN_SEC = float(os.getenv("S4_COOLDOWN_SEC", str(6 * 3600)))
REQUIRE_DIVERGENCE = os.getenv("S4_REQUIRE_DIVERGENCE", "true").strip().lower() in ("1", "true", "yes")
REQUIRE_OI = os.getenv("S4_REQUIRE_OI", "true").strip().lower() in ("1", "true", "yes")
UNIVERSE_TTL_SEC = 6 * 3600
PACE_SEC = float(os.getenv("S4_PACE_SEC", "0.12"))

# OI read, same four states as Divergence_Radar.pine
OI_LONGS_OPENING = 1
OI_SHORTS_OPENING = 2
OI_SHORTS_CLOSING = 3
OI_LONGS_CLOSING = 4
OI_TEXT = {0: "no data", 1: "開多 longs opening", 2: "開空 shorts opening",
           3: "空單回補 shorts closing", 4: "多單平倉 longs closing"}
# For a LONG: new longs entering, or shorts being squeezed out. The two states
# that mean the opposite side is being built are what disqualifies.
OI_OK_FOR_LONG = (OI_LONGS_OPENING, OI_SHORTS_CLOSING)


# ── state ────────────────────────────────────────────────────────────────────
def _load(path=None) -> dict:
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh
        return {}


def _save(state: dict, path=None) -> None:
    p = path or STATE_FILE
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, p)


# ── pure helpers (unit-tested; no network) ───────────────────────────────────
def ema(values, period):
    """Standard EMA, seeded with the first value."""
    if not values or period < 1:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def ema_rising(closes, period=200, bars=EMA_SLOPE_BARS) -> tuple:
    """(is_rising, slope_pct). Slope is measured over `bars`, in percent of the
    EMA's own level, so it is comparable across a 300 USDT and a 3 USDT perp."""
    if len(closes) < period + bars + 1:
        return False, None
    e = ema(closes, period)
    now, then = e[-1], e[-1 - bars]
    if not then:
        return False, None
    slope = (now - then) / abs(then) * 100
    return slope > 0, slope


def _pivots(highs, lows, left, right):
    """Confirmed pivot indices. A pivot at i is only known at i+right — the same
    lag the chart markers carry, and the reason a live scan can never see the
    most recent `right` bars' pivots."""
    ph, plw = [], []
    for i in range(left, len(highs) - right):
        win_h = highs[i - left:i + right + 1]
        win_l = lows[i - left:i + right + 1]
        if highs[i] == max(win_h) and win_h.count(highs[i]) == 1:
            ph.append(i)
        if lows[i] == min(win_l) and win_l.count(lows[i]) == 1:
            plw.append(i)
    return ph, plw


def support_below(highs, lows, price, left=3, right=3):
    """Most recent CONFIRMED swing low below price. The stop goes here, so an
    invented level is worse than no signal: return None and let the gate fail."""
    _, plw = _pivots(highs, lows, left, right)
    for i in reversed(plw):
        if lows[i] < price:
            return {"level": lows[i], "bars_ago": len(lows) - 1 - i}
    return None


def macd_series(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return [], []
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es, strict=False)]
    return line, ema(line, signal)


def stoch_k(highs, lows, closes, length=9, k_smooth=1):
    """RSV, then the Chinese KDJ's 2/3·1/3 recursion — the same K the
    Divergence Radar draws, so the two cannot disagree about a divergence."""
    if len(closes) < length + 1:
        return []
    rsv = []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - length + 1):i + 1])
        hi = max(highs[max(0, i - length + 1):i + 1])
        rsv.append(50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100)
    k = []
    prev = 50.0
    for v in rsv:
        prev = (2.0 / 3.0) * prev + (1.0 / 3.0) * v
        k.append(prev)
    return k


def cvd_series(ohlcv):
    """Cumulative volume delta ESTIMATED from where each bar closed in its own
    range. The chart measures this from 1-minute intrabars; that data is not
    available here, so this is the same fallback the .pine uses beyond its
    intrabar history limit — an estimate, and labelled as one wherever it is
    reported."""
    out, run = [], 0.0
    for c in ohlcv:
        _, _, h, lo, cl, v = c[0], c[1], c[2], c[3], c[4], c[5]
        run += 0.0 if h == lo else float(v) * (2 * (cl - lo) / (h - lo) - 1)
        out.append(run)
    return out


def bullish_divergence(highs, lows, osc, pivot=DIV_PIVOT, lookback=DIV_LOOKBACK):
    """Regular bullish divergence: price prints a LOWER low while the
    oscillator prints a HIGHER low. Same rule and the same confirmation lag as
    f_divTrack() in Divergence_Radar.pine — if these two ever disagree about a
    bar, one of them is wrong, and this repo has already been bitten once by a
    Python mirror drifting from the .pine it claimed to reproduce.

    Returns bars-ago of the most recent one inside `lookback`, else None.
    """
    n = min(len(lows), len(osc))
    if n < pivot * 2 + 2:
        return None
    _, plw = _pivots(highs[:n], lows[:n], pivot, pivot)
    best = None
    for a, b in zip(plw, plw[1:], strict=False):
        if lows[b] < lows[a] and osc[b] > osc[a]:
            confirmed = b + pivot                  # when the chart would show it
            ago = n - 1 - confirmed
            if 0 <= ago <= lookback:
                best = ago if best is None else min(best, ago)
    return best


def oi_state(oi_values, closes, smooth=3):
    """The four-state OI read. A missing series returns 0 ('no data') rather
    than a fabricated flat reading — asserting open interest did not move is a
    measurement nobody made."""
    if not oi_values or len(oi_values) < smooth + 2 or len(closes) < smooth + 2:
        return 0, None
    deltas = []
    for prev, cur in zip(oi_values, oi_values[1:], strict=False):
        deltas.append(0.0 if not prev else (cur - prev) / prev * 100)
    if not deltas:
        return 0, None
    oi_d = ema(deltas, smooth)[-1]
    px_d = ema([b - a for a, b in zip(closes, closes[1:], strict=False)], smooth)[-1]
    if oi_d > 0:
        return (OI_LONGS_OPENING if px_d >= 0 else OI_SHORTS_OPENING), oi_d
    if oi_d < 0:
        return (OI_SHORTS_CLOSING if px_d >= 0 else OI_LONGS_CLOSING), oi_d
    return 0, oi_d


def plan(entry, support_level):
    """Stop under the support that qualified the setup, target at TP_R × risk.
    Returns None when the resulting stop is absurd in either direction — too
    tight to survive noise, or so wide the 2R target needs a move the symbol
    will not make."""
    if not entry or not support_level or support_level >= entry:
        return None
    sl = support_level * (1 - STOP_BUFFER)
    risk = entry - sl
    stop_pct = risk / entry
    if stop_pct < MIN_STOP_PCT or stop_pct > MAX_STOP_PCT:
        return None
    return {"entry": entry, "sl": sl, "tp": entry + risk * TP_R,
            "stop_pct": stop_pct * 100, "tp_pct": risk * TP_R / entry * 100,
            "rr": TP_R}


def evaluate(ohlcv, oi_values=None) -> dict:
    """All gates on one symbol's candles. Pure — every failure is NAMED, so the
    scan can report what it rejected instead of only what it passed. A filter
    you cannot see the effect of is a filter you cannot tune."""
    out = {"pass": False, "reason": None, "score": None, "slope": None,
           "support": None, "div_ago": None, "oi_state": 0, "oi_delta": None,
           "plan": None}
    if not ohlcv or len(ohlcv) < 380:
        out["reason"] = "not enough history"
        return out

    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]
    price = closes[-1]

    import strategy2_meter
    sig = strategy2_meter.compute_signal(ohlcv)
    out["score"] = sig.get("score")
    if sig.get("signal") != "long":
        out["reason"] = "no long triangle"
        return out

    rising, slope = ema_rising(closes)
    out["slope"] = slope
    if not rising:
        out["reason"] = "EMA200 not rising"
        return out

    sup = support_below(highs, lows, price)
    out["support"] = sup
    if not sup:
        out["reason"] = "no support below"
        return out

    p = plan(price, sup["level"])
    out["plan"] = p
    if not p:
        out["reason"] = "stop distance out of range"
        return out

    if REQUIRE_DIVERGENCE:
        macd_line, _ = macd_series(closes)
        kk = stoch_k(highs, lows, closes)
        cvd = cvd_series(ohlcv)
        agos = [bullish_divergence(highs, lows, s)
                for s in (macd_line, kk, cvd) if s]
        agos = [a for a in agos if a is not None]
        out["div_ago"] = min(agos) if agos else None
        if not agos:
            out["reason"] = "no recent bullish divergence"
            return out

    st, delta = oi_state(oi_values or [], closes)
    out["oi_state"], out["oi_delta"] = st, delta
    if REQUIRE_OI and st not in OI_OK_FOR_LONG:
        out["reason"] = "OI not supportive" if st else "no OI data"
        return out

    out["pass"] = True
    return out


# ── network ──────────────────────────────────────────────────────────────────
def _client():
    import ccxt
    return ccxt.bybit({"enableRateLimit": True,
                       "options": {"defaultType": "linear"}})


def universe(client=None, state=None) -> dict:
    """{symbol: segment} — 'tradfi' for Bybit's stock/commodity perps, 'crypto'
    for everything else it settles in USDT.

    The segment comes from Bybit's own symbolType tag, never the ticker text.
    Guessing from the name would be a coin flip: this venue lists AMD the token
    and AMDSTOCK the equity side by side, and name-based mapping is exactly how
    a mirror once sized an order 344× off."""
    st = state if state is not None else _load()
    cached = st.get("universe")
    # A cache written by the previous version is a LIST of tradfi symbols. It
    # is not merely the wrong type — it is the wrong ANSWER, and serving it for
    # the next six hours would have quietly scanned no crypto at all while
    # reporting success. Shape mismatch invalidates the cache, it does not get
    # coerced.
    if isinstance(cached, dict) and cached \
            and time.time() - float(st.get("universe_ts") or 0) < UNIVERSE_TTL_SEC:
        return cached
    ex = client or _client()
    markets = ex.load_markets()
    uni = {}
    for m in markets.values():
        if not (m.get("linear") and m.get("swap") and m.get("active")):
            continue
        if m.get("quote") != "USDT":
            continue          # the USDC twins would double every base
        stype = (m.get("info") or {}).get("symbolType")
        if stype in ("stock", "commodity"):
            uni[m["symbol"]] = "tradfi"
        elif SCAN_CRYPTO and stype in ("", None, "innovation"):
            uni[m["symbol"]] = "crypto"
    if not SCAN_TRADFI:
        uni = {s: g for s, g in uni.items() if g != "tradfi"}
    if state is not None:
        state["universe"] = uni
        state["universe_ts"] = time.time()
    return uni


CAPS = {"tradfi": (MAX_TRADFI, MIN_TURNOVER), "crypto": (MAX_CRYPTO, MIN_TURNOVER_CRYPTO)}


def select(client, uni) -> list:
    """[(symbol, segment)] — each pool ranked by 24h turnover and cut to its own
    cap. One fetch_tickers call ranks everything; 600 individual ticker calls to
    learn the same thing is the rate-limit incident this repo already had."""
    if isinstance(uni, (list, tuple)):        # tolerate an old cached shape
        uni = {s: "tradfi" for s in uni}
    syms = list(uni)
    try:
        tk = client.fetch_tickers(syms)
    except Exception:  # noqa: BLE001 — no ranking is better than no scan
        tk = {}
    pools = {}
    for s in syms:
        seg = uni[s]
        turnover = float((tk.get(s) or {}).get("quoteVolume") or 0)
        _, floor = CAPS.get(seg, (0, 0))
        if tk and turnover < floor:
            continue
        pools.setdefault(seg, []).append((s, turnover))
    out = []
    for seg, rows in pools.items():
        rows.sort(key=lambda r: -r[1])
        cap = CAPS.get(seg, (MAX_TRADFI, 0))[0]
        out += [(s, seg) for s, _ in rows[:cap]]
    return out


def _oi_history(client, symbol, limit=60):
    try:
        rows = client.fetch_open_interest_history(symbol, TIMEFRAME, limit=limit)
        return [float(r.get("openInterestAmount") or 0) for r in rows]
    except Exception:  # noqa: BLE001 — OI is one gate, not the scan
        return []


def scan(client=None, limit=None) -> dict:
    """Full sweep. Returns {signals, checked, rejected, ts}. Never raises."""
    ex = client or _client()
    state = _load()
    uni = universe(ex, state)
    _save(state)
    picks = select(ex, uni)
    if limit:
        picks = picks[:limit]

    signals, rejected, checked = [], {}, {"tradfi": 0, "crypto": 0}
    for sym, seg in picks:
        try:
            ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=CANDLES)
        except Exception:  # noqa: BLE001 — one bad symbol never stops the scan
            continue
        checked[seg] = checked.get(seg, 0) + 1
        res = evaluate(ohlcv)
        # OI costs a call, so it is only asked for once everything cheaper has
        # already passed. Re-run the last gate with the data now in hand.
        if res["reason"] in ("no OI data", "OI not supportive") or res["pass"]:
            oi = _oi_history(ex, sym)
            res = evaluate(ohlcv, oi)
        if res["pass"]:
            signals.append({"symbol": sym, "base": sym.split("/")[0], "segment": seg,
                            **res, "price": ohlcv[-1][4], "bar_ts": ohlcv[-1][0]})
        else:
            rejected[res["reason"] or "?"] = rejected.get(res["reason"] or "?", 0) + 1
        time.sleep(PACE_SEC)

    # TradFi first, then by score. The stock perps are the half that cannot be
    # validated, so burying them under 100 crypto names would defeat the point
    # of scanning them at all.
    signals.sort(key=lambda s: (s.get("segment") != "tradfi", -(s.get("score") or 0)))
    return {"signals": signals, "checked": sum(checked.values()),
            "checked_by": checked, "rejected": rejected, "ts": time.time()}


# ── presentation ─────────────────────────────────────────────────────────────
def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001
        return timezone.utc


def format_signal(sig: dict) -> str:
    """One setup, in the shared house style so S4 reads like S1 and S3.

    The levels go in this table rather than through tg_format.mono_plan(),
    which needs TWO targets and returns an empty string if either is missing.
    S4 has a single target by construction, so mono_plan silently dropped the
    entire entry/stop/target block and the alert shipped with only percentages
    — a plan you cannot act on. One table, one aligned column, nothing to
    drop."""
    import tg_format as F
    p = sig.get("plan") or {}
    base = sig.get("base") or ""
    tag = "美股永續" if sig.get("segment") == "tradfi" else "加密永續"
    bits = [F.headline(f"📊 S4 {tag}", base, "做多 LONG")]
    rows = []
    if p:
        rows += [("進場", F.fmt_price(p["entry"])),
                 ("停損", f"{F.fmt_price(p['sl'])}  −{p['stop_pct']:.2f}%"),
                 ("目標", f"{F.fmt_price(p['tp'])}  +{p['tp_pct']:.2f}%  {p['rr']:g}R")]
    rows += [("信心", f"{sig['score']:.0f}/100" if sig.get("score") is not None else "—"),
             ("EMA200", f"上升 +{sig['slope']:.2f}%" if sig.get("slope") is not None else "—"),
             ("支撐", F.fmt_price(sig["support"]["level"]) + f"（{sig['support']['bars_ago']} 根前）"
              if sig.get("support") else "—"),
             ("背離", f"{sig['div_ago']} 根前" if sig.get("div_ago") is not None else "—"),
             ("未平倉", OI_TEXT.get(sig.get("oi_state"), "—"))]
    bits.append(F.pre_table(rows))
    bits.append(F.bybit_line(base, sig.get("price")))
    return "\n".join(b for b in bits if b)


DISCLAIMER = ("⚠️ S4 是「掃描通知」，不是已驗證的策略。這些美股永續大多 30 天內才上市"
              "（中位數 33 天），資料根本不夠做前向測試 —— 本專案量過 48 組高勝率設定有 46 組"
              "在賠錢，所以「圖上看起來對」不等於有優勢。自己判斷，這裡不會自動下單。")


def build_digest(result: dict, now=None) -> str:
    now = now or datetime.now(_tz())
    sigs = result.get("signals") or []
    by = result.get("checked_by") or {}
    seg = f"（美股 {by.get('tradfi', 0)} · 加密 {by.get('crypto', 0)}）" if by else ""
    head = f"📊 S4 掃描 · {now.strftime('%m-%d %H:%M')}"
    if not sigs:
        return ""
    body = "\n\n".join(format_signal(s) for s in sigs[:5])
    more = f"\n\n（另有 {len(sigs) - 5} 檔符合，見網頁）" if len(sigs) > 5 else ""
    return (f"{head}\n掃描 {result.get('checked', 0)} 檔{seg}，{len(sigs)} 檔符合"
            f"\n\n{body}{more}\n\n{DISCLAIMER}")


def due_signals(result: dict, state: dict, now_ts: float) -> list:
    """Drop anything alerted for the same symbol inside the cooldown. Without
    this a setup that stays valid for two hours pays out eight identical
    messages and the topic becomes unreadable."""
    sent = state.get("sent") or {}
    out = []
    for s in result.get("signals") or []:
        last = float(sent.get(s["symbol"]) or 0)
        if now_ts - last >= COOLDOWN_SEC:
            out.append(s)
    return out


def web_view() -> dict:
    """Last scan, for the /s4 page. Reads state only — never scans on a page
    load, or every visitor would fire 60 exchange calls."""
    payload = _load(SIGNALS_FILE)
    return {"signals": payload.get("signals") or [],
            "checked": payload.get("checked"),
            "rejected": payload.get("rejected") or {},
            "ts": payload.get("ts"),
            "enabled": ENABLED,
            "universe": len(payload.get("universe") or []),
            "disclaimer": DISCLAIMER,
            "tf": TIMEFRAME, "tp_r": TP_R,
            "min_turnover": MIN_TURNOVER,
            "checked_by": payload.get("checked_by") or {},
            "max_tradfi": MAX_TRADFI, "max_crypto": MAX_CRYPTO}


# ── orchestration ────────────────────────────────────────────────────────────
def _bar_key(now_ts: float) -> int:
    """Which 15m bar we are in. The scan runs once per CLOSED bar: the meter
    and every divergence here are defined on closed bars, so running more often
    would re-evaluate the same candle and change nothing but the API bill."""
    step = 15 * 60
    return int(now_ts // step)


def tick(client=None) -> bool:
    """Called once per S2 sweep. Self-paced: a no-op until a new bar closes.
    Returns True when a scan actually ran."""
    if not ENABLED:
        return False
    now_ts = time.time()
    state = _load()
    bar = _bar_key(now_ts)
    if state.get("last_bar") == bar:
        return False
    state["last_bar"] = bar
    _save(state)

    result = scan(client)
    payload = {**result, "universe": state.get("universe") or []}
    _save(payload, SIGNALS_FILE)

    fresh = due_signals(result, state, now_ts)
    if fresh:
        import telegram_utils
        text = build_digest({**result, "signals": fresh})
        if text:
            # channel="s1signals": the 📈 signal topic the owner asked for.
            # It is a PUBLIC group topic, so nothing here may carry balances,
            # position sizes or free margin — only the setup itself.
            telegram_utils.send_message(text, force=True, channel="s1signals")
        sent = state.get("sent") or {}
        for s in fresh:
            sent[s["symbol"]] = now_ts
        state["sent"] = sent
    state["last_run"] = now_ts
    state["last_count"] = len(result.get("signals") or [])
    _save(state)
    print(f"[s4] scanned {result.get('checked')} · {len(result.get('signals') or [])} setups"
          f" · {len(fresh)} alerted")
    return True


def report_tg() -> str:
    """/s4 command — the last scan, on demand."""
    v = web_view()
    sigs = v["signals"]
    if not v["enabled"]:
        return "S4 沒有啟用（S4_ENABLED=false）。"
    if not v["ts"]:
        return "S4 還沒跑過第一次掃描。"
    age = int((time.time() - float(v["ts"])) / 60)
    if not sigs:
        rej = v.get("rejected") or {}
        top = "、".join(f"{k} {n}" for k, n in sorted(rej.items(), key=lambda r: -r[1])[:3])
        return (f"📊 S4 · {age} 分鐘前掃描 {v['checked']} 檔\n目前沒有符合的設定。\n"
                + (f"主要卡在：{top}" if top else ""))
    body = "\n\n".join(format_signal(s) for s in sigs[:5])
    return f"📊 S4 · {age} 分鐘前 · {len(sigs)} 檔符合\n\n{body}\n\n{DISCLAIMER}"
