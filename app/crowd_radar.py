"""
🐋 Crowd radar — when the market piles massively into one side of a symbol.

whale_tracker follows a handful of curated Hyperliquid addresses; liq_alerts
reports positions that have already been destroyed. Neither answers the
question in between: is the CROWD, right now, building an unusually large
long or short in something?

Open interest answers it. OI counts contracts that exist, so a rise means
positions were OPENED and a fall means they were CLOSED — that part is
measured, not inferred. Pair it with price over the same window and you get
the four standard reads (see `oi_read`).

WHAT "MASSIVE" MEANS HERE
Each symbol is judged against ITS OWN recent behaviour, not a number picked by
hand. One call to Binance's public openInterestHist returns 500 × 15m bars —
5.2 days — and the current 2h OI change is ranked against every 2h change in
that window. Top 2% of its own history = massive. BTC's OI moving 6% and a
mid-cap's moving 6% are not the same event, and a fixed threshold cannot tell
them apart; a percentile can.

A RANK IS NOT A P-VALUE
The windows overlap (2h changes sampled every 15m), so the ~490 samples behind
each percentile are heavily autocorrelated and the effective sample size is far
smaller. That makes "bigger than 98% of recent 2h moves" an honest DESCRIPTION
of the last five days and NOT a claim that this had a 2% probability of
happening. It is a rank. This module never converts it into a probability, and
MIN_OI_PCT exists because on a symbol whose OI barely moves, the 98th
percentile of nothing is still nothing.

WHAT THIS IS NOT
It is not an entry signal, and nothing here has been measured against forward
returns on this system. "Crowded longs get squeezed" is a widely repeated
belief; this repo has measured 48 high-win-rate setups and found 46 of them
losing money, so a belief that sounds right is exactly the kind this project
does not act on. The alert reports POSITIONING and says so. If it ever earns
the right to be a signal, that will be because signal_outcomes scored it, not
because the story is good.

The long/short account ratio is a second, independent view of the same
question — OI says how much was opened, the ratio says who is holding — and it
is only fetched for symbols that already passed the OI gate, so the extra call
is never spent on a symbol that was never going to alert.

Data: Binance public futures-data endpoints. No API key, no account.
"""
import json
import os
import time

import requests

import tg_format as F
import telegram_utils

_DIR = os.path.dirname(__file__)
STATE_FILE = os.path.join(_DIR, "crowd_radar_state.json")

BASE = "https://fapi.binance.com"
PERIOD = "15m"
HIST = 500                       # max the endpoint allows: 5.2 days at 15m
SPAN_BARS = int(os.getenv("CROWD_SPAN_BARS", "8"))        # 8 × 15m = 2h

# Top 2% of this symbol's own 2h OI moves. Deliberately not "tuned": there is
# no measured outcome to tune against, so it is set where "unusual" is normally
# drawn and left alone. See the rank-is-not-a-p-value note above.
PCTILE_GATE = float(os.getenv("CROWD_PCTILE", "98"))

# The floor that stops a percentile from firing on noise. On a symbol whose OI
# is flat, the 98th percentile of its 2h changes can be +0.4%, and ranking that
# as "massive" would be true and useless. A guard, not an edge parameter.
MIN_OI_PCT = float(os.getenv("CROWD_MIN_OI_PCT", "5"))

MIN_TURNOVER = float(os.getenv("CROWD_MIN_TURNOVER", "50000000"))   # 24h USDT
TOP_N = int(os.getenv("CROWD_TOP_N", "120"))
COOLDOWN_SEC = float(os.getenv("CROWD_COOLDOWN_SEC", "7200"))
ESCALATE_MULT = 1.5              # inside cooldown, only a 1.5× bigger move re-alerts
RUN_EVERY_SEC = float(os.getenv("CROWD_RUN_EVERY_SEC", "900"))
PACE_SEC = float(os.getenv("CROWD_PACE_SEC", "0.15"))
KEEP_RECENT = 60
TIMEOUT = 15

# Binance lists ~118 stock/ETF perps alongside the crypto ones, and on the
# first live sweep FIVE of six hits were TSLA, GOOGL, SNDK, SKHY and AAOI.
# They are excluded for a reason specific to this module, on top of the one
# behind config.EXCLUDE_TRADFI_PERPS: a stock perp only trades during market
# hours, so its open interest steps at the open and flatlines all weekend.
# Those steps are calendar mechanics, not a crowd piling in — and because the
# percentile is calibrated on five days that include two dead ones, the
# weekend flatline drags the reference distribution down and makes the Monday
# open rank as extreme every single week.
#
# The test for "is this a stock" lives in market_data and is reused rather than
# re-derived: the raw exchangeInfo rows here ARE the `info` payload that
# is_tradfi_market reads, so the two can never drift apart. The first version
# of this guessed from the ticker string and silently matched nothing.
_tradfi_cache: dict = {"ts": 0.0, "symbols": frozenset()}
_TRADFI_TTL = 6 * 3600


def tradfi_symbols(now: float = None) -> frozenset:
    """Raw Binance symbols that are stock/ETF perps, cached for six hours."""
    from market_data import is_tradfi_market
    now = now if now is not None else time.time()
    if now - _tradfi_cache["ts"] < _TRADFI_TTL and _tradfi_cache["symbols"]:
        return _tradfi_cache["symbols"]
    info = _get("/fapi/v1/exchangeInfo", {})
    syms = frozenset(s["symbol"] for s in (info.get("symbols") or [])
                     if is_tradfi_market({"info": s}))
    _tradfi_cache.update({"ts": now, "symbols": syms})
    return syms


# ── the four reads ───────────────────────────────────────────────────────────
# Same semantics as strategy4's OI_* constants, restated rather than imported:
# strategy4 is a heavy module and this needs four strings.
LONGS_OPENING = "longs_opening"
SHORTS_OPENING = "shorts_opening"
SHORTS_CLOSING = "shorts_closing"
LONGS_CLOSING = "longs_closing"

BUILDING = (LONGS_OPENING, SHORTS_OPENING)

READ_ZH = {
    LONGS_OPENING: "新多單進場",
    SHORTS_OPENING: "新空單進場",
    SHORTS_CLOSING: "空單回補",
    LONGS_CLOSING: "多單平倉",
}
HEAD_ZH = {
    LONGS_OPENING: "🐋 大量做多堆積",
    SHORTS_OPENING: "🐻 大量做空堆積",
    SHORTS_CLOSING: "🔥 空單大量回補",
    LONGS_CLOSING: "🩸 多單大量平倉",
}


def oi_read(oi_pct: float, px_pct: float) -> str:
    """Which of the four things happened.

    OI direction is measured: contracts either came into existence or left it.
    WHICH SIDE opened them is an inference — the convention is that price rising
    while OI rises means buyers were the aggressors — and open interest cannot
    actually name the aggressor, because every contract has a long and a short.
    It is the standard read and it is reported as a read, never as a fact.
    """
    if oi_pct >= 0:
        return LONGS_OPENING if px_pct >= 0 else SHORTS_OPENING
    return SHORTS_CLOSING if px_pct >= 0 else LONGS_CLOSING


# ── pure maths ───────────────────────────────────────────────────────────────
def pct_changes(values: list, span: int) -> list:
    """Every `span`-bar percentage change in the series.

    Overlapping on purpose: stepping by `span` instead would leave 61 samples
    from 5 days and make the top 2% a single observation. The cost is
    autocorrelation, which is why the result is only ever used as a rank.
    """
    out = []
    for i in range(span, len(values)):
        prev = values[i - span]
        if prev:
            out.append((values[i] - prev) / abs(prev) * 100.0)
    return out


def percentile_of(dist: list, value: float) -> float:
    """Where `value` ranks in `dist`, 0-100.

    Ranks by MAGNITUDE, not by signed value. A −20% OI collapse and a +20%
    build are both extreme events for this symbol; ranking signed would put
    every unwind at the bottom of the distribution and the radar would only
    ever see one half of what it exists to catch.
    """
    if not dist:
        return 0.0
    mag = abs(value)
    below = sum(1 for d in dist if abs(d) < mag)
    return below / len(dist) * 100.0


def assess(oi_series: list, px_series: list, span: int = SPAN_BARS,
           pctile_gate: float = PCTILE_GATE,
           min_oi_pct: float = MIN_OI_PCT) -> dict:
    """Rank this symbol's latest `span`-bar OI move against its own history."""
    if len(oi_series) < span + 2 or len(px_series) < span + 2:
        return {"massive": False, "reason": "not enough history"}
    oi_now, oi_then = oi_series[-1], oi_series[-1 - span]
    px_now, px_then = px_series[-1], px_series[-1 - span]
    if not oi_then or not px_then:
        return {"massive": False, "reason": "empty baseline"}

    oi_pct = (oi_now - oi_then) / abs(oi_then) * 100.0
    px_pct = (px_now - px_then) / abs(px_then) * 100.0
    # The current move is excluded from its own reference distribution — left
    # in, an extreme reading partly defines the bar it has to clear.
    dist = pct_changes(oi_series[:-1], span)
    pctile = percentile_of(dist, oi_pct)
    state = oi_read(oi_pct, px_pct)

    out = {"oi_pct": round(oi_pct, 2), "px_pct": round(px_pct, 2),
           "pctile": round(pctile, 1), "state": state,
           "samples": len(dist), "massive": False, "reason": None}
    if abs(oi_pct) < min_oi_pct:
        out["reason"] = f"below the {min_oi_pct:g}% floor"
    elif pctile < pctile_gate:
        out["reason"] = f"only p{pctile:.0f} of its own history"
    else:
        out["massive"] = True
    return out


def ratio_extreme(ratios: list) -> dict:
    """Where the current long/short account ratio sits in its own 5-day range.

    A second, independent view: OI says how much was opened, this says who is
    holding it. They can disagree — OI can build while the ratio stays flat if
    both sides added — and when they do, that is information, not an error.
    """
    if len(ratios) < 20:
        return {}
    cur = ratios[-1]
    hist = ratios[:-1]
    below = sum(1 for r in hist if r < cur)
    return {"ratio": round(cur, 3),
            "ratio_pctile": round(below / len(hist) * 100.0, 1),
            "ratio_n": len(hist)}


# ── network ──────────────────────────────────────────────────────────────────
def _get(path: str, params: dict) -> list:
    r = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def universe(top_n: int = TOP_N, min_turnover: float = MIN_TURNOVER) -> list:
    """The most liquid USDT perps, in one call.

    Liquidity is a hard requirement rather than a preference: on a thin book a
    single position IS the open interest, so "the crowd is piling in" would be
    describing one trader.
    """
    rows = _get("/fapi/v1/ticker/24hr", {})
    try:
        skip = tradfi_symbols()
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED on the crypto side rather than silently sweeping stocks:
        # an unfiltered sweep is the failure this guard exists to prevent.
        raise RuntimeError(f"cannot identify TradFi perps: {exc}") from exc
    out = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT") or sym in skip:
            continue
        try:
            turnover = float(r.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if turnover >= min_turnover:
            out.append({"symbol": sym, "turnover": turnover})
    out.sort(key=lambda x: -x["turnover"])
    return out[:top_n]


def oi_history(symbol: str, period: str = PERIOD, limit: int = HIST) -> tuple:
    """(oi_series, implied_price_series).

    sumOpenInterestValue / sumOpenInterest is the notional divided by the coin
    count, which is a price — so ONE call carries both series this needs. The
    implied price tracks the mark closely but is not exactly the close, which
    is fine for a direction over 2h and is only ever used for that.
    """
    rows = _get("/futures/data/openInterestHist",
                {"symbol": symbol, "period": period, "limit": limit})
    oi, px = [], []
    for r in rows:
        try:
            amount = float(r["sumOpenInterest"])
            value = float(r["sumOpenInterestValue"])
        except (KeyError, TypeError, ValueError):
            continue
        if amount > 0:
            oi.append(amount)
            px.append(value / amount)
    return oi, px


def ls_ratio(symbol: str, period: str = PERIOD, limit: int = HIST) -> list:
    rows = _get("/futures/data/topLongShortPositionRatio",
                {"symbol": symbol, "period": period, "limit": limit})
    out = []
    for r in rows:
        try:
            out.append(float(r["longShortRatio"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── state ────────────────────────────────────────────────────────────────────
def _blank() -> dict:
    return {"last": {}, "recent": [], "ran_ts": 0}


def load(path: str = None) -> dict:
    try:
        with open(path or STATE_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return _blank()
    for k, v in _blank().items():
        d.setdefault(k, v)
    return d


def save(store: dict, path: str = None) -> None:
    p = path or STATE_FILE
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(tmp, p)


def should_alert(store: dict, symbol: str, state: str, oi_pct: float,
                 now: float, cooldown: float = COOLDOWN_SEC) -> bool:
    """Once per symbol+direction per cooldown, unless it gets materially worse.

    Keyed on the direction too, so a build that flips to an unwind is a new
    event rather than being swallowed by the build's cooldown — the flip is
    usually the more interesting half.
    """
    prev = (store.get("last") or {}).get(f"{symbol}:{state}")
    if not prev:
        return True
    if now - (prev.get("ts") or 0) >= cooldown:
        return True
    return abs(oi_pct) >= abs(prev.get("oi_pct") or 0) * ESCALATE_MULT


# ── the alert ────────────────────────────────────────────────────────────────
def _usd(v: float) -> str:
    v = float(v or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:.0f}"


def build_alert(row: dict) -> str:
    base = row["symbol"][:-4]
    head = HEAD_ZH.get(row["state"], "🐋 未平倉異動")
    rows = [("未平倉", f"{F.pct(row['oi_pct'])}  ({row['span_h']:g}h)"),
            ("價格", F.pct(row["px_pct"])),
            ("判讀", READ_ZH.get(row["state"], "—"))]
    # The rank is the whole basis for the word "massive", so it is shown with
    # the sample it was drawn from rather than asserted as a bare superlative.
    # Kept short: this cell is the widest in the table and every other row is
    # padded to match it.
    rows.append(("罕見度", f"前 {max(0.1, 100 - row['pctile']):.1f}%"
                           f" · n={row['samples']}"))
    if row.get("ratio") is not None:
        rows.append(("多空比", f"{row['ratio']:.2f}"
                               f" · 前 {max(0.1, 100 - row['ratio_pctile']):.0f}%"))
    rows.append(("成交額", f"{_usd(row['turnover'])} / 24h"))

    tail = ("擁擠的多單是反向插針的燃料。" if row["state"] == LONGS_OPENING else
            "擁擠的空單是軋空的燃料。" if row["state"] == SHORTS_OPENING else
            "部位正在被清掉，不是新資金進場。")
    return "\n".join([
        F.headline(head, base),
        F.pre_table(rows),
        # Not boilerplate. Positioning data reads like a trade idea, and this
        # one has never been scored against forward returns here. The second
        # line says what the rank IS, because "前 0.1%" invites being read as a
        # likelihood and it is a ranking against this symbol's own five days.
        f"\n{tail}\n⚠️ 這是「持倉位置」，不是進場訊號 —— "
        f"本系統從未驗證過它能預測方向。\n"
        f"罕見度 = 跟自己過去 5 天的每 2 小時變化比的排名。",
        F.bybit_line(base),
    ])


# ── the scan ─────────────────────────────────────────────────────────────────
def scan(symbols: list = None, now: float = None, store: dict = None,
         send=None) -> dict:
    """One sweep. Returns what it found; sends nothing if `send` is None."""
    now = now if now is not None else time.time()
    store = load() if store is None else store
    try:
        pool = symbols if symbols is not None else universe()
    except Exception as exc:  # noqa: BLE001 — a dead endpoint must not kill the scanner
        return {"error": f"universe: {exc}", "checked": 0, "hits": []}

    hits, checked, errors = [], 0, 0
    for item in pool:
        sym = item["symbol"] if isinstance(item, dict) else item
        turnover = item.get("turnover", 0) if isinstance(item, dict) else 0
        try:
            oi, px = oi_history(sym)
        except Exception:  # noqa: BLE001 — one symbol must not stop the sweep
            errors += 1
            continue
        finally:
            time.sleep(PACE_SEC)
        checked += 1
        a = assess(oi, px)
        if not a.get("massive"):
            continue

        row = {**a, "symbol": sym, "turnover": turnover,
               "span_h": SPAN_BARS * 15 / 60, "ts": now}
        # Only now is the second call worth spending.
        try:
            row.update(ratio_extreme(ls_ratio(sym)))
            time.sleep(PACE_SEC)
        except Exception:  # noqa: BLE001 — the ratio is a bonus, not a gate
            pass

        if not should_alert(store, sym, a["state"], a["oi_pct"], now):
            row["suppressed"] = "cooldown"
            hits.append(row)
            continue
        store.setdefault("last", {})[f"{sym}:{a['state']}"] = {
            "ts": now, "oi_pct": a["oi_pct"]}
        store.setdefault("recent", []).insert(0, row)
        hits.append(row)
        if send:
            try:
                send(build_alert(row))
            except Exception as exc:  # noqa: BLE001
                print(f"[crowd] alert failed {sym}: {exc}")

    store["recent"] = (store.get("recent") or [])[:KEEP_RECENT]
    store["ran_ts"] = now
    return {"checked": checked, "errors": errors, "hits": hits,
            "alerted": sum(1 for h in hits if not h.get("suppressed"))}


def _send(msg: str) -> None:
    # 💥 清算 topic: this is the positioning thread, alongside liq_alerts and
    # whale_tracker. It carries no account data, so it is safe in a joinable
    # topic — see the group-channel note in telegram_utils.
    telegram_utils.send_message(msg, parse_mode="HTML", channel="liq")


def tick(now: float = None, force: bool = False) -> dict:
    """Called from the S2 sweep. Self-paces to RUN_EVERY_SEC."""
    now = now if now is not None else time.time()
    store = load()
    if not force and now - (store.get("ran_ts") or 0) < RUN_EVERY_SEC:
        return {"skipped": "not due"}
    result = scan(now=now, store=store, send=_send)
    save(store)
    return result


# ── the read ─────────────────────────────────────────────────────────────────
def web_view(store: dict = None, limit: int = 20) -> dict:
    store = load() if store is None else store
    return {
        "recent": (store.get("recent") or [])[:limit],
        "ran_ts": store.get("ran_ts") or 0,
        "span_h": SPAN_BARS * 15 / 60,
        "pctile_gate": PCTILE_GATE,
        "min_oi_pct": MIN_OI_PCT,
        "read_zh": READ_ZH,
        "head_zh": HEAD_ZH,
    }


if __name__ == "__main__":
    import sys
    out = tick(force="--force" in sys.argv)
    print(json.dumps({k: v for k, v in out.items() if k != "hits"}, indent=1))
    for h in out.get("hits", []):
        print(f"  {h['symbol']:14s} OI {h['oi_pct']:+7.2f}%  px {h['px_pct']:+6.2f}%  "
              f"p{h['pctile']:.0f}  {h['state']}"
              f"{'  [' + h['suppressed'] + ']' if h.get('suppressed') else ''}")
