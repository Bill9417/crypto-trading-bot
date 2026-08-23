"""
📒 The trade log — every signal, uncapped, and the variants replayed over it.

Asked for 2026-08-22: a page listing every trade, in two versions —
  TYPE 1  exactly what is recorded now: take them as they come
  TYPE 2  the same, except SKIP a setup with resistance overhead (上方有壓)
and keep a full log for both.

WHY TYPE 2 IS NOT A FILTER ON TYPE 1'S LOG, which is the whole design problem:

A book that holds 8 positions at once DECLINES what fires while it is full.
The flip book has declined 824 signals against 82 taken — 91% of everything it
saw. So dropping the 上方有壓 rows from that log does not give you Type 2. It
gives you Type 1 minus some trades, which is a different and much worse thing:
in reality, skipping a ceiling setup FREES A SLOT, and a later setup that
Type 1 had to decline gets taken instead. The two variants hold genuinely
different portfolios, and neither is a subset of the other.

The only way to compute both honestly is to record EVERY signal's outcome with
no cap, and apply each variant's filter and capacity at READ time. That is what
this module is: an uncapped log, plus a replay that walks it in fire order and
decides what each rule-set would actually have held.

WHAT THIS CANNOT RECOVER. The 824 already declined were never settled, so their
outcomes do not exist and no amount of arithmetic invents them. The log starts
from the moment it is switched on, and every variant computed here is honest
only about signals recorded after that point. `since` is reported on the page
for exactly this reason.
"""
import json
import os
import time

STORE_FILE = os.path.join(os.path.dirname(__file__), "trade_log.json")

# The uncapped log is the point, so it is only pruned when it would grow
# unreasonably — and it prunes the OLDEST settled rows, never the open ones.
KEEP = int(os.getenv("TRADELOG_KEEP", "4000"))
MAX_EVAL_PER_TICK = int(os.getenv("TRADELOG_EVAL_PER_TICK", "14"))
PACE_SEC = float(os.getenv("TRADELOG_PACE_SEC", "0.2"))
TRACK_HOURS = float(os.getenv("TRADELOG_TRACK_HOURS", "48"))
TIMEFRAME = "15m"

# Default capacity for a replay. Same 8 the live books use — the number a 25
# USDT account could plausibly carry — and overridable per variant.
MAX_CONCURRENT = int(os.getenv("TRADELOG_MAX_CONCURRENT", "8"))


def _blank() -> dict:
    return {"rows": [], "since": None}


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
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as exc:  # noqa: BLE001 — a log must not break the sweep
        print(f"[tradelog] save failed: {exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def key_of(sig: dict) -> str:
    """bar_ts, not wall-clock: re-reading the same closed bar must not create a
    second copy of the same signal."""
    return f"{sig.get('symbol')}:{int(sig.get('ts') or sig.get('bar_ts') or 0)}"


def record(sig: dict, source: str, store: dict = None,
           now_ts: float = None, path: str = None) -> bool:
    """Log one signal. NO capacity check — that is the point.

    Every field a row needs to be replayed later is copied in now: the plan
    (entry/sl/tp), the timestamps, and whatever the variant filters key on
    (`segment` for the ceiling rule). A field not stored here cannot be
    filtered on later, and back-filling it is impossible once the bar is gone.
    """
    store = load(path) if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    pl = sig.get("plan") or {}
    entry, sl, tp = pl.get("entry"), pl.get("sl"), pl.get("tp")
    if not entry or not sl or not tp:
        return False
    k = key_of(sig)
    if any(r.get("key") == k for r in store["rows"]):
        return False
    store["rows"].append({
        "key": k, "source": source,
        "symbol": sig.get("symbol"), "base": sig.get("base"),
        "side": pl.get("side") or sig.get("side") or "long",
        "entry": entry, "sl": sl, "tp": tp,
        "stop_pct": pl.get("stop_pct"), "rr": pl.get("rr"),
        # What the variants key on. blue_sky is the flip's own reading of
        # whether anything sits overhead; room_pct is how far away it is.
        "blue_sky": sig.get("blue_sky"),
        "room_pct": sig.get("room_pct"),
        "segment": "blue" if sig.get("blue_sky") else "ceiling",
        "full_setup": sig.get("full_setup"),
        "touches": sig.get("touches"),
        "bar_ts": int(sig.get("ts") or 0), "fired_ts": now_ts,
        # settle() fills these; present as None so a row's shape never changes.
        "outcome": None, "exit_price": None, "exit_ts": None,
        "r": None, "r_gross": None, "cost_r": None,
    })
    if store.get("since") is None:
        store["since"] = now_ts
    return True


def note(sig: dict, source: str, now_ts: float = None) -> bool:
    store = load()
    added = record(sig, source, store, now_ts)
    if added:
        save(store)
    return added


# ── settlement ───────────────────────────────────────────────────────────────
def open_rows(store: dict) -> list:
    return [r for r in store.get("rows") or [] if r.get("outcome") is None]


def evaluate(client=None, store: dict = None, now_ts: float = None,
             max_eval: int = None, path: str = None) -> dict:
    """Settle what has resolved, least-recently-checked first.

    Same rotation as the other books: sorted by age with a per-tick cap, the
    first `max_eval` rows are the SAME rows every tick and everything behind
    them starves.
    """
    import strategy4_outcomes as S4O
    store = load(path) if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    max_eval = MAX_EVAL_PER_TICK if max_eval is None else max_eval
    done = {"settled": 0, "still_open": 0, "errors": 0}
    if client is None:
        return done
    pending = sorted(open_rows(store),
                     key=lambda r: (r.get("checked_ts") or 0, r.get("bar_ts") or 0))
    need = int(TRACK_HOURS * 60 / 15) + 5
    for row in pending[:max_eval]:
        row["checked_ts"] = now_ts          # stamped before the call can fail
        try:
            candles = client.call("fetch_ohlcv", row["symbol"], TIMEFRAME,
                                  int(row["bar_ts"]), need)
        except Exception:  # noqa: BLE001 — a dead symbol must not stall the log
            done["errors"] += 1
            continue
        finally:
            time.sleep(PACE_SEC)
        closed = S4O.settle(row, candles, now_ts, track_hours=TRACK_HOURS)
        if not closed:
            done["still_open"] += 1
            continue
        row.update({k: closed.get(k) for k in
                    ("outcome", "exit_price", "exit_ts", "r", "r_gross",
                     "cost_r", "mae", "mfe", "bars")})
        done["settled"] += 1
    rows = store.get("rows") or []
    if len(rows) > KEEP:
        # Oldest SETTLED first; an open row is still needed and is never cut.
        settled = [r for r in rows if r.get("outcome") is not None]
        keep_open = [r for r in rows if r.get("outcome") is None]
        settled.sort(key=lambda r: r.get("exit_ts") or 0)
        store["rows"] = settled[-(KEEP - len(keep_open)):] + keep_open
    return done


def tick(client=None, now_ts: float = None) -> dict:
    store = load()
    done = evaluate(client, store, now_ts)
    save(store)
    return {**done, "rows": len(store.get("rows") or []),
            "open": len(open_rows(store))}


# ── the variants ─────────────────────────────────────────────────────────────
# A variant is a NAME, a filter, and a capacity. Nothing else — the point is
# that two rule-sets see identical data and differ only in what they decline.
VARIANTS = {
    "all": {
        "label": "全部進場",
        "label_en": "Take every setup",
        "desc": "訊號來就進，滿了就等 —— 現在紀錄的做法",
        "keep": lambda r: True,
    },
    "blue_only": {
        "label": "跳過上方有壓",
        "label_en": "Skip setups with resistance overhead",
        "desc": "只做上方無壓的；有壓力的直接跳過，空出來的位子留給後面的訊號",
        "keep": lambda r: r.get("segment") == "blue",
    },
}


def replay(rows: list, keep=None, max_concurrent: int = None) -> dict:
    """What a rule-set would ACTUALLY have held, walking signals in fire order.

    The slot accounting is the whole reason this exists. A filtered-out setup
    does not merely vanish from the results — it never occupies a slot, so a
    later setup the unfiltered run had to decline gets taken instead. Two
    variants over the same signals hold different portfolios, and neither is a
    subset of the other.

    A row still open occupies its slot to the end of the walk, because it does
    in reality: capacity you have not got back is capacity you have not got.
    """
    cap = MAX_CONCURRENT if max_concurrent is None else max_concurrent
    ordered = sorted([r for r in rows or [] if r.get("fired_ts") is not None],
                     key=lambda r: r["fired_ts"])
    busy_until = []          # exit_ts of positions still held
    taken, skipped = [], []
    for r in ordered:
        t = r["fired_ts"]
        busy_until = [x for x in busy_until if x > t]
        if keep is not None and not keep(r):
            skipped.append({**r, "skipped_because": "filter"})
            continue
        if cap > 0 and len(busy_until) >= cap:
            skipped.append({**r, "skipped_because": "no_slot"})
            continue
        taken.append(r)
        # An unsettled row is still held; float("inf") keeps its slot busy for
        # the rest of the walk rather than silently freeing it.
        busy_until.append(r.get("exit_ts") or float("inf"))
    return {"taken": taken, "skipped": skipped}


def stats(taken: list) -> dict:
    """Only SETTLED rows score. An open trade has no result yet, and counting
    it as a zero would be a fabricated outcome."""
    done = [r for r in taken if r.get("outcome") is not None
            and isinstance(r.get("r"), (int, float))]
    n = len(done)
    out = {"n": n, "open": len(taken) - n, "wins": 0, "losses": 0,
           "exp": None, "net": None, "wr": None, "ci": None, "gross_exp": None}
    if not n:
        return out
    rs = [float(r["r"]) for r in done]
    out["wins"] = sum(1 for x in rs if x > 0)
    out["losses"] = n - out["wins"]
    out["net"] = round(sum(rs), 3)
    out["exp"] = round(sum(rs) / n, 4)
    out["wr"] = round(out["wins"] / n * 100, 1)
    gross = [float(r["r_gross"]) for r in done
             if isinstance(r.get("r_gross"), (int, float))]
    out["gross_exp"] = round(sum(gross) / len(gross), 4) if gross else None
    if n > 1:
        mean = out["exp"]
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        se = (var ** 0.5) / (n ** 0.5)
        out["ci"] = [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)]
    return out


def sources(store: dict = None) -> dict:
    """{source: count} — which detectors are in the log."""
    store = load() if store is None else store
    out = {}
    for r in store.get("rows") or []:
        s = r.get("source") or "?"
        out[s] = out.get(s, 0) + 1
    return out


def board(store: dict = None, limit: int = 500, source: str = None) -> dict:
    """Every variant, computed from the same rows, with the full trade list.

    `source` scopes it to one detector. Mixing two detectors into one variant
    would answer a question nobody asked — they have different setups, different
    stops and different hit rates, and a combined expectancy is an average over
    two populations rather than a result for either.
    """
    store = load() if store is None else store
    rows = store.get("rows") or []
    if source:
        rows = [r for r in rows if (r.get("source") or "?") == source]
    out = {"since": store.get("since"), "logged": len(rows),
           "source": source, "sources": sources(store),
           "open": sum(1 for r in rows if r.get("outcome") is None),
           "max_concurrent": MAX_CONCURRENT,
           "variants": {}}
    for name, v in VARIANTS.items():
        res = replay(rows, keep=v["keep"])
        st = stats(res["taken"])
        by_reason = {}
        for r in res["skipped"]:
            by_reason[r["skipped_because"]] = by_reason.get(
                r["skipped_because"], 0) + 1
        out["variants"][name] = {
            "label": v["label"], "label_en": v["label_en"], "desc": v["desc"],
            "stats": st,
            "skipped": by_reason,
            # Newest first — the list is read, not scrolled from the start.
            "trades": sorted(res["taken"],
                             key=lambda r: -(r.get("fired_ts") or 0))[:limit],
        }
    # What the two rule-sets did DIFFERENTLY, which is the only reason to run
    # both: a coin one took and the other never saw.
    a = {r["key"] for r in out["variants"]["all"]["trades"]}
    b = {r["key"] for r in out["variants"]["blue_only"]["trades"]}
    out["only_in_all"] = len(a - b)
    out["only_in_blue"] = len(b - a)
    return out
