"""
📓 Vegas 隧道翻多 track record — forward data on the shape, in the units it is
actually sold in.

DELIBERATELY NOT A COPY OF flip_outcomes. That book settles a TRADE: entry,
stop, target, R. This signal ships no entry, stop or target — it is an observe
-list flag, and the claim printed beside it is about forward PRICE:

    mean +0.47% at 6h, +2.05% at 48h … but median -0.21% and -0.23%

So that is what gets recorded. Borrowing the R machinery would have meant
inventing a stop this signal never proposed, and then the live book would be
answering a question the card never asked while the card's own claim stayed
unfalsifiable. This repo has shipped that mistake before — the ⭐ tier was
printed in every alert for weeks and stored nowhere.

MEDIAN IS RECORDED ALONGSIDE MEAN, not instead of it. The backtest's whole
character is that those two disagree in sign: the average is carried by a
minority of large winners while the typical signal drifts down. A book that
kept only the mean would confirm the flattering half of its own claim.
"""
import json
import os
import statistics
import time

STORE_FILE = os.path.join(os.path.dirname(__file__), "vegas_outcomes.json")

# The horizons the card quotes, in hours. Changing these orphans the stored
# rows against the printed claim, so they are not env-tunable.
HORIZONS = (6, 24, 48)
TIMEFRAME = "1h"
MAX_EVAL_PER_TICK = int(os.getenv("VEGAS_EVAL_PER_TICK", "8"))
PACE_SEC = float(os.getenv("VEGAS_PACE_SEC", "0.25"))
KEEP_CLOSED = int(os.getenv("VEGAS_KEEP_CLOSED", "400"))
KEEP_RECENT = 60


def _blank() -> dict:
    return {"open": {}, "closed": [], "recent": []}


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
        json.dump(store, f, ensure_ascii=False)
    os.replace(tmp, p)


def key_of(sig: dict) -> str:
    """bar_ts, not wall-clock — re-reading the same closed bar must not create
    a second copy of the same signal."""
    return f"{sig.get('symbol')}:{int(sig.get('ts') or 0)}"


def record(sig: dict, store: dict = None, now_ts: float = None) -> bool:
    store = load() if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    k = key_of(sig)
    if k in store["open"] or any(c.get("key") == k for c in store["closed"]):
        return False
    row = {
        "key": k, "symbol": sig.get("symbol"), "base": sig.get("base"),
        "bar_ts": int(sig.get("ts") or 0), "fired_ts": now_ts,
        "close": sig.get("close"),
        # Stored so the live book can answer what the backtest could not be
        # asked without re-fitting it: whether the size of the surge matters.
        "vol_mult": sig.get("vol_mult"),
        "atr_pct": sig.get("atr_pct"),
        "slope": sig.get("slope"),
        "tunnel_top": sig.get("tunnel_top"),
        "tunnel_bottom": sig.get("tunnel_bottom"),
        "ema200": sig.get("ema200"),
        "loud": sig.get("loud"),
        "side": "long",
    }
    store["open"][k] = row
    store.setdefault("recent", []).insert(0, row)
    store["recent"] = store["recent"][:KEEP_RECENT]
    return True


def note(sig: dict, now_ts: float = None, path: str = None) -> bool:
    store = load(path)
    added = record(sig, store, now_ts)
    if added:
        save(store, path)
    return added


def settle(row: dict, candles: list, now_ts: float,
           horizons: tuple = None) -> dict:
    """Forward % at each horizon, or None while the longest one is unreached.

    Entry is the OPEN OF THE NEXT BAR. The signal is read at a bar's close, so
    that bar cannot fill its own trade — the same rule strategy4_outcomes.settle
    uses, and the difference between a measurement and a flattering one.
    """
    bar = int(row.get("bar_ts") or 0)
    rows = sorted([c for c in (candles or []) if int(c[0]) >= bar],
                  key=lambda c: c[0])
    if len(rows) < 2 or int(rows[0][0]) != bar:
        return None
    entry = rows[1][1]
    if not entry:
        return None
    out = {}
    for h in (horizons or HORIZONS):
        # rows[0] is the signal bar; the bar h hours after ENTRY is index h+1.
        if len(rows) <= h + 1:
            return None
        out[f"fwd_{h}h"] = round((rows[h + 1][4] - entry) / entry * 100, 4)
    return {**row, "entry": entry, "settled_ts": now_ts, **out}


def evaluate_open(client=None, store: dict = None, now_ts: float = None,
                  max_eval: int = None, horizons: tuple = None,
                  path: str = None) -> dict:
    store = load(path) if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    max_eval = MAX_EVAL_PER_TICK if max_eval is None else max_eval
    hz = horizons or HORIZONS
    done = {"settled": 0, "still_open": 0, "errors": 0}
    if client is None:
        return done
    longest = max(hz) * 3600
    # Least-recently-checked first. Sorted by age with a per-tick cap, the same
    # rows get looked at every tick and everything behind them starves — the
    # bug strategy4_outcomes had to learn twice.
    pending = sorted(store["open"].values(),
                     key=lambda t: (t.get("checked_ts") or 0, t.get("bar_ts") or 0))
    for row in pending[:max_eval]:
        # Nothing to learn until the longest horizon has actually elapsed.
        if now_ts - (row.get("bar_ts") or 0) / 1000.0 < longest + 3600:
            done["still_open"] += 1
            continue
        row["checked_ts"] = now_ts          # stamped before the call can fail
        try:
            candles = client.call("fetch_ohlcv", row["symbol"], TIMEFRAME,
                                  int(row["bar_ts"]), max(hz) + 5)
        except Exception:  # noqa: BLE001 — a dead symbol must not stall the book
            done["errors"] += 1
            continue
        finally:
            time.sleep(PACE_SEC)
        closed = settle(row, candles, now_ts, hz)
        if not closed:
            done["still_open"] += 1
            continue
        store["open"].pop(row["key"], None)
        store["closed"].append(closed)
        done["settled"] += 1
    if len(store["closed"]) > KEEP_CLOSED:
        store["closed"] = store["closed"][-KEEP_CLOSED:]
    return done


def tick(client=None, now_ts: float = None, horizons: tuple = None,
         path: str = None) -> dict:
    store = load(path)
    done = evaluate_open(client, store, now_ts, horizons=horizons, path=path)
    save(store, path)
    return {**done, "open": len(store["open"]), "closed": len(store["closed"])}


# ── the read ─────────────────────────────────────────────────────────────────
def stats(store: dict = None, horizons: tuple = None) -> dict:
    """Mean AND median forward % per horizon, from every settled row."""
    store = load() if store is None else store
    closed = store.get("closed") or []
    out = {"n": len(closed), "horizons": {}}
    for h in (horizons or HORIZONS):
        vals = [c.get(f"fwd_{h}h") for c in closed
                if isinstance(c.get(f"fwd_{h}h"), (int, float))]
        if not vals:
            # No rows yet is NOT "0.0%". A neutral default here would assert
            # the signal went nowhere, which is a claim about the market made
            # by an empty book.
            out["horizons"][h] = {"n": 0, "mean": None, "median": None,
                                  "up_pct": None}
            continue
        out["horizons"][h] = {
            "n": len(vals),
            "mean": round(statistics.fmean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "up_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        }
    return out


def web_view(store: dict = None, limit: int = 20) -> dict:
    import vegas_reclaim as V
    store = load() if store is None else store

    def _one_per_symbol(rows):
        seen, out = set(), []
        for r in sorted(rows or [], key=lambda x: -(x.get("fired_ts") or 0)):
            s = r.get("symbol")
            if s and s not in seen:
                seen.add(s)
                out.append(r)
        return out

    open_rows = _one_per_symbol((store.get("open") or {}).values())
    open_syms = {r.get("symbol") for r in open_rows}
    recent = [r for r in _one_per_symbol(store.get("recent"))
              if r.get("symbol") not in open_syms]
    return {
        "recent": recent[:limit],
        "open": open_rows[:limit],
        "live": stats(store),
        "measured": V.MEASURED,
        "params": {
            "timeframe": V.TIMEFRAME, "vol_mult": V.VOL_MULT,
            "vol_loud": V.VOL_LOUD, "flip_window": V.FLIP_WINDOW,
            "min_atr_pct": V.MIN_ATR_PCT,
            "fast": V.FAST_EMA, "slow": V.SLOW_EMA, "trend": V.TREND_EMA,
        },
        "horizons": list(HORIZONS),
    }
