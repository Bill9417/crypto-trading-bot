"""
S3 daily circuit breaker — cap the worst day on the live Bybit engine.

Each S3 stop-out costs ~37.5% of the margin in play (1.5% stop at 50x); a
whipsaw day can chain them. Before every NEW entry the scanner asks
entry_blocked(): if the last 24h of REAL Bybit closed-P&L shows too many
losing trades (STRATEGY3_MAX_DAILY_STOPS) or too deep a net loss
(STRATEGY3_MAX_DAILY_LOSS_USDT), a halt file is written, one 🛑 Telegram
alert goes out, and every further entry is refused until an admin sends
/resume (or /halt to trip it manually).

Only trades on the symbols S3 ITSELF trades (STRATEGY3_SYMBOLS) are counted
— the sub-account also sees the user's MANUAL trading on other symbols, and
on 2026-07-14 two tiny manual ETH losses (−1.8 USDT total, on a +24 USDT
day) tripped a halt meant for ~22-USDT gold stop-outs. Manual trades on the
SAME symbol S3 trades still count — closed-P&L rows carry no author. Exits are NEVER blocked — the
breaker only stops NEW risk, an open position still closes on its signal.

Fail-open by design: if the Bybit P&L endpoint errors, entries proceed —
the per-trade emergency stop still protects each position, and blocking a
live engine on an API blip would be the worse failure mode.
Kill switch: set both limits to 0.
"""
import json
import os
import time

MAX_DAILY_STOPS = int(os.getenv("STRATEGY3_MAX_DAILY_STOPS", "2"))
MAX_DAILY_LOSS_USDT = float(os.getenv("STRATEGY3_MAX_DAILY_LOSS_USDT", "45"))
WINDOW_H = 24
HALT_FILE = os.path.join(os.path.dirname(__file__), "s3_halt.json")
# The breaker judges S3's own trading only — the same env list the scanner
# trades (bases, e.g. {"XAUT"}; closed-P&L rows print symbols like "XAUTUSDT").
S3_BASES = {s.strip().upper() for s in
            os.getenv("STRATEGY3_SYMBOLS", "XAUT").split(",") if s.strip()}


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def losses_in_window(trades: list, now_ms: int, window_h: float = WINDOW_H,
                     bases: set = None) -> tuple:
    """(losing-POSITION count, net P&L) over the trailing window.
    trades: [{"pnl": float, "time": ms, "symbol": "XAUTUSDT", "entry": str,
    "side": str}, ...] — closed_pnl_history shape. bases (e.g. {"XAUT"}) limits
    the count to those symbols; None counts everything (the pure-math tests).

    Rows are grouped into POSITIONS before counting. Bybit emits one closed-P&L
    row per closing FILL, not per position, and each partial close repeats that
    position's average entry — so a single trade closed in chunks arrives as
    several rows. On 2026-08-03 one 0.371 XAUT short (entry 4041.8, −3.68 USDT
    all-in) was closed in four pieces and read as "4 筆虧損平倉", tripping a
    limit of 2 on what was one small losing trade; the −45 USDT net limit, the
    one that measures actual damage, was nowhere near.

    A group counts as ONE loss only when the position lost money OVERALL, so
    scaling out of a winner through one red chunk no longer registers.

    Rows without an entry price keep their old one-row-per-trade behaviour
    (each becomes its own group). Two distinct positions sharing a symbol, side
    AND exact average entry would merge — vanishingly unlikely, and it errs
    toward allowing entries, which matches this module's fail-open stance."""
    cutoff = now_ms - window_h * 3600 * 1000
    groups: dict = {}
    net = 0.0
    for i, t in enumerate(trades):
        if (t.get("time") or 0) < cutoff:
            continue
        if bases is not None and \
                (t.get("symbol") or "").upper().replace("USDT", "") not in bases:
            continue
        pnl = t.get("pnl") or 0.0
        net += pnl
        entry = t.get("entry")
        key = (t.get("symbol"), t.get("side"), str(entry)) if entry else ("row", i)
        groups[key] = groups.get(key, 0.0) + pnl
    n_loss = sum(1 for total in groups.values() if total < 0)
    return n_loss, net


def breach(n_losses: int, net: float,
           max_stops: int = None, max_loss: float = None) -> str:
    max_stops = MAX_DAILY_STOPS if max_stops is None else max_stops
    max_loss = MAX_DAILY_LOSS_USDT if max_loss is None else max_loss
    if max_stops > 0 and n_losses >= max_stops:
        return f"{n_losses} 筆虧損平倉 / 24h (上限 {max_stops})"
    if max_loss > 0 and net <= -max_loss:
        return f"24h 淨虧損 {net:+.1f} USDT (上限 -{max_loss:g})"
    return ""


# ── halt file ────────────────────────────────────────────────────────────────
def halted() -> dict | None:
    try:
        with open(HALT_FILE, "r", encoding="utf-8") as f:
            h = json.load(f)
        return h if h.get("halted") else None
    except Exception:  # noqa: BLE001 — no/corrupt file = not halted
        return None


def set_halt(reason: str) -> None:
    tmp = HALT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"halted": True, "reason": reason, "ts": time.time()}, f,
                  ensure_ascii=False)
    os.replace(tmp, HALT_FILE)


def clear_halt() -> bool:
    """Returns True if a halt existed and was cleared."""
    existed = halted() is not None
    try:
        os.remove(HALT_FILE)
    except FileNotFoundError:
        pass
    return existed


# ── the gate the scanner calls before every NEW entry ────────────────────────
def entry_blocked() -> str:
    """'' = entry may proceed; otherwise the human-readable halt reason.
    Only hits the Bybit API when an entry is actually being attempted (a few
    times a day at most), never per-sweep."""
    h = halted()
    if h:
        return h.get("reason") or "manual halt"
    if MAX_DAILY_STOPS <= 0 and MAX_DAILY_LOSS_USDT <= 0:
        return ""
    try:
        import strategy3_exec as X
        hist = X.closed_pnl_history(limit=30)
    except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
        print(f"[s3risk] closed-pnl check failed ({exc}) — allowing entry")
        return ""
    if not hist.get("ok"):
        print(f"[s3risk] closed-pnl unavailable ({hist.get('error')}) — allowing entry")
        return ""
    n_loss, net = losses_in_window(hist.get("trades") or [], int(time.time() * 1000),
                                   bases=S3_BASES)
    reason = breach(n_loss, net)
    if not reason:
        return ""
    set_halt(reason)
    try:
        import telegram_utils
        telegram_utils.send_message(
            f"🛑 S3 CIRCUIT BREAKER 觸發 — 今日停止新倉\n{reason}\n"
            f"已開的倉位照常由訊號管理，只擋新進場。\n"
            f"確認過後用 /resume 解除 (限管理員)。", force=True)
    except Exception as exc:  # noqa: BLE001 — the halt itself must still hold
        print(f"[s3risk] halt alert failed: {exc}")
    print(f"[s3risk] HALTED: {reason}")
    return reason
