"""
⚡ 隧道上方爆量 — price is holding above the middle tunnel and an hour's worth
of buying just arrived.

Asked for 2026-08-21: "if it is above middle ema tunnel and within an hour
there are huge amount volume buy in then it will have an alert on the
observation list. Make sure the alert will be on time."

DIFFERENT FROM 隧道翻多 (vegas_reclaim), and the difference is the whole point:

    vegas_reclaim   an EVENT — the close CROSSES back above the tunnel while
                    EMA200 turns up. It fires once, on the bar of the cross,
                    and cannot fire again while price stays above.
    this module     a STATE plus an event — price is ALREADY above the tunnel
                    and stays there, and buying arrives underneath it. Nothing
                    has to cross. A coin that reclaimed the tunnel yesterday
                    and gets bought today is invisible to the other detector
                    and is exactly what this one is for.

"MIDDLE TUNNEL" is read as the INNER Vegas pair, EMA144/169 — the band in the
middle of the EMA set this repo already draws (strategy2_meter's Double Tunnel
is inner 144/169, outer 576/676), and the green ribbon on the chart the
request came with. The outer pair is available behind TUNNEL_PAIR if that
reading turns out to be wrong.

── ON TIME, AND WHAT THAT COSTS ────────────────────────────────────────────
The timeliness requirement is the hard part, and the obvious implementation
fails it silently.

strategy2_scanner caches candles per 15m BUCKET and only patches the forming
bar's PRICE across sweeps — high, low, close. Volume is not patched; a
synthetic forming bar is appended with volume 0.0. So a detector reading the
sweep's candles cannot see volume that arrived inside the current bucket at
all, and a surge starting at :01 is invisible until :15. Add the sweep's own
period and the alert lands up to ~20 minutes after the buying. It would still
"work" — it would just always be late, and nothing in the output would say so.

So the read is split, the same way zones.py splits it:

  TUNNEL   free, on the 15m candles the sweep already holds. The tunnel is an
           EMA144 — it moves slowly, and a bucket-old read of it is the same
           read.
  SURGE    ONE 5m fetch, and only for symbols that already passed the tunnel
           gate and rank highest on the (bucket-old) 15m volume. Fresh 5m
           bars put the worst-case delay at one 5m close plus the sweep's own
           position — measured and reported in `age_s` on every signal rather
           than asserted here.

`age_s` ships on every alert for the same reason a cache reports its age: a
detector that cannot say how late it is turns a stale observation into a
current one.

── WHAT IT IS NOT ──────────────────────────────────────────────────────────
An observe-list flag. No entry, no stop, no target. Every volume-and-trend
shape this repo has measured has come back somewhere between negative and
indistinguishable from zero after costs, including the closely-related
隧道翻多 (-0.094R per firing hour, interval excluding zero), and there is no
reason to expect this one to be different until it has its own forward record.
MEASURED below carries whatever has actually been established.
"""
import os
import time

# ── the tunnel ───────────────────────────────────────────────────────────────
# Same periods as strategy2_meter's INNER tunnel and vegas_reclaim, so "the
# tunnel" means one thing across this codebase rather than three.
TUNNEL_FAST = int(os.getenv("THRUST_TUNNEL_FAST", "144"))
TUNNEL_SLOW = int(os.getenv("THRUST_TUNNEL_SLOW", "169"))

TIMEFRAME = os.getenv("THRUST_TIMEFRAME", "15m")      # tunnel read (free)
CONFIRM_TF = os.getenv("THRUST_CONFIRM_TF", "5m")     # surge read (1 call)

# "within an hour", in minutes. Not env-tunable below the confirm timeframe:
# a window shorter than one bar cannot be measured on that bar.
WINDOW_MIN = int(os.getenv("THRUST_WINDOW_MIN", "60"))

# How much buying counts as "huge". Measured against the symbol's OWN typical
# hour over the baseline window, not against other symbols — a 3x hour on a
# thin alt and on BTC are the same statement about that market.
VOL_MULT = float(os.getenv("THRUST_VOL_MULT", "3.0"))
BASELINE_HOURS = int(os.getenv("THRUST_BASELINE_HOURS", "24"))
# Louder than the gate, for display only.
VOL_LOUD = float(os.getenv("THRUST_VOL_LOUD", "5.0"))

# The buying has to actually be buying. Without this a capitulation hour —
# enormous volume, price collapsing — reads identically to accumulation.
MIN_BUY_SHARE = float(os.getenv("THRUST_MIN_BUY_SHARE", "0.55"))

# Price must be above the tunnel by more than a tick of noise, or a symbol
# oscillating across the line fires every time it wobbles up.
MIN_ABOVE_PCT = float(os.getenv("THRUST_MIN_ABOVE_PCT", "0.1"))

# Volatility floor, same reasoning as vegas_reclaim: below this the "surge" is
# an ATR artefact on a stablecoin pair rather than a market event.
MIN_ATR_PCT = float(os.getenv("THRUST_MIN_ATR_PCT", "0.30"))

COOLDOWN_SEC = float(os.getenv("THRUST_COOLDOWN_SEC", str(3 * 3600)))

WARMUP = TUNNEL_SLOW + 60          # bars of TIMEFRAME needed for the tunnel


def _tf_sec(tf: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    return int(tf[:-1]) * units[tf[-1]]


def bars_in_window(tf: str = None) -> int:
    """How many CONFIRM_TF bars make up WINDOW_MIN."""
    return max(1, (WINDOW_MIN * 60) // _tf_sec(tf or CONFIRM_TF))


def ema_series(vals: list, period: int) -> list:
    k = 2.0 / (period + 1)
    out, e = [], None
    for v in vals:
        e = float(v) if e is None else float(v) * k + e * (1 - k)
        out.append(e)
    return out


def atr_pct(ohlcv: list, period: int = 14) -> float:
    if len(ohlcv) < period + 1:
        return 0.0
    trs = []
    for i in range(len(ohlcv) - period, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    close = ohlcv[-1][4]
    return (sum(trs) / len(trs)) / close * 100 if close else 0.0


# ── gate 1: the tunnel (free, on the sweep's 15m candles) ────────────────────
def tunnel_read(ohlcv: list) -> dict:
    """Where the last CLOSED bar sits relative to the middle tunnel.

    {} when there is not enough history — NOT a dict saying "below". A missing
    read must not assert a position the data cannot support.
    """
    closed = ohlcv[:-1] if ohlcv else []
    if len(closed) < WARMUP:
        return {}
    closes = [c[4] for c in closed]
    fast = ema_series(closes, TUNNEL_FAST)
    slow = ema_series(closes, TUNNEL_SLOW)
    top, bottom = max(fast[-1], slow[-1]), min(fast[-1], slow[-1])
    price = closes[-1]
    above_pct = (price - top) / top * 100 if top else 0.0
    return {
        "price": price, "tunnel_top": top, "tunnel_bottom": bottom,
        "above_pct": round(above_pct, 3),
        "above": bool(price > top and above_pct >= MIN_ABOVE_PCT),
        "atr_pct": round(atr_pct(closed), 3),
        "bar_ts": int(closed[-1][0]),
    }


def recent_volume_rank(ohlcv: list, hours: int = 1) -> float:
    """A cheap, bucket-old ordering key for choosing which candidates deserve
    the 5m call. NOT a gate — it decides who gets LOOKED AT, never who fires,
    because within the current bucket it cannot see the newest volume at all.
    """
    closed = ohlcv[:-1] if ohlcv else []
    per_hour = max(1, 3600 // _tf_sec(TIMEFRAME))
    need = per_hour * (BASELINE_HOURS + hours)
    if len(closed) < need:
        return 0.0
    vols = [c[5] or 0.0 for c in closed]
    recent = sum(vols[-per_hour * hours:])
    base = vols[-need:-per_hour * hours]
    avg = (sum(base) / len(base)) * per_hour * hours if base else 0.0
    return (recent / avg) if avg > 0 else 0.0


# ── gate 2: the surge (one 5m call, fresh) ───────────────────────────────────
def surge_read(ohlcv5: list, now: float = None) -> dict:
    """Buying in the last WINDOW_MIN minutes vs this symbol's own typical hour.

    Closed bars only. The forming bar is dropped: its volume is a partial
    hour's worth compared against a full hour's average, which UNDER-reports
    early in the bar and would make the detector systematically late in
    exactly the situation it exists to catch — while looking like it worked.
    """
    now = now if now is not None else time.time()
    closed = ohlcv5[:-1] if ohlcv5 else []
    win = bars_in_window()
    per_hour = max(1, 3600 // _tf_sec(CONFIRM_TF))
    need = win + per_hour * BASELINE_HOURS
    if len(closed) < need:
        return {}
    rows = closed[-win:]
    base = closed[-need:-win]
    base_hour = (sum(r[5] or 0.0 for r in base) / len(base)) * win if base else 0.0

    total = sum(r[5] or 0.0 for r in rows)
    buy = sum((r[5] or 0.0) for r in rows if r[4] >= r[1])
    sell = total - buy
    last_close_ms = int(rows[-1][0]) + _tf_sec(CONFIRM_TF) * 1000
    return {
        # None, not 0.0, when there is no baseline to compare against — the
        # difference between "this hour was normal" and "nobody measured".
        "vol_mult": round(total / base_hour, 2) if base_hour > 0 else None,
        "buy_mult": round(buy / base_hour, 2) if base_hour > 0 else None,
        "buy_share": round(buy / total, 3) if total > 0 else None,
        "window_vol": total, "buy_vol": buy, "sell_vol": sell,
        "baseline_vol": base_hour,
        "bars": win, "confirm_tf": CONFIRM_TF,
        # How stale the newest CLOSED bar is. Shipped rather than assumed:
        # this is the number the "on time" requirement is actually about.
        "age_s": max(0, int(now - last_close_ms / 1000.0)),
        "last_bar_ts": int(rows[-1][0]),
    }


def is_surge(s: dict) -> bool:
    if not s:
        return False
    if s.get("vol_mult") is None or s.get("buy_share") is None:
        return False
    return s["vol_mult"] >= VOL_MULT and s["buy_share"] >= MIN_BUY_SHARE


def why_not(t: dict, s: dict = None) -> str:
    """Which gate refused — so "nothing fired" can be told apart from "the
    scan is broken" without reading a log."""
    if not t:
        return "K 棒不足"
    if not t["above"]:
        if t["price"] <= t["tunnel_bottom"]:
            return "在隧道下方"
        if t["price"] <= t["tunnel_top"]:
            return "還在隧道裡"
        return f"剛站上隧道 {t['above_pct']:.2f}%（不到 {MIN_ABOVE_PCT}%）"
    if t["atr_pct"] < MIN_ATR_PCT:
        return f"波動太小 {t['atr_pct']:.2f}%"
    if s is None:
        return "未確認量能"
    if not s:
        return "5m K 棒不足"
    if s.get("vol_mult") is None:
        return "沒有量能基準"
    if s["vol_mult"] < VOL_MULT:
        return f"這小時量能只有 {s['vol_mult']:.1f}x"
    if (s.get("buy_share") or 0) < MIN_BUY_SHARE:
        return f"量大但買方只占 {100 * (s.get('buy_share') or 0):.0f}%"
    return ""


def consider(sym: str, ohlcv15: list, state: dict, now: float,
             fetch_tf=None) -> dict:
    """One symbol. {} unless it fired.

    The 5m confirmation is asked ONLY after the free tunnel gate has passed,
    the same cheapest-gate-first order zones.py and strategy4 use — it fires a
    handful of calls a sweep instead of one per symbol.
    """
    t = tunnel_read(ohlcv15)
    if not t or not t["above"] or t["atr_pct"] < MIN_ATR_PCT:
        return {}
    last = (state.get("last") or {}).get(sym)
    if last is not None and now - last < COOLDOWN_SEC:
        return {}
    if fetch_tf is None:
        return {}
    per_hour = max(1, 3600 // _tf_sec(CONFIRM_TF))
    want = bars_in_window() + per_hour * BASELINE_HOURS + 5
    try:
        rows5 = fetch_tf(sym, CONFIRM_TF, want)
    except Exception:  # noqa: BLE001 — one dead symbol is not a failed scan
        return {}
    s = surge_read(rows5, now)
    if not is_surge(s):
        return {}
    state.setdefault("last", {})[sym] = now
    return {
        "symbol": sym, "base": sym.split("/")[0], "side": "long",
        "timeframe": TIMEFRAME, "fired_ts": now,
        "ts": t["bar_ts"],
        "loud": bool((s.get("vol_mult") or 0) >= VOL_LOUD),
        **t, **s,
    }


MEASURED = {
    # 139 perps x 3000 15m bars (31 days, 2026-07 -> 2026-08), next-bar entry,
    # 1.5xATR stop / 2R target, 48h cap, net of fees + 10bp slippage, WITH the
    # 3h cooldown applied so one surge counts once.
    "n": 1551, "hours": 549, "per_day": 53.8,
    "mean_r": -0.125, "lo": -0.198, "hi": -0.053,
    "per_hour_r": -0.202, "per_hour_lo": -0.293, "per_hour_hi": -0.112,
    "win_pct": 37.9,
    "span": "139 檔永續 × 3000 根 15m K（31 天）",
    # Both intervals sit ENTIRELY below zero. This is not "unproven", it is
    # measured and negative — a stronger statement than 隧道翻多's, which at
    # least straddles zero per signal.
    "verdict": "實測是負的（不是「還沒證明」）—— 只進觀察清單，不給進出場價",
    # mean vs median at the same horizon. Positive mean, negative median: the
    # average is carried by a minority of large winners while the typical
    # signal drifts down.
    "fwd": [(1, 0.223, -0.046), (6, 0.961, -0.132),
            (24, 3.193, -0.136), (48, 4.000, 0.040)],
    # The counter-intuitive one, and the reason it is recorded rather than
    # quietly dropped: requiring the hour's volume to be MOSTLY BUYING makes
    # the result monotonically WORSE. The gate the request asked for is the
    # part that costs the most.
    "buy_share_ladder": [(0.00, -0.051, 9627), (0.55, -0.075, 6202),
                         (0.70, -0.101, 4594)],
    # Not monotone — 6x is the best cell and 10x is worse again, which is what
    # noise looks like. VOL_MULT is NOT set from this.
    "dose": [(2.0, -0.107, 12340), (3.0, -0.075, 6202),
             (4.0, -0.056, 3672), (6.0, 0.034, 1680), (10.0, -0.010, 681)],
    # Signals cluster hard: the biggest single hour fired 59 coins after the
    # cooldown (279 before it). When the whole market is bid, every chart is
    # above its tunnel with volume underneath. One such hour is one event.
    "biggest_hour": 59,
    "note": ("量能倍數的門檻沒有依實測挑選 —— 3x 是「爆量」的先驗定義，"
             "而且 6x 最好、10x 又變差，那個形狀就是雜訊"),
}


# ── the record ───────────────────────────────────────────────────────────────
import json  # noqa: E402 — kept beside the store it serves

STATE_FILE = os.path.join(os.path.dirname(__file__), "thrust_state.json")
RETAIN_HOURS = float(os.getenv("THRUST_RETAIN_HOURS", "12"))
MAX_KEEP = 80

# How many of a scan's hits reach the OBSERVE LIST, strongest first.
# Not a detection threshold — every hit is still recorded and counted. This
# repo's watchlist ranks by how many INDEPENDENT engines flagged a coin, and a
# source that fires on 54 coins a day (biggest hour: 59 at once) would appear
# beside almost everything and stop distinguishing anything. The cap is
# reported next to the list so "the strongest 8" is never read as "all 8".
TOP_N = int(os.getenv("THRUST_TOP_N", "8"))


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = start clean
        return {}


def save_state(state: dict) -> None:
    """Write via a temp file and rename.

    open(path, "w") TRUNCATES before it writes, so a crash mid-dump leaves a
    half-file that no longer parses — which is exactly what happened to a
    candle cache during this feature's own data collection when two writers
    raced. The rename is atomic, so a reader sees either the old state or the
    new one and never a partial one.
    """
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — a scanner must not break the loop
        print(f"[thrust] save failed: {exc}")
        # The partial temp file is the failed write. Leaving it behind litters
        # the app directory with one file per crash, forever.
        try:
            os.remove(tmp)
        except OSError:
            pass


def note(sig: dict, state: dict = None, now: float = None) -> dict:
    """Record one hit. Returns the state so the caller can batch a save."""
    state = load_state() if state is None else state
    now = now if now is not None else time.time()
    rows = [r for r in (state.get("recent") or [])
            if now - (r.get("fired_ts") or 0) <= RETAIN_HOURS * 3600]
    rows.insert(0, {k: sig.get(k) for k in (
        "symbol", "base", "side", "ts", "fired_ts", "vol_mult", "buy_mult",
        "buy_share", "above_pct", "atr_pct", "age_s", "loud", "price",
        "tunnel_top", "tunnel_bottom", "confirm_tf", "bars")})
    state["recent"] = rows[:MAX_KEEP]
    state["hits_today"] = int(state.get("hits_today") or 0) + 1
    return state


def top(state: dict = None, n: int = None, now: float = None) -> list:
    """The strongest live hits, one per symbol, newest kept."""
    state = load_state() if state is None else state
    now = now if now is not None else time.time()
    n = TOP_N if n is None else n
    live = [r for r in (state.get("recent") or [])
            if now - (r.get("fired_ts") or 0) <= RETAIN_HOURS * 3600]
    seen, uniq = set(), []
    for r in sorted(live, key=lambda x: -(x.get("fired_ts") or 0)):
        sym = r.get("symbol")
        if sym and sym not in seen:
            seen.add(sym)
            uniq.append(r)
    # Strongest first. vol_mult is None when there was no baseline, and a
    # missing reading must not sort as though it were the weakest measured one.
    return sorted(uniq, key=lambda r: -(r.get("vol_mult") or 0))[:n]


def _record_view() -> dict:
    """The forward record's own view, or {} if it is unavailable. Never fatal:
    a card must still render its live signals when the book cannot be read."""
    try:
        import thrust_outcomes
        return thrust_outcomes.web_view()
    except Exception as exc:  # noqa: BLE001
        print(f"[thrust] record view failed: {exc}")
        return {}


def web_view(state: dict = None, now: float = None) -> dict:
    state = load_state() if state is None else state
    now = now if now is not None else time.time()
    live = [r for r in (state.get("recent") or [])
            if now - (r.get("fired_ts") or 0) <= RETAIN_HOURS * 3600]
    shown = top(state, now=now)
    ages = [r.get("age_s") for r in live
            if isinstance(r.get("age_s"), (int, float))]
    return {
        "top": shown,
        # Both numbers, always: "the strongest 8" must never read as "all 8".
        "live_n": len(live),
        "hidden": max(0, len(live) - len(shown)),
        "top_n": TOP_N,
        "retain_hours": RETAIN_HOURS,
        "scanned": state.get("scanned"),
        "confirmed": state.get("confirmed"),
        "confirm_cap": state.get("confirm_cap"),
        # How many cleared the free gate and never got a 5m call. A capped
        # scan that says "1 fired" without saying it only looked at 40 of 400
        # is indistinguishable from a complete one.
        "skipped_cap": state.get("skipped_cap"),
        "funnel": state.get("funnel") or {},
        "ran_ts": state.get("ran_ts") or 0,
        # The "on time" requirement, measured rather than asserted. None when
        # nothing has fired — an empty list has no latency, and 0 would claim
        # a promptness nobody observed.
        "age_median_s": (sorted(ages)[len(ages) // 2] if ages else None),
        "age_max_s": (max(ages) if ages else None),
        "measured": MEASURED,
        # The forward book's terms. vol_thrust records sightings; the R that
        # the record accumulates comes from thrust_outcomes, so its
        # assumptions belong on this card and not only on that module.
        "live": _record_view(),
        "params": {
            "timeframe": TIMEFRAME, "confirm_tf": CONFIRM_TF,
            "window_min": WINDOW_MIN, "vol_mult": VOL_MULT,
            "vol_loud": VOL_LOUD, "min_buy_share": MIN_BUY_SHARE,
            "min_above_pct": MIN_ABOVE_PCT, "baseline_hours": BASELINE_HOURS,
            "fast": TUNNEL_FAST, "slow": TUNNEL_SLOW,
            "cooldown_h": COOLDOWN_SEC / 3600.0,
        },
    }
