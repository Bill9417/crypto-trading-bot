"""
Daily Report — one morning message per day → the Telegram "report" channel
(📈 Daily Report topic in the group).

Sent once per local (Asia/Taipei) calendar day, the first Strategy-2 sweep
after DAILY_REPORT_HOUR (default 08:00). One glance answers: what did both
live accounts do yesterday, what's open right now, and what does today look
like (BTC, Fear & Greed, high-impact US prints)?

All numbers come from the same ground-truth helpers the web pages use:
  • Binance — executor.account_snapshot() + realized_pnl_summary()
  • Bybit   — strategy3_exec.account_snapshot() + closed_pnl_summary()
  • Market  — market_intel btc_snapshot / fear_greed / econ_calendar

Every data source is optional: an API blip degrades that section to
"unavailable" instead of skipping the day's report. State (the last local
date a report was sent) persists in daily_report_state.json so a scanner
restart never double-sends.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import telegram_utils

STATE_FILE = os.path.join(os.path.dirname(__file__), "daily_report_state.json")

REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))   # local hour (0-23)
TZ = ZoneInfo("Asia/Taipei")


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = start fresh
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _due(state: dict, now: datetime) -> bool:
    """True when today's report hasn't been sent yet and the hour has come."""
    return now.hour >= REPORT_HOUR and state.get("last_report") != now.strftime("%Y-%m-%d")


# ── formatting (pure given `data` — unit-testable without network) ──────────
def _n(v, digits=2):
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _pnl(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"{v:+,.2f}"


def _acct_section(icon: str, name: str, snap: dict, pnl: dict) -> list:
    """One account block: balance line, realized-P&L line, open positions."""
    lines = [f"{icon} {name}"]
    bal = (snap or {}).get("balance") or {}
    total = bal.get("wallet") if bal.get("wallet") is not None else bal.get("equity")
    if total is None:
        lines.append("  balance unavailable" +
                     (f" ({snap.get('error')})" if (snap or {}).get("error") else ""))
    else:
        upnl = bal.get("unrealized_pnl")
        lines.append(f"  balance {_n(total)} USDT · avail {_n(bal.get('available'))}"
                     + (f" · uPnL {_pnl(upnl)}" if upnl not in (None, 0.0) else ""))
    daily = (pnl or {}).get("daily") or []
    if len(daily) >= 2:
        week = sum(d.get("net") or 0.0 for d in daily[-7:])
        lines.append(f"  P&L today {_pnl(daily[-1].get('net'))} · "
                     f"yesterday {_pnl(daily[-2].get('net'))} · 7d {_pnl(week)}")
    positions = (snap or {}).get("positions") or []
    if positions:
        for p in positions[:8]:
            base = (p.get("symbol") or "?").split("/")[0]
            pct = p.get("pnl_pct")
            lines.append(f"  ▸ {base} {p.get('side')} {_pnl(p.get('unrealized_pnl'))}"
                         + (f" ({_pnl(pct)}%)" if pct is not None else ""))
    else:
        lines.append("  no open positions")
    return lines


def _today_events(events: list, now: datetime) -> list:
    """Today's high-impact US prints, local (HH:MM) times, chronological."""
    import market_intel
    today = now.strftime("%Y-%m-%d")
    rows = []
    for ev in events or []:
        ts = market_intel._pub_ts({"published": ev.get("date")})
        if not ts:
            continue
        local = datetime.fromtimestamp(ts, TZ)
        if local.strftime("%Y-%m-%d") != today:
            continue
        extra = f" (forecast {ev['forecast']})" if ev.get("forecast") else ""
        rows.append((ts, f"  • {local.strftime('%H:%M')} {ev.get('title')}{extra}"))
    return [r[1] for r in sorted(rows)]


def build_report(data: dict, now: datetime) -> str:
    lines = [f"📈 DAILY REPORT · {now.strftime('%a %Y-%m-%d')}", ""]
    lines += _acct_section("🟨", "Binance (S1/S2)",
                           data.get("binance_snap"), data.get("binance_pnl"))
    lines.append("")
    lines += _acct_section("🟧", "Bybit (S3)",
                           data.get("bybit_snap"), data.get("bybit_pnl"))

    market_bits = []
    btc = data.get("btc") or {}
    if btc.get("price"):
        market_bits.append(f"BTC {_n(btc['price'], 0)} ({_pnl(btc.get('change_pct'))}% 24h)")
    fng = data.get("fng") or {}
    if fng.get("value") is not None:
        market_bits.append(f"Fear&Greed {fng['value']} ({fng.get('label')})")
    if market_bits:
        lines += ["", "🌡 Market", "  " + " · ".join(market_bits)]

    cal = _today_events(data.get("calendar") or [], now)
    lines += ["", "🗓 Today (high-impact US)"]
    lines += cal if cal else ["  none — quiet macro day"]
    return "\n".join(lines)


# ── data gathering (each piece optional) ─────────────────────────────────────
def _gather() -> dict:
    import executor
    import market_intel
    import strategy3_exec

    data = {}
    for key, fn in (
        ("binance_snap", executor.account_snapshot),
        ("binance_pnl", executor.realized_pnl_summary),
        ("bybit_snap", strategy3_exec.account_snapshot),
        ("bybit_pnl", strategy3_exec.closed_pnl_summary),
        ("btc", market_intel.btc_snapshot),
        ("fng", market_intel.fear_greed),
    ):
        try:
            data[key] = fn()
        except Exception as exc:  # noqa: BLE001 — one dead source ≠ no report
            print(f"[report] {key} unavailable: {exc}")
            data[key] = None
    try:
        data["calendar"] = market_intel.econ_calendar().get("events") or []
    except Exception as exc:  # noqa: BLE001
        print(f"[report] calendar unavailable: {exc}")
        data["calendar"] = []
    return data


# ── orchestrator (called once per scanner sweep) ─────────────────────────────
def tick() -> bool:
    """Send today's report if due; returns True only when one was sent."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _due(state, now):
        return False
    msg = build_report(_gather(), now)
    ok = telegram_utils.send_message(msg, force=True, channel="report")
    if ok:
        # Only mark done on a confirmed send — a Telegram blip retries next sweep.
        state["last_report"] = now.strftime("%Y-%m-%d")
        state["last_sent_ts"] = time.time()
        _save_state(state)
        print(f"[report] daily report sent for {state['last_report']}")
    return ok
