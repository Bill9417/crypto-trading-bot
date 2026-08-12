"""Did the S4 setup work? — outcome tracking for the alert-only radar.

S4 fires alerts and never places an order, which makes it the one strategy here
whose record has to be kept deliberately: there are no exchange fills to
reconcile against later. Until now nothing was kept at all — strategy4_signals
.json is OVERWRITTEN by every scan, so the moment a setup stopped qualifying,
the fact that it ever fired was gone. The module docstring in strategy4.py
promises "the scan records every signal it fires so that test has data to work
with when the time comes". It did not. This is that record.

WHAT IS TRACKED
Only signals that were ALERTED, not every signal every scan produced. A setup
that stays valid for two hours reappears in eight consecutive scans; counting
it eight times would inflate n eightfold and make every statistic meaningless.
strategy4.due_signals already applies the alert cooldown, so it is the honest
unit: one entry per thing you were actually told about.

THE SAME-CANDLE TIE GOES TO THE STOP
A 15m candle whose range spans both the stop and the target says nothing about
which was touched first. Assuming the target would be flattering and unfalsifi-
able — the reading that makes the strategy look best is exactly the one the
data cannot support. So the stop wins every tie, the same convention
signal_outcomes._step already uses. Real results can only be better than what
this reports, never worse, which is the direction an honest bias should point.

EXPIRED TRADES ARE MARKED TO MARKET, NOT SCORED ZERO
A setup that neither stopped out nor hit target inside the window is closed at
the last close and scored in R. Calling it 0R would quietly flatter anything
that dawdles, and dropping it would be worse still: it would delete precisely
the trades that went nowhere, which is survivorship bias applied to your own
record.

EXACT CONFIDENCE INTERVALS, UNLIKE THE S2 TALLY
/reality has to fall back on a lower-bound interval because signal_outcomes'
lifetime totals never stored a sum of squares and the per-trade R values are
long pruned. This starts clean, so it stores sumsq from the first trade and
reality.ci() can compute the real thing. Same reader, better data.
"""
import json
import os
import time

TZ_NAME = os.getenv("TZ_DISPLAY", "Asia/Taipei")
STORE_FILE = os.path.join(os.path.dirname(__file__), "strategy4_outcomes.json")

# How long a setup gets to resolve before it is marked to market. 48h at 15m is
# 192 bars — the same window signal_outcomes uses, so the two records are
# comparable rather than accidentally different.
TRACK_HOURS = float(os.getenv("S4_TRACK_HOURS", "48"))
# Raised 6→12 on 2026-08-12. The scan that calls this already fetches candles
# for ~140 symbols on the same tick, so a dozen more is not what threatens the
# rate limit — but a cap of 6 was small enough to be smaller than the open book,
# which is what broke the record (see evaluate_open).
MAX_EVAL_PER_TICK = int(os.getenv("S4_EVAL_PER_TICK", "12"))
KEEP_CLOSED = int(os.getenv("S4_KEEP_CLOSED", "400"))
PACE_SEC = float(os.getenv("S4_PACE_SEC", "0.25"))

OUTCOMES = ("tp", "sl", "expired")

# Tally keys. Segments partition the universe, sides partition the direction,
# and the two are tallied independently rather than crossed — a 2×2 grid of
# n≈7 buckets would be four numbers none of which can say anything.
#
# The side split exists because strategy4's own docstring says the short side
# has NOTHING measured behind it, and it was switched on 2026-08-11. Pooling
# both sides into one expectancy is precisely how an unmeasured half hides
# inside a measured one. Prefixed so a side can never collide with a segment.
SIDE_KEYS = ("side:long", "side:short")


def _side_key(side: str) -> str:
    return "side:short" if side == "short" else "side:long"


# ── store ────────────────────────────────────────────────────────────────────
def _blank() -> dict:
    return {"open": {}, "closed": [], "tally": {}}


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
    """One tracked trade. bar_ts (not wall-clock) so a re-run of the same bar
    cannot create a second copy of the same signal."""
    return f"{sig.get('symbol')}:{int(sig.get('bar_ts') or 0)}"


# ── recording ────────────────────────────────────────────────────────────────
def record(signals: list, store: dict = None, now_ts: float = None) -> int:
    """Add alerted signals to the open book. Returns how many were new."""
    store = load() if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    added = 0
    for s in signals or []:
        plan = s.get("plan") or {}
        entry, sl, tp = plan.get("entry"), plan.get("sl"), plan.get("tp")
        side = s.get("side") or plan.get("side") or "long"
        if not entry or not sl or not tp:
            continue                       # unusable plan — nothing to score
        # The geometry test has to follow the side. Left as the long-only form
        # (sl < entry < tp) every short would have been silently dropped here
        # and the book would have shown shorts firing but never being scored.
        ok = (sl < entry < tp) if side == "long" else (tp < entry < sl)
        if not ok:
            continue
        k = key_of(s)
        if k in store["open"] or any(c.get("key") == k for c in store["closed"]):
            continue
        store["open"][k] = {
            "key": k, "symbol": s.get("symbol"), "base": s.get("base"),
            "segment": s.get("segment"), "entry": entry, "sl": sl, "tp": tp,
            "side": side,
            "rr": plan.get("rr"), "stop_pct": plan.get("stop_pct"),
            "quality": s.get("quality"), "score": s.get("score"),
            "div_sources": s.get("div_sources") or [],
            "bar_ts": s.get("bar_ts"), "fired_ts": now_ts,
        }
        added += 1
    return added


# ── settlement (pure — this is the part worth testing) ──────────────────────
def settle(trade: dict, candles: list, now_ts: float = None,
           track_hours: float = TRACK_HOURS) -> dict:
    """Walk the candles AFTER the signal bar and decide what happened.

    Returns {} while the trade is still live and undecided, otherwise the
    closing fields. Handles BOTH sides.

    Order inside a candle is deliberate and pessimistic: the stop is checked
    before the target, so a candle spanning both is scored as a loss. A 15m
    range cannot tell you which came first, and the alternative assumption
    flatters the strategy in a way no future data could ever correct.

    Everything directional below is expressed in R FROM THE TRADE'S SIDE, so a
    short that falls to its target scores +2R exactly as a long that rises to
    its own. Scoring shorts on the long formulae would not merely be wrong, it
    would be wrong in the flattering direction on losses and the punishing one
    on wins — the stop sits ABOVE entry on a short, so `low <= sl` would have
    fired on the first candle of every short ever recorded.
    """
    entry, sl, tp = trade.get("entry"), trade.get("sl"), trade.get("tp")
    if not entry or not sl or not tp:
        return {}
    side = trade.get("side") or "long"
    is_long = side == "long"
    risk = (entry - sl) if is_long else (sl - entry)
    if risk <= 0:
        return {}
    bar_ts = int(trade.get("bar_ts") or 0)
    now_ts = now_ts if now_ts is not None else time.time()

    # Strictly AFTER the signal bar: the setup is read at that bar's close, so
    # its own high and low already happened and cannot fill anything.
    after = [c for c in (candles or []) if int(c[0]) > bar_ts]

    mae = mfe = 0.0
    for c in after:
        high, low = float(c[2]), float(c[3])
        # Adverse excursion is the move against the trade, favourable the move
        # with it — which extreme of the candle supplies each one flips by side.
        adverse = (low - entry) / risk if is_long else (entry - high) / risk
        favour = (high - entry) / risk if is_long else (entry - low) / risk
        mae = min(mae, adverse)
        mfe = max(mfe, favour)
        hit_sl = low <= sl if is_long else high >= sl
        hit_tp = high >= tp if is_long else low <= tp
        if hit_sl:                                      # stop first, always
            return _close(trade, "sl", -1.0, c[0], mae, mfe, after)
        if hit_tp:
            r = (tp - entry) / risk if is_long else (entry - tp) / risk
            return _close(trade, "tp", r, c[0], mae, mfe, after)

    age_h = (now_ts - (bar_ts / 1000.0)) / 3600.0
    if age_h >= track_hours:
        last = float(after[-1][4]) if after else entry
        r = (last - entry) / risk if is_long else (entry - last) / risk
        return _close(trade, "expired", r,
                      after[-1][0] if after else bar_ts, mae, mfe, after)
    return {}                                            # still live


def _close(trade, outcome, r, exit_ms, mae, mfe, after) -> dict:
    return {**trade, "outcome": outcome, "r": round(float(r), 4),
            "exit_ts": exit_ms / 1000.0, "bars": len(after),
            "mae": round(mae, 3), "mfe": round(mfe, 3)}


# ── tally ────────────────────────────────────────────────────────────────────
def _bucket() -> dict:
    return {"n": 0, "sum": 0.0, "sumsq": 0.0, "sumsq_n": 0,
            "wins": 0, "gain": 0.0, "loss": 0.0,
            "tp": 0, "sl": 0, "expired": 0}


def accumulate(store: dict, closed: dict) -> None:
    """Roll one settled trade into the lifetime tally.

    Separate from the `closed` list on purpose: that list is pruned to
    KEEP_CLOSED, and a strategy whose long-run expectancy vanishes with its
    oldest records has no long-run expectancy. sumsq is recorded from the very
    first trade so the intervals can be exact — the hole /reality had to work
    around for S2.
    """
    r = closed.get("r")
    if not isinstance(r, (int, float)):
        return
    seg = closed.get("segment") or "?"
    # Missing side means a row recorded before the short side existed, which
    # settle() scored on the long formulae — so long is what it actually is,
    # not a guess. Both must default identically or the record would disagree
    # with the maths that produced it.
    side = _side_key(closed.get("side") or "long")
    for name in ("all", seg, side):
        b = store.setdefault("tally", {}).setdefault(name, _bucket())
        for k, v in _bucket().items():                 # heal older shapes
            b.setdefault(k, v)
        b["n"] += 1
        b["sum"] = round(b["sum"] + r, 4)
        b["sumsq"] = round(b["sumsq"] + r * r, 4)
        b["sumsq_n"] += 1
        if r > 0:
            b["wins"] += 1
            b["gain"] = round(b["gain"] + r, 4)
        else:
            b["loss"] = round(b["loss"] - r, 4)
        oc = closed.get("outcome")
        if oc in OUTCOMES:
            b[oc] = b.get(oc, 0) + 1


# ── the network step ─────────────────────────────────────────────────────────
def _candles(client, symbol, since_ms, timeframe, need):
    return client.fetch_ohlcv(symbol, timeframe, since_ms, need)


def evaluate_open(client=None, store: dict = None, now_ts: float = None,
                  max_eval: int = None) -> dict:
    """Fetch candles for open trades and settle what has resolved.

    LEAST-RECENTLY-CHECKED FIRST, not oldest-first. That ordering is the whole
    point of this function's correctness, and getting it wrong is not a delay,
    it is a wrong record. Sorted by signal age with a per-tick cap, the first
    `max_eval` trades are the SAME trades on every tick: nothing after position
    `max_eval` is ever looked at until something ahead of it closes. On
    2026-08-12 that left 0G showing as live on /s4 five hours after it had
    stopped out, queued behind six older trades, and it would have stayed there
    until the oldest of them expired 31 hours later.

    Rotating on `checked_ts` bounds the staleness of every row instead: with n
    open and a cap of k, each trade is re-checked at least every ceil(n/k)
    ticks, whatever else is in the book. Trades that error out are stamped too
    — a delisted symbol that can never be fetched must lose its turn like any
    other, or it re-acquires the head of the queue forever and starves the book
    exactly the way the old ordering did.
    """
    import strategy4 as S4
    store = load() if store is None else store
    now_ts = now_ts if now_ts is not None else time.time()
    max_eval = MAX_EVAL_PER_TICK if max_eval is None else max_eval
    ex = client or S4._client()

    pending = sorted(store["open"].values(),
                     key=lambda t: (t.get("checked_ts") or 0, t.get("bar_ts") or 0))
    done = {"settled": 0, "still_open": 0, "errors": 0}
    for trade in pending[:max_eval]:
        bar_ms = int(trade.get("bar_ts") or 0)
        need = int(TRACK_HOURS * 60 / 15) + 5
        trade["checked_ts"] = now_ts       # stamped before the call can fail
        try:
            candles = _candles(ex, trade["symbol"], bar_ms, S4.TIMEFRAME, need)
        except Exception:  # noqa: BLE001 — a dead symbol must not stall the book
            done["errors"] += 1
            continue
        finally:
            # Pacing belongs to the FETCH, not to the settlement: the request is
            # what costs rate limit, and it was charged whether or not the trade
            # happened to resolve. Behind the old placement a book of live
            # trades made every one of its calls back to back.
            time.sleep(PACE_SEC)
        closed = settle(trade, candles, now_ts)
        if not closed:
            done["still_open"] += 1
            continue
        store["open"].pop(trade["key"], None)
        store["closed"].append(closed)
        accumulate(store, closed)
        done["settled"] += 1

    if len(store["closed"]) > KEEP_CLOSED:
        store["closed"] = store["closed"][-KEEP_CLOSED:]
    return done


def backfill_sides(store: dict) -> bool:
    """One-time: build the side buckets from trades settled before they existed.

    Only safe while the `closed` list is still the COMPLETE history, which is
    what the length check tests. `closed` is pruned to KEEP_CLOSED and the
    tally deliberately is not, so once anything has been dropped the list can
    no longer reproduce the totals — replaying it then would silently invent a
    record shorter than the real one. In that case the split simply starts
    from today and says so by being smaller than `all`, which is honest;
    a fabricated backfill would not be.
    """
    tally = store.setdefault("tally", {})
    if any(k in tally for k in SIDE_KEYS) or store.get("side_backfill"):
        return False
    closed = store.get("closed") or []
    total = (tally.get("all") or {}).get("n", 0)
    if total and len(closed) != total:
        store["side_backfill"] = "skipped: closed list is already pruned"
        return False
    for c in closed:
        r = c.get("r")
        if not isinstance(r, (int, float)):
            continue
        b = tally.setdefault(_side_key(c.get("side") or "long"), _bucket())
        b["n"] += 1
        b["sum"] = round(b["sum"] + r, 4)
        b["sumsq"] = round(b["sumsq"] + r * r, 4)
        b["sumsq_n"] += 1
        if r > 0:
            b["wins"] += 1
            b["gain"] = round(b["gain"] + r, 4)
        else:
            b["loss"] = round(b["loss"] - r, 4)
        if c.get("outcome") in OUTCOMES:
            b[c["outcome"]] += 1
    store["side_backfill"] = f"rebuilt from {len(closed)} settled trades"
    return True


def tick(client=None, signals: list = None, now_ts: float = None) -> dict:
    """One maintenance pass: record what just fired, settle what has resolved."""
    store = load()
    backfill_sides(store)
    added = record(signals or [], store, now_ts)
    done = evaluate_open(client, store, now_ts)
    save(store)
    return {"recorded": added, **done, "open": len(store["open"]),
            "closed": len(store["closed"])}


# ── the read ─────────────────────────────────────────────────────────────────
# /s4 is a Chinese page; reality.verdict() writes English for /reality. Keyed on
# the short tag rather than the sentence, so a reworded verdict cannot silently
# fall back to English here.
VERDICT_ZH = {
    "too few": "樣本太少，還不能下任何結論",
    "no edge": "沒有可測量的優勢 —— 信賴區間仍然包含 0",
    "negative": "負期望值，而且區間不含 0",
    "probably negative": "目前為止是賠錢的，但樣本還不足以確定",
    "positive": "正期望值，而且區間不含 0",
    "probably positive": "目前為止是賺錢的，但還沒得到證實",
}


def stats(store: dict = None, segment: str = "all") -> dict:
    """Scoreboard for one segment, using /reality's interval maths so the two
    pages hedge their claims identically.

    One real difference: this tally has recorded sumsq since its first trade,
    so reality.ci() returns an EXACT interval here rather than the lower bound
    it must fall back to for S2's older totals.
    """
    import reality
    store = load() if store is None else store
    b = (store.get("tally") or {}).get(segment) or {}
    row = reality.row(b, segment, {segment: (segment, segment)})
    row.update({k: b.get(k, 0) for k in OUTCOMES})
    tp, sl = b.get("tp", 0), b.get("sl", 0)
    # Expired trades are excluded on purpose: they never hit either level, so
    # counting them as misses would answer a different question than "when it
    # resolved, which way did it resolve".
    row["hit_rate"] = 100.0 * tp / (tp + sl) if (tp + sl) else None
    row["verdict_zh"] = VERDICT_ZH.get(row.get("tag"), row.get("verdict") or "")
    return row


def web_view(store: dict = None) -> dict:
    """Everything /s4 needs to show the record."""
    store = load() if store is None else store
    closed = sorted(store.get("closed") or [],
                    key=lambda c: c.get("exit_ts") or 0, reverse=True)
    segs = [s for s in ("all", "crypto", "tradfi") + SIDE_KEYS
            if (store.get("tally") or {}).get(s)]
    return {
        "tracked": len(store.get("open") or {}) + len(closed),
        "open": sorted((store.get("open") or {}).values(),
                       key=lambda t: t.get("fired_ts") or 0, reverse=True),
        "closed": closed[:60],
        "stats": {s: stats(store, s) for s in segs},
        "track_hours": TRACK_HOURS,
        "tie_rule": "a candle spanning both stop and target is scored as a stop",
    }
