"""🛑 Account-wide daily loss limit — one brake shared by every engine.

S3 already has a circuit breaker (strategy3_risk), but it counts only
STRATEGY3_SYMBOLS. Nothing stopped the S1 mirror or the copy engine from
opening new positions on a day the ACCOUNT was bleeding — and they all settle
into the same Bybit sub-account, so a bad day compounds across three engines
that cannot see each other.

This is deliberately the crudest possible rule, because a risk brake that
nobody understands is one nobody trusts: sum today's REALISED P&L on the
account, and once it is worse than -MAX_DAILY_LOSS_USDT, block NEW entries for
the rest of the local day. It never closes anything — an open position keeps
its own stop, and yanking positions on a threshold is how a bad day becomes a
worse one.

WHAT IT DOES NOT DO, on purpose:
  · never touches manual positions or manual trading
  · never cancels or closes; entries only
  · resets by the calendar, not by a timer — "today" is 台北 local, matching
    every other daily boundary in this project

The limit reads the same closed-P&L feed /winrate does, so it is measuring the
exchange's own record rather than a local tally that can drift from it.

OFF by default (MAX_DAILY_LOSS_USDT=0). It gates entries, so a
misconfiguration is a silent no-trade day — that has to be opt-in.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
STATE_FILE = os.path.join(os.path.dirname(__file__), "daily_risk_state.json")

# 0 disables the brake entirely (the default).
MAX_DAILY_LOSS_USDT = float(os.getenv("MAX_DAILY_LOSS_USDT", "0") or 0)
CACHE_SEC = 120          # entries are rare; don't re-poll the exchange per call


_cache = {"ts": 0.0, "pnl": None, "day": ""}


def today_str(now=None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y-%m-%d")


def enabled() -> bool:
    # abs(): someone writing MAX_DAILY_LOSS_USDT=-10 plainly means "10 USDT of
    # loss". Reading that as "disabled" would silently disarm a risk brake the
    # operator explicitly asked for — the worst possible way to be pedantic.
    return abs(MAX_DAILY_LOSS_USDT) > 0


def day_bounds_ms(now=None):
    """(start_ms, end_ms) of the current 台北 day — the window 'today' means."""
    now = now or datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


def realised_today(trades: list, now=None) -> float:
    """Net realised P&L of `trades` closed inside the current 台北 day.

    `trades` is the shape strategy3_exec.closed_pnl_summary() produces —
    {'pnl', 'time' (ms)} — i.e. the exchange's own record, not a local tally.
    """
    start_ms, end_ms = day_bounds_ms(now)
    total = 0.0
    for t in trades or []:
        ts = t.get("time") or 0
        if start_ms <= ts <= end_ms:
            try:
                total += float(t.get("pnl") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(total, 4)


def breached(pnl: float, limit: float = None) -> bool:
    limit = MAX_DAILY_LOSS_USDT if limit is None else limit
    return bool(abs(limit) > 0 and pnl <= -abs(limit))


# ── persisted notice (so the alert fires once per day, not per entry) ────────
def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = nothing announced yet
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _announce_once(day: str, pnl: float) -> None:
    state = _load()
    if state.get("announced_day") == day:
        return
    state["announced_day"] = day
    state["pnl"] = pnl
    _save(state)
    try:
        import telegram_utils
        telegram_utils.send_message(
            f"🛑 今日虧損達上限 — 帳戶今日已實現 {pnl:+.2f} USDT，"
            f"超過設定的 {MAX_DAILY_LOSS_USDT:g} USDT。\n"
            f"今天不再開新倉（已持有的部位不動，停損照舊）。"
            f"明天 00:00（台北）自動恢復。",
            force=True)
    except Exception as exc:  # noqa: BLE001 — a notice must never block the brake
        print(f"[daily-risk] announce failed: {exc}")


def _todays_pnl() -> float:
    """Cached read of the exchange's realised P&L for today; 0.0 on failure.

    Failing OPEN is deliberate. This gate sits in front of every entry on three
    engines, so an exchange blip must not silently halt all trading — the
    per-engine stops and S3's own breaker are the protections that stay armed
    regardless.
    """
    day = today_str()
    if _cache["day"] == day and time.time() - _cache["ts"] < CACHE_SEC \
            and _cache["pnl"] is not None:
        return _cache["pnl"]
    pnl = 0.0
    try:
        import strategy3_exec as X
        summary = X.closed_pnl_summary(limit=400)
        if summary.get("ok"):
            pnl = realised_today(summary.get("trades") or [])
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        print(f"[daily-risk] pnl read failed (failing open): {exc}")
        return 0.0
    _cache.update(ts=time.time(), pnl=pnl, day=day)
    return pnl


def entry_blocked() -> str:
    """'' = a new entry may proceed; otherwise the reason, ready to display.

    Called by every engine before opening. Cheap: cached CACHE_SEC, and a
    complete no-op when the limit is unset.
    """
    if not enabled():
        return ""
    pnl = _todays_pnl()
    if not breached(pnl):
        return ""
    day = today_str()
    _announce_once(day, pnl)
    return (f"今日虧損 {pnl:+.2f} USDT 已達上限 "
            f"{MAX_DAILY_LOSS_USDT:g} USDT — 今天不再開新倉")


def status() -> dict:
    """Read-only snapshot for /health. Never raises."""
    try:
        pnl = _todays_pnl() if enabled() else 0.0
        return {"enabled": enabled(), "limit": MAX_DAILY_LOSS_USDT,
                "pnl_today": pnl, "blocked": bool(enabled() and breached(pnl)),
                "day": today_str()}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": enabled(), "limit": MAX_DAILY_LOSS_USDT,
                "pnl_today": None, "blocked": False, "error": str(exc)[:150]}
