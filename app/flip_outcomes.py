"""
📓 壓力翻支撐 track record — what actually happened to every flip we alerted.

breakout_flip ships with 163 backtested trades whose confidence interval
straddles zero, over ONE ~10-day regime. That is not a verdict, it is a
hypothesis, and the only thing that can settle it is forward data collected the
same way every time. This module collects it.

REUSES strategy4_outcomes' settlement rather than reimplementing it. That
function already encodes the decisions that determine whether a record is
honest, and each one took a bug to learn:
  · same-bar tie → STOP, because a 15m range containing both levels cannot say
    which came first and guessing the target is flattering AND unfalsifiable
  · the signal bar cannot fill its own trade — entry is the next bar
  · both sides scored from the trade's own direction
A second copy of that logic would drift from it, and the drift would be silent
and in the flattering direction.

SEGMENTED BY blue_sky, on purpose. The backtest's most interesting split was
blue sky +0.137R vs ceiling-nearby −0.261R, and that is exactly the claim
forward data needs to confirm or kill. Pooled into one number it cannot do
either.
"""
import json
import os
import time

import strategy4_outcomes as S4O

STORE_FILE = os.path.join(os.path.dirname(__file__), "flip_outcomes.json")

TRACK_HOURS = float(os.getenv("FLIP_TRACK_HOURS", "48"))
MAX_EVAL_PER_TICK = int(os.getenv("FLIP_EVAL_PER_TICK", "12"))
KEEP_CLOSED = int(os.getenv("FLIP_KEEP_CLOSED", "400"))
PACE_SEC = float(os.getenv("FLIP_PACE_SEC", "0.25"))
TIMEFRAME = "15m"


def _blank() -> dict:
    return {"open": {}, "closed": [], "tally": {}, "recent": []}


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


def key_of(sig: dict) -> str:
    """bar_ts, not wall-clock: re-running the same bar must not create a second
    copy of the same flip."""
    return f"{sig.get('symbol')}:{int(sig.get('ts') or 0)}"


def record(sig: dict, store: dict = None, now_ts: float = None) -> bool:
    """Add one alerted flip to the open book. False if it was already there."""
    store = load() if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    pl = sig.get("plan") or {}
    entry, sl, tp = pl.get("entry"), pl.get("sl"), pl.get("tp")
    if not entry or not sl or not tp or not (sl < entry < tp):
        return False
    k = key_of(sig)
    if k in store["open"] or any(c.get("key") == k for c in store["closed"]):
        return False
    row = {
        "key": k, "symbol": sig.get("symbol"), "base": sig.get("base"),
        "entry": entry, "sl": sl, "tp": tp, "side": "long",
        # The split the backtest cared about, carried into the live record.
        "segment": "blue" if sig.get("blue_sky") else "ceiling",
        "blue_sky": bool(sig.get("blue_sky")),
        "room_pct": sig.get("room_pct"),
        "zone_top": sig.get("zone_top"), "zone_bottom": sig.get("zone_bottom"),
        "touches": sig.get("touches"), "stop_pct": pl.get("stop_pct"),
        "rr": pl.get("rr"),
        # The ⭐ tier was printed in the log and shown in the alert but never
        # STORED, so the one cut this module claims is its best (MEASURED_SEQ,
        # +0.287R) had no live record at all — unfalsifiable by construction.
        "full_setup": bool(sig.get("full_setup")),
        "triangle_ts": sig.get("triangle_ts"),
        # MACD/volume as they stood at the confirming bar. Shown and stored,
        # never required — see breakout_flip.confirm_context().
        "context": sig.get("context") or {},
        # settle() measures from the bar AFTER this one.
        "bar_ts": int(sig.get("ts") or 0), "fired_ts": now_ts,
    }
    store["open"][k] = row
    store.setdefault("recent", []).insert(0, row)
    store["recent"] = store["recent"][:40]
    return True


def evaluate_open(client=None, store: dict = None, now_ts: float = None,
                  max_eval: int = None) -> dict:
    """Settle what has resolved, least-recently-checked first.

    The ordering is the same lesson strategy4_outcomes learned the hard way:
    sorted by age with a per-tick cap, the first `max_eval` trades are the SAME
    trades every tick and everything behind them is never looked at. Rotating
    on checked_ts bounds every row's staleness instead.
    """
    store = load() if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    max_eval = MAX_EVAL_PER_TICK if max_eval is None else max_eval
    done = {"settled": 0, "still_open": 0, "errors": 0}
    if client is None:
        return done

    pending = sorted(store["open"].values(),
                     key=lambda t: (t.get("checked_ts") or 0, t.get("bar_ts") or 0))
    need = int(TRACK_HOURS * 60 / 15) + 5
    for trade in pending[:max_eval]:
        trade["checked_ts"] = now_ts          # stamped before the call can fail
        try:
            candles = client.call("fetch_ohlcv", trade["symbol"], TIMEFRAME,
                                  int(trade["bar_ts"]), need)
        except Exception:  # noqa: BLE001 — a dead symbol must not stall the book
            done["errors"] += 1
            continue
        finally:
            time.sleep(PACE_SEC)
        closed = S4O.settle(trade, candles, now_ts, track_hours=TRACK_HOURS)
        if not closed:
            done["still_open"] += 1
            continue
        store["open"].pop(trade["key"], None)
        store["closed"].append(closed)
        S4O.accumulate(store, closed)
        done["settled"] += 1

    if len(store["closed"]) > KEEP_CLOSED:
        store["closed"] = store["closed"][-KEEP_CLOSED:]
    return done


def tick(client=None, now_ts: float = None) -> dict:
    store = load()
    done = evaluate_open(client, store, now_ts)
    save(store)
    return {**done, "open": len(store["open"]), "closed": len(store["closed"])}


def note(sig: dict, now_ts: float = None) -> bool:
    """Record one flip and persist immediately — called from the sweep."""
    store = load()
    added = record(sig, store, now_ts)
    if added:
        save(store)
    return added


# ── the read ─────────────────────────────────────────────────────────────────
SEGMENT_ZH = {"all": "全部", "blue": "上方無壓", "ceiling": "上方有壓"}


def stats(store: dict = None, segment: str = "all") -> dict:
    return S4O.stats(store if store is not None else load(), segment)


def web_view(store: dict = None, limit: int = 20) -> dict:
    """Everything the dashboard card needs."""
    import breakout_flip as B
    store = load() if store is None else store
    closed = sorted(store.get("closed") or [],
                    key=lambda c: c.get("exit_ts") or 0, reverse=True)
    segs = [s for s in ("all", "blue", "ceiling")
            if (store.get("tally") or {}).get(s)]
    # One row per SYMBOL. The previous client-side dedupe keyed on
    # `symbol:bar_ts`, which is unique per flip — so the same coin flipping
    # again produced a second key and rendered twice. Live it was GENIUS×6,
    # BLESS×6, AWE×5. Keying on the symbol is the only thing that fixes it,
    # and doing it here means /market and the dashboard cannot disagree.
    def _one_per_symbol(rows):
        seen, out = set(), []
        for r in sorted(rows or [], key=lambda x: -(x.get("fired_ts") or 0)):
            sym = r.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                out.append(r)
        return out

    open_rows = _one_per_symbol((store.get("open") or {}).values())
    open_syms = {r.get("symbol") for r in open_rows}
    # A symbol that is still OPEN must not also appear from `recent` — it is
    # one position, not two.
    recent_rows = [r for r in _one_per_symbol(store.get("recent"))
                   if r.get("symbol") not in open_syms]
    return {
        "recent": recent_rows[:limit],
        "open": open_rows,
        "closed": closed[:30],
        "stats": {s: stats(store, s) for s in segs},
        "segment_zh": SEGMENT_ZH,
        "track_hours": TRACK_HOURS,
        # The backtest is shown next to the live record on purpose: it is the
        # claim being tested, and a live sample of 12 next to a hidden 163
        # invites reading the small number as the answer.
        "measured": B.MEASURED,
        # The Telegram alert carries the out-of-sample re-measurement and the
        # ⭐ tier; the page carried only the original 163-trade backtest, so the
        # two disagreed about what has been measured. Same module, same numbers,
        # both surfaces.
        "measured_oos": B.MEASURED_OOS,
        "measured_seq": B.MEASURED_SEQ,
    }
