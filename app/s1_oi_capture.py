"""
📓 Open interest at the moment S1 fires — so the question becomes answerable.

Asked on 2026-08-13: "add OI to S1 to know if it will be better or not."

MEASURED FIRST, AND THE ANSWER WAS THAT IT CANNOT BE MEASURED YET:

  · Binance retains ~30 days of openInterestHist. There is no free source of
    longer history — CoinGlass-style archives are paid.
  · S1 fires 0.1 times per symbol per 30 days (7 trades over 180 days across 8
    liquid crypto symbols, replayed through backtest.evaluate).
  · So the whole OI window contains about THREE S1 trades across 25 symbols —
    measured directly, and exactly what that rate predicts.

Three trades cannot show whether a filter helps. Splitting them would produce
two groups of one or two and a number that looks like an answer, which is worse
than no answer: this repo has already measured 48 high-win-rate setups and
found 46 losing money, and every one of them came from a small sample read
confidently.

The only honest route is forward: record the OI state at the instant each S1
signal fires, and wait. That is all this module does. It never filters, gates or
blocks anything — S1 behaves exactly as before — it only writes down what open
interest was doing, so that in a few months the split can be looked at with a
real sample behind it.

At S1's rate, 25 symbols produce roughly 4 signals a month, so a usable sample
is a year away on this universe. That is a genuine and unwelcome answer; it is
still the true one, and knowing it beats acting on three trades.
"""
import json
import os
import time

STORE_FILE = os.path.join(os.path.dirname(__file__), "s1_oi_capture.json")
KEEP = int(os.getenv("S1_OI_KEEP", "2000"))
# Same window crowd_radar ranks on, so "OI was building" means one thing across
# this codebase rather than two.
SPAN_BARS = 8
MIN_N_FOR_SPLIT = 30


def _blank() -> dict:
    return {"rows": [], "started": None}


def load(path: str = None) -> dict:
    try:
        with open(path or STORE_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return _blank()
    for k, v in _blank().items():
        d.setdefault(k, v)
    return d


def save(store: dict, path: str = None) -> None:
    p = path or STORE_FILE
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(tmp, p)


def oi_snapshot(symbol: str) -> dict:
    """OI %change and the four-state read for one symbol, right now.

    Returns {} on any failure. A missing snapshot must never stop a signal:
    the trade is the product, this is bookkeeping about it.
    """
    import crowd_radar as C
    raw = symbol.split("/")[0].split(":")[0]
    raw = raw if raw.endswith("USDT") else raw + "USDT"
    oi, px = C.oi_history(raw, period="15m", limit=200)
    if len(oi) < SPAN_BARS + 2:
        return {}
    try:
        oi = list(oi) + [C.live_oi(raw)]
        px = list(px) + [px[-1]]
    except Exception:  # noqa: BLE001 — the closed bar is good enough here
        pass
    oi_pct = (oi[-1] - oi[-1 - SPAN_BARS]) / abs(oi[-1 - SPAN_BARS]) * 100
    px_pct = (px[-1] - px[-1 - SPAN_BARS]) / abs(px[-1 - SPAN_BARS]) * 100
    dist = C.pct_changes(oi[:-1], SPAN_BARS)
    return {"oi_pct": round(oi_pct, 3), "px_pct": round(px_pct, 3),
            "state": C.oi_read(oi_pct, px_pct),
            "pctile": round(C.percentile_of(dist, oi_pct), 1),
            "samples": len(dist)}


def note(symbol: str, direction: str, lights=None, entry=None,
         now: float = None) -> bool:
    """Record one S1 signal with its OI state. Never raises."""
    try:
        snap = oi_snapshot(symbol)
        if not snap:
            return False
        store = load()
        store.setdefault("rows", []).append({
            "symbol": symbol, "direction": direction, "lights": lights,
            "entry": entry, "ts": now if now is not None else time.time(),
            **snap,
        })
        store["rows"] = store["rows"][-KEEP:]
        if not store.get("started"):
            store["started"] = time.time()
        save(store)
        return True
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks a signal
        print(f"[s1oi] capture failed for {symbol}: {exc}")
        return False


def report(store: dict = None) -> dict:
    """What the capture can say so far — usually 'not yet', and it says so.

    The refusal is the point. With n below MIN_N_FOR_SPLIT this returns a
    verdict of 樣本太少 rather than an expectancy, because a split of six
    trades into two groups of three is exactly the shape of evidence this
    project keeps disproving.
    """
    store = load() if store is None else store
    rows = store.get("rows") or []
    days = ((time.time() - store["started"]) / 86400.0) if store.get("started") else 0
    out = {"n": len(rows), "days": round(days, 1),
           "started": store.get("started"),
           "enough": len(rows) >= MIN_N_FOR_SPLIT,
           "need": max(0, MIN_N_FOR_SPLIT - len(rows))}
    if not rows:
        out["verdict"] = "還沒有 S1 訊號 —— S1 每個幣約 30 天才 0.1 次"
        return out
    building = [r for r in rows
                if r.get("state") in ("longs_opening", "shorts_opening")]
    out["building"] = len(building)
    out["unwinding"] = len(rows) - len(building)
    if not out["enough"]:
        out["verdict"] = (f"樣本太少（{len(rows)}/{MIN_N_FOR_SPLIT}）—— "
                          f"還不能說 OI 對 S1 有沒有幫助")
    else:
        out["verdict"] = "樣本已足夠，可以做分組比較"
    return out
