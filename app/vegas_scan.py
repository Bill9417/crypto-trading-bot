"""
🌊 The hourly sweep behind 隧道翻多 — see vegas_reclaim for what it detects and
what it is measured to be worth.

RUNS HOURLY, NOT EVERY SWEEP, and that is a property of the signal rather than
a budget compromise. Every gate is a comparison against a CLOSED 1h bar, so
between two bar closes the answer cannot change; scanning at the S2 cadence
would ask the same question twelve times and spend 3,600 API calls to get the
same answer. It runs a few minutes past the hour so the bar it needs has
actually closed.

THE UNIVERSE IS CAPPED AND THE CAP IS REPORTED. MAX_SYMBOLS most-liquid perps,
because 300 extra fetches an hour is real load on a client that has already
been rate-limited into cooldowns this month. A capped scan that says
"nothing fired" without saying what it looked at is indistinguishable from a
complete one, and this repo has already lost three days to exactly that
ambiguity — so `scanned` and `universe` both ship in the state file and both
reach the card.
"""
import json
import os
import time

import vegas_reclaim as V
import vegas_outcomes as O

STATE_FILE = os.path.join(os.path.dirname(__file__), "vegas_state.json")
RUN_EVERY_SEC = float(os.getenv("VEGAS_RUN_EVERY_SEC", "3600"))
MAX_SYMBOLS = int(os.getenv("VEGAS_MAX_SYMBOLS", "180"))
# DERIVED, not a literal — same reason strategy4 and strategy2_scanner derive
# theirs. consider() needs WARMUP CLOSED bars and drops the forming candle, so
# the floor is WARMUP + 1; raising VEGAS_TREND_EMA raises WARMUP, and a fixed
# 320 would silently start returning "K 棒不足" for every symbol while the log
# kept printing "0 fired" exactly as it does on a quiet hour.
CANDLES = max(int(os.getenv("VEGAS_CANDLES", "320")), V.WARMUP + 20)
PACE_SEC = float(os.getenv("VEGAS_SCAN_PACE_SEC", "0.12"))
RETAIN_HOURS = float(os.getenv("VEGAS_RETAIN_HOURS", "36"))
MAX_KEEP = 60


def load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = start clean
        return {}


def save(state: dict) -> None:
    try:
        tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — a scanner must not break the loop
        print(f"[vegas] save failed: {exc}")


def scan(client, symbols: list, state: dict = None, now: float = None,
         max_symbols: int = None, on_fire=None) -> dict:
    """One pass. Returns what fired AND what it looked at."""
    state = load() if state is None else state
    now = now if now is not None else time.time()
    cap = MAX_SYMBOLS if max_symbols is None else max_symbols
    universe = len(symbols or [])
    syms = list(symbols or [])[:cap] if cap > 0 else list(symbols or [])
    fired, checked, errors = [], 0, 0
    # A FUNNEL, not a first-failure tally. The first gate is a cross EVENT, so
    # on a normal hour every symbol fails it and a first-failure breakdown
    # reads "80x 沒有站回隧道上方" — which is exactly what a scanner returning
    # a hardcoded False would also print. Counting how many symbols clear each
    # gate in turn distinguishes a quiet market from a broken pipeline, which
    # is the entire reason this breakdown exists.
    funnel = {"reclaim": 0, "turn": 0, "buyer": 0, "surge": 0, "atr": 0}
    for sym in syms:
        try:
            rows = client.call("fetch_ohlcv", sym, V.TIMEFRAME, None, CANDLES)
        except Exception:  # noqa: BLE001 — one dead symbol is not a failed scan
            errors += 1
            continue
        finally:
            time.sleep(PACE_SEC)
        checked += 1
        sig = V.consider(sym, rows, state, now)
        if sig:
            for g in funnel:
                funnel[g] += 1
            fired.append(sig)
            if on_fire:
                try:
                    on_fire(sig)
                except Exception as exc:  # noqa: BLE001
                    print(f"[vegas] on_fire {sym}: {exc}")
            continue
        closed = (rows or [])[:-1]
        r = V.read(closed) if len(closed) >= V.WARMUP else {}
        if not r:
            continue
        if r["reclaim"]:
            funnel["reclaim"] += 1
            if r["turn"]:
                funnel["turn"] += 1
                if r["buyer_bar"]:
                    funnel["buyer"] += 1
                    if r["surge"]:
                        funnel["surge"] += 1

    recent = [s for s in (state.get("recent") or [])
              if now - (s.get("fired_ts") or 0) <= RETAIN_HOURS * 3600]
    recent = fired + recent
    state["recent"] = recent[:MAX_KEEP]
    state["ran_ts"] = now
    state["scanned"] = checked
    state["universe"] = universe
    state["errors"] = errors
    state["funnel"] = funnel
    return {"fired": len(fired), "checked": checked, "universe": universe,
            "errors": errors, "signals": fired, "funnel": funnel}


def is_due(now: float = None) -> bool:
    """Whether a pass is owed. Exposed so the caller can avoid BUILDING the
    universe (a fetch_tickers call) on the eleven sweeps an hour that would
    only discard it."""
    now = now if now is not None else time.time()
    return now - (load().get("ran_ts") or 0) >= RUN_EVERY_SEC


def tick(client, symbols: list, now: float = None, force: bool = False) -> dict:
    """Called from the S2 sweep. Self-paces to RUN_EVERY_SEC."""
    now = now if now is not None else time.time()
    state = load()
    if not force and now - (state.get("ran_ts") or 0) < RUN_EVERY_SEC:
        return {"skipped": "not due"}
    result = scan(client, symbols, state, now, on_fire=O.note)
    save(state)
    return result


def web_view(state: dict = None, limit: int = 20) -> dict:
    """The card's payload: what fired, plus what the scan actually covered."""
    state = load() if state is None else state
    seen, rows = set(), []
    for r in sorted(state.get("recent") or [],
                    key=lambda x: -(x.get("fired_ts") or 0)):
        s = r.get("symbol")
        if s and s not in seen:
            seen.add(s)
            rows.append(r)
    # ONLY the record half of the outcome view is merged. Spreading all of
    # O.web_view() here overwrote `recent` with the outcome book's own list —
    # which holds nothing until a signal has aged 48h — so a scan with twelve
    # live signals rendered an empty card that read "過去 36 小時沒有符合的幣".
    # A silent key collision between two dicts that both mean "recent" is not
    # visible at either call site, so the fields are named explicitly instead.
    ov = O.web_view(limit=limit)
    return {
        # The DISPLAY list is the scan's, because it is the one that holds a
        # signal from the moment it fires.
        "recent": rows[:limit],
        # Signals whose 48h horizon has not elapsed. Shown as live too — the
        # card concatenates them and dedupes by symbol.
        "open": ov.get("open") or [],
        "ran_ts": state.get("ran_ts") or 0,
        # Both numbers, always. `scanned` alone reads as the whole market.
        "scanned": state.get("scanned"),
        "universe": state.get("universe"),
        "errors": state.get("errors") or 0,
        "funnel": state.get("funnel") or {},
        "retain_hours": RETAIN_HOURS,
        "run_every_sec": RUN_EVERY_SEC,
        "live": ov.get("live") or {},
        "measured": ov.get("measured") or {},
        "params": ov.get("params") or {},
        "horizons": ov.get("horizons") or list(O.HORIZONS),
    }
