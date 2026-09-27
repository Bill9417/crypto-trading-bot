"""
📓 What happened AFTER the crowd piled in — the OI radar's forward book.

crowd_radar ends its own docstring with the admission that matters: nothing
it reports "has been measured against forward returns on this system", and
"crowded longs get squeezed" is a belief this repo has every reason to
distrust until it is counted. This module does the counting.

WHAT IS RECORDED. Every pile-up the radar puts on the board (one row per
symbol + direction + timestamp) is scored against the exchange's own 15m
candles at three horizons — 1h, 4h and 24h after the alert — plus the largest
move either way inside the 24h. The reference price is the OPEN of the first
15m bar after the alert, never the implied price the radar ranks with: that
one is a notional divided by a coin count and can sit 0.3% off the tape,
which is nothing to a 2h sign and everything to a 1h return.

WHAT IS REPORTED. Counts and medians, per read, per horizon. For a build
(new longs, new shorts) the question is whether price then went AGAINST the
crowd — the squeeze the alert text warns about — and the answer is a share
with a Wilson interval around it. Below MIN_N the page says "not enough" and
nothing else, because a 60% on eleven events is a coin toss wearing a
percentage.

WHY IT SHARES crowd_radar_state.json. Every state file here has exactly one
writer, and the radar already owns this one — so the book lives under a
"book" key and the radar's tick calls settle(). The dashboard's push feed
watches that file and therefore refreshes the record for free.
"""
import math
import os
import statistics
import time

HORIZONS_H = (1, 4, 24)
BAR_MIN = 15
KEEP_CLOSED = int(os.getenv("CROWD_BOOK_KEEP", "800"))
MAX_FETCH_PER_TICK = int(os.getenv("CROWD_BOOK_FETCH_PER_TICK", "24"))
# An alert with no candle data after three days is a delisted or renamed
# symbol, not a pending measurement. Dropped and counted, never guessed.
GIVE_UP_H = 72
MIN_N = 30

# The price direction that DEFINES each read (see crowd_radar.oi_read): new
# longs come with price up, new shorts with price down, short covering with
# price up, long liquidation with price down. "Continued" means price kept
# going that way; "reversed" means it turned. For the two builds, reversed is
# the crowd getting hurt — the squeeze.
FOLLOW_UP = {
    "longs_opening": True,
    "shorts_opening": False,
    "shorts_closing": True,
    "longs_closing": False,
}
BUILDS = ("longs_opening", "shorts_opening")


def _blank() -> dict:
    return {"open": {}, "closed": [], "n_lifetime": 0, "dropped": 0}


def book_of(store: dict) -> dict:
    b = store.setdefault("book", {})
    for k, v in _blank().items():
        b.setdefault(k, v)
    return b


def key_of(row: dict) -> str:
    return f"{row.get('symbol')}:{row.get('state')}:{int(row.get('ts') or 0)}"


def note(store: dict, row: dict) -> bool:
    """Open a row for one pile-up the radar just put on the board."""
    if not row.get("symbol") or row.get("state") not in FOLLOW_UP or not row.get("ts"):
        return False
    book = book_of(store)
    k = key_of(row)
    if k in book["open"]:
        return False
    book["open"][k] = {
        "key": k, "symbol": row["symbol"], "state": row["state"],
        "ts": float(row["ts"]), "oi_pct": row.get("oi_pct"),
        "px_pct": row.get("px_pct"), "pctile": row.get("pctile"),
        "tier": row.get("tier"), "notional": row.get("notional"),
        "ret": {},
    }
    return True


def due_horizons(entry: dict, now: float) -> list:
    """Horizons whose closing bar has already printed and are not yet scored."""
    ts = float(entry.get("ts") or 0)
    done = entry.get("ret") or {}
    return [h for h in HORIZONS_H
            if str(h) not in done and now - ts >= h * 3600 + BAR_MIN * 60]


def score(entry: dict, klines: list, now: float) -> bool:
    """Fill every due horizon from Binance-shaped klines
    [open_ms, o, h, l, c, v, close_ms, ...]. True if anything changed."""
    ts_ms = float(entry.get("ts") or 0) * 1000
    bars = []
    for k in klines or []:
        try:
            bars.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                         float(k[4]), int(k[6])))
        except (TypeError, ValueError, IndexError):
            continue
    # Reference = the open of the first bar that OPENS after the alert. Up to
    # 15 minutes late, which is the honest direction: a price you could have
    # had, not one that printed before the alert existed.
    after = [b for b in bars if b[0] >= ts_ms]
    if not after:
        return False
    px0 = after[0][1]
    if px0 <= 0:
        return False
    changed = False
    entry["px0"] = px0
    ret = entry.setdefault("ret", {})
    for h in due_horizons(entry, now):
        target = ts_ms + h * 3600 * 1000
        bar = next((b for b in after if b[5] >= target), None)
        if bar is None:
            continue
        ret[str(h)] = round((bar[4] / px0 - 1) * 100, 3)
        changed = True
        if h == max(HORIZONS_H):
            # Up to and including the bar the horizon was read on, so the
            # excursion can never be smaller than the return it brackets.
            path = [b for b in after if b[5] <= bar[5]] or after[:1]
            entry["max_up"] = round((max(b[2] for b in path) / px0 - 1) * 100, 3)
            entry["max_dn"] = round((min(b[3] for b in path) / px0 - 1) * 100, 3)
    return changed


def settle(store: dict, fetch, now: float = None,
           max_fetch: int = None, pace: float = 0.0) -> dict:
    """Score what is due, least-recently-checked first (the same rotation the
    other books use, so a per-tick cap cannot starve the rows behind it).

    `fetch(symbol, ts_seconds)` returns klines from the alert onward; a dead
    symbol costs one error, never the tick.
    """
    now = time.time() if now is None else now
    max_fetch = MAX_FETCH_PER_TICK if max_fetch is None else max_fetch
    book = book_of(store)
    out = {"scored": 0, "closed": 0, "errors": 0, "dropped": 0}
    pending = [e for e in book["open"].values() if due_horizons(e, now)]
    pending.sort(key=lambda e: (e.get("checked_ts") or 0, e.get("ts") or 0))
    for e in pending[:max_fetch]:
        e["checked_ts"] = now
        try:
            kl = fetch(e["symbol"], e["ts"])
        except Exception:  # noqa: BLE001 — one symbol must not stall the book
            out["errors"] += 1
            continue
        finally:
            if pace:
                time.sleep(pace)
        if score(e, kl, now):
            out["scored"] += 1
        if str(max(HORIZONS_H)) in e.get("ret", {}):
            book["open"].pop(e["key"], None)
            book["closed"].append(e)
            book["n_lifetime"] = int(book.get("n_lifetime") or 0) + 1
            out["closed"] += 1
    for k, e in list(book["open"].items()):
        if now - float(e.get("ts") or 0) > GIVE_UP_H * 3600 and not e.get("ret"):
            book["open"].pop(k, None)
            book["dropped"] = int(book.get("dropped") or 0) + 1
            out["dropped"] += 1
    if len(book["closed"]) > KEEP_CLOSED:
        book["closed"] = book["closed"][-KEEP_CLOSED:]
    out["open"] = len(book["open"])
    return out


# ── the read ─────────────────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """95% interval for a share, in percent. Does not collapse to ±0 at k=0
    or k=n the way the normal approximation does."""
    if n <= 0:
        return (0.0, 100.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half) * 100, 1),
            round(min(1.0, centre + half) * 100, 1))


def _cell(state: str, rets: list) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0}
    up = sum(1 for r in rets if r > 0)
    cont = up if FOLLOW_UP[state] else sum(1 for r in rets if r < 0)
    lo, hi = wilson(cont, n)
    return {"n": n, "median": round(statistics.median(rets), 2),
            "mean": round(sum(rets) / n, 2),
            "up_pct": round(100 * up / n, 1),
            "cont_pct": round(100 * cont / n, 1),
            "rev_pct": round(100 * (n - cont) / n, 1),
            "cont_ci": [lo, hi], "rev_ci": [round(100 - hi, 1), round(100 - lo, 1)]}


def verdict_zh(state: str, cell: dict) -> str:
    n = cell.get("n") or 0
    if n < MIN_N:
        return f"樣本不足（{n}/{MIN_N}）"
    lo, hi = cell["rev_ci"] if state in BUILDS else cell["cont_ci"]
    if state in BUILDS:
        if lo > 50:
            return "反向擠壓成立：多數時候價格轉頭"
        if hi < 50:
            return "沒有擠壓：多數時候順著堆積方向走"
        return "分不出來（區間含 50%）"
    if lo > 50:
        return "多半延續"
    if hi < 50:
        return "多半反轉"
    return "分不出來（區間含 50%）"


def summary(store: dict, horizon_for_verdict: int = 4) -> dict:
    """Per read × horizon: n, median, and the continued/reversed share with
    its interval. Open rows contribute the horizons they have already scored,
    so the 1h column fills long before the 24h one closes."""
    book = book_of(store)
    rows = list(book["open"].values()) + list(book["closed"])
    by = {s: {str(h): [] for h in HORIZONS_H} for s in FOLLOW_UP}
    for e in rows:
        s = e.get("state")
        if s not in by:
            continue
        for h, v in (e.get("ret") or {}).items():
            if h in by[s] and isinstance(v, (int, float)):
                by[s][h].append(float(v))
    states = {}
    for s, hs in by.items():
        cells = {h: _cell(s, v) for h, v in hs.items()}
        states[s] = {"h": cells,
                     "verdict_zh": verdict_zh(s, cells[str(horizon_for_verdict)])}
    squeezes = [e for e in book["closed"] if e.get("state") in BUILDS
                and isinstance(e.get("max_up"), (int, float))]
    return {
        "states": states,
        "horizons": list(HORIZONS_H),
        "min_n": MIN_N,
        "pending": len(book["open"]),
        "n_lifetime": int(book.get("n_lifetime") or 0),
        "dropped": int(book.get("dropped") or 0),
        # Median worst excursion against the crowd inside 24h — the size of
        # the "fuel" the alert talks about, measured instead of asserted.
        "squeeze_median_pct": (
            round(statistics.median(
                [-e["max_dn"] if e["state"] == "longs_opening" else e["max_up"]
                 for e in squeezes]), 2) if squeezes else None),
        "squeeze_n": len(squeezes),
    }


def record_line(store: dict, state: str, horizon: int = 4) -> str:
    """One line for the Telegram alert, or "" until the sample can carry it.
    Written as a count of past events, never as a probability of this one."""
    if state not in FOLLOW_UP:
        return ""
    cell = summary(store).get("states", {}).get(state, {}).get("h", {}).get(str(horizon)) or {}
    n = cell.get("n") or 0
    if n < MIN_N:
        return ""
    if state in BUILDS:
        return (f"📓 過去 {n} 次同類堆積，{horizon}h 後 {cell['rev_pct']:.0f}% 價格反向"
                f"（區間 {cell['rev_ci'][0]:.0f}–{cell['rev_ci'][1]:.0f}%），"
                f"中位數 {cell['median']:+.1f}%")
    return (f"📓 過去 {n} 次同類平倉，{horizon}h 後 {cell['cont_pct']:.0f}% 延續原方向，"
            f"中位數 {cell['median']:+.1f}%")
