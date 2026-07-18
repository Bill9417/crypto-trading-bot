"""
Signal outcome tracker — did the alerts actually work? Measured, not felt.

Every fired S2 signal carries an Entry/SL/TP plan, but nothing ever checked
what happened next. This module replays each signal ≥48h old against the
real candles that followed (pessimistic: SL checked before TP inside the
same candle) and records the outcome:

    tp2      final target hit before the stop
    tp1      first target hit, window ended before tp2/sl
    tp1→sl   first target hit, then stopped out (partial winner)
    sl       stopped out first
    none     nothing hit inside the 48h window

A weekly scorecard (Sunday evening) goes to the Signals topic, and /outcomes
answers on demand. This is the honesty loop: if the scorecard says the
high-conviction alerts stop out more than they pay, believe it.

2026-07-16 fix: strategy2_signals.json only RETAINS signals for 24h (it is
the /strategy2 page feed), but evaluation waits 48h — so nothing was EVER
evaluated (signal_outcomes.json didn't exist after 3 days live). Every tick
now SNAPSHOTS new signals into this module's own state first; evaluation
reads the snapshot, not the page feed, so page retention can't starve it.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

STATE_FILE = os.path.join(os.path.dirname(__file__), "signal_outcomes.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")
TZ = ZoneInfo("Asia/Taipei")

WINDOW_H = 48                 # evaluation window after the signal
MIN_AGE_H = 48                # evaluate only once the window is complete
MAX_AGE_D = 10                # too old to fetch candles for — skip
EVAL_PER_TICK = 3             # API-friendly trickle
RETAIN_D = 30
WEEKLY_HOUR = 20              # Sunday ≥20:00 台北


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── pure evaluation (unit-tested) ────────────────────────────────────────────
def evaluate(sig: dict, candles: list, window_h: float = WINDOW_H):
    """Outcome of one signal against the candles that FOLLOWED it.
    candles: [(ts_ms, o, h, l, c, v), ...]. Pessimistic: within a candle the
    stop is assumed to hit before any target. None = not resolvable (no plan
    or no candles)."""
    sl, tp1, tp2 = sig.get("sl"), sig.get("tp1"), sig.get("tp2")
    ts_ms = (sig.get("ts") or 0) * 1000
    if not (sl and tp1 and tp2 and ts_ms):
        return None
    end_ms = ts_ms + window_h * 3600 * 1000
    is_long = sig.get("direction") == "long"
    tp1_hit = False
    for (t, o, h, l, c, v) in candles:
        if t <= ts_ms:
            continue
        if t >= end_ms:
            break
        stop = l <= sl if is_long else h >= sl
        full = h >= tp2 if is_long else l <= tp2
        part = h >= tp1 if is_long else l <= tp1
        if stop:
            return {"outcome": "tp1→sl" if tp1_hit else "sl",
                    "hours": round((t - ts_ms) / 3.6e6, 1)}
        if full:
            return {"outcome": "tp2", "hours": round((t - ts_ms) / 3.6e6, 1)}
        if part:
            tp1_hit = True
    return {"outcome": "tp1" if tp1_hit else "none", "hours": None}


def summarize(outcomes: list, title: str = "📋 訊號成績單 (7天)") -> str:
    """outcomes: [{"outcome", "direction", "score", "base", ...}, ...]"""
    if not outcomes:
        return f"{title}\n尚無已評估的訊號 — 訊號滿48小時後才會結算。"
    import tg_format
    counts: dict = {}
    for o in outcomes:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    n = len(outcomes)
    wins = counts.get("tp2", 0)
    partial = counts.get("tp1", 0) + counts.get("tp1→sl", 0)
    stops = counts.get("sl", 0) + counts.get("tp1→sl", 0)
    rows = [
        ("🎯 到 TP2", wins, f"{100 * wins / n:.0f}%"),
        ("◐ 碰到 TP1", partial, f"{100 * partial / n:.0f}%"),
        ("⚠️ 停損", stops, f"{100 * stops / n:.0f}%"),
        ("➖ 都沒碰到", counts.get("none", 0), ""),
    ]
    hc = [o for o in outcomes if o.get("hc")]
    if hc:
        hw = sum(1 for o in hc if o["outcome"] == "tp2")
        rows.append(("高信心→TP2", f"{hw}/{len(hc)}", f"{100 * hw / len(hc):.0f}%"))
    prem = [o for o in outcomes if o.get("premium")]
    if prem:
        pw = sum(1 for o in prem
                 if o["outcome"] in ("tp2", "tp1", "tp1→sl"))
        rows.append(("⭐ 精選→TP1", f"{pw}/{len(prem)}",
                     f"{100 * pw / len(prem):.0f}%"))
    lines = [title,
             f"共 {n} 個訊號（進場=訊號價 · 48h 窗口 · 同根K線先算停損）",
             tg_format.pre_table(rows, align="lrr")]
    if n < 20:
        lines.append(f"（樣本只有 {n} 個 — 先當參考，別當結論）")
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────
_SNAP_KEYS = ("symbol", "base", "direction", "score", "ts",
              "entry", "sl", "tp1", "tp2", "premium")


def _snapshot(state: dict, signals: list, now: float) -> None:
    """Copy new signals into our OWN state so the page feed's 24h retention
    can never starve the 48h evaluation window. Prunes snapshots that are
    already evaluated or too old to ever evaluate."""
    snaps = state.setdefault("signals", {})
    evaluated = state.get("evaluated") or {}
    for s in signals:
        ts = s.get("ts") or 0
        key = f"{s.get('symbol')}:{int(ts)}"
        if key not in snaps and key not in evaluated and s.get("sl"):
            snaps[key] = {k: s.get(k) for k in _SNAP_KEYS if s.get(k) is not None}
    cutoff = now - MAX_AGE_D * 86400
    state["signals"] = {k: v for k, v in snaps.items()
                        if k not in evaluated and (v.get("ts") or 0) >= cutoff}


def _pending(snaps: dict, evaluated: dict, now: float) -> list:
    out = []
    for key, s in snaps.items():
        if key in evaluated or not s.get("sl"):
            continue
        age_h = (now - (s.get("ts") or 0)) / 3600
        if MIN_AGE_H <= age_h <= MAX_AGE_D * 24:
            out.append((key, s))
    return out


def tick(client) -> int:
    """Evaluate a few due signals per sweep; send the Sunday scorecard."""
    now = time.time()
    state = _load_state()
    evaluated = state.setdefault("evaluated", {})
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            signals = (json.load(f) or {}).get("signals") or []
    except Exception:  # noqa: BLE001 — no signal file yet
        signals = []
    before = set(state.get("signals") or {})
    _snapshot(state, signals, now)
    snapped = set(state["signals"]) != before   # new/pruned snaps must persist

    done = 0
    for key, sig in _pending(state["signals"], evaluated, now)[:EVAL_PER_TICK]:
        try:
            candles = client.call("fetch_ohlcv", sig["symbol"], "15m",
                                  int(sig["ts"] * 1000), 250)
        except Exception:  # noqa: BLE001 — retry on a later sweep
            continue
        res = evaluate(sig, candles or [])
        if res is None:
            res = {"outcome": "none", "hours": None}
        import config
        hc = ((sig.get("direction") == "long"
               and (sig.get("score") or 0) >= config.STRATEGY2_LIVE_MIN_SCORE)
              or (sig.get("direction") == "short"
                  and (sig.get("score") or 100) <= 100 - config.STRATEGY2_LIVE_MIN_SCORE))
        evaluated[key] = {**res, "base": sig.get("base"), "score": sig.get("score"),
                          "direction": sig.get("direction"), "hc": hc,
                          "premium": bool(sig.get("premium")),
                          "sig_ts": sig.get("ts"), "eval_ts": now}
        done += 1
        print(f"[outcomes] {sig.get('base')} {sig.get('direction')} → {res['outcome']}")

    cutoff = now - RETAIN_D * 86400
    state["evaluated"] = {k: v for k, v in evaluated.items()
                          if (v.get("sig_ts") or 0) >= cutoff}

    local = datetime.now(TZ)
    today = local.strftime("%Y-%m-%d")
    if (local.weekday() == 6 and local.hour >= WEEKLY_HOUR
            and state.get("last_weekly") != today):
        state["last_weekly"] = today
        recent = [v for v in state["evaluated"].values()
                  if (v.get("sig_ts") or 0) >= now - 7 * 86400]
        import telegram_utils
        telegram_utils.send_message(summarize(recent), parse_mode="HTML",
                                    force=True, channel="signals")
        print(f"[outcomes] weekly scorecard sent ({len(recent)} signals)")

    if done or snapped or state.get("last_weekly") == today:
        _save_state(state)
    return done


def report() -> str:
    """/outcomes — everything evaluated in the last 7 days, on demand."""
    state = _load_state()
    now = time.time()
    recent = [v for v in (state.get("evaluated") or {}).values()
              if (v.get("sig_ts") or 0) >= now - 7 * 86400]
    return summarize(recent, title="📋 訊號成績單 (最近7天)")
