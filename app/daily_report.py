"""
Daily Report — one morning message per day → the OWNER'S PRIVATE CHAT with
the bot (channel="private"). It carries real account balances and P&L, so
since 2026-07-16 it deliberately does NOT go to the group's 📈 Daily Report
topic — group members should never see the owner's account numbers. The
/report command is owner-only for the same reason (see tg_commands).

Sent once per local (Asia/Taipei) calendar day, the first Strategy-2 sweep
after DAILY_REPORT_HOUR (default 08:00). One glance answers: what did both
live accounts do yesterday, what's open right now, and what does today look
like (BTC, Fear & Greed, today's and this week's high-impact US prints,
and what broke overnight)?

All numbers come from the same ground-truth helpers the web pages use:
  • Binance — executor.account_snapshot() + realized_pnl_summary()
  • Bybit   — strategy3_exec.account_snapshot() + closed_pnl_summary()
  • Market  — market_intel btc_snapshot / fear_greed
  • 大事件  — macro_events (US macro calendar) + event_radar (last 24h)

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
    """One account block: an ALIGNED <pre> table (balance / P&L / positions).
    The report is sent with parse_mode='HTML' so the columns actually line up."""
    import tg_format
    lines = [f"{icon} {name}"]
    rows = []
    bal = (snap or {}).get("balance") or {}
    total = bal.get("wallet") if bal.get("wallet") is not None else bal.get("equity")
    if total is None:
        lines.append("餘額暫時無法取得" +
                     (f"（{tg_format.esc(snap.get('error'))}）"
                      if (snap or {}).get("error") else ""))
        return lines
    upnl = bal.get("unrealized_pnl")
    rows.append(("餘額", f"{_n(total)} USDT", f"可用 {_n(bal.get('available'))}",
                 f"未實現 {_pnl(upnl)}" if upnl not in (None, 0.0) else ""))
    daily = (pnl or {}).get("daily") or []
    if len(daily) >= 2:
        week = sum(d.get("net") or 0.0 for d in daily[-7:])
        rows.append(("損益", f"今日 {_pnl(daily[-1].get('net'))}",
                     f"昨日 {_pnl(daily[-2].get('net'))}", f"7日 {_pnl(week)}"))
    positions = (snap or {}).get("positions") or []
    for p in positions[:8]:
        base = (p.get("symbol") or "?").split("/")[0]
        pct = p.get("pnl_pct")
        eng = f"（{p['engine']}）" if p.get("engine") else ""
        rows.append((f"▸ {base}{eng}", tg_format.dir_zh(p.get("side"), arrow=False),
                     _pnl(p.get("unrealized_pnl")),
                     f"{_pnl(pct)}%" if pct is not None else ""))
    # Positions the bot does NOT manage (opened by hand) still belong to the
    # owner, and their P&L is already inside the 未實現 figure above — listing
    # only bot positions made the report contradict its own balance line.
    foreign = (snap or {}).get("foreign") or []
    for p in foreign[:6]:
        base = (p.get("symbol") or "?").split("/")[0]
        pct = p.get("pnl_pct")
        naked = " ⚠️無停損" if p.get("sl") is None else ""
        rows.append((f"▸ {base}（手動）{naked}",
                     tg_format.dir_zh(p.get("side"), arrow=False),
                     _pnl(p.get("unrealized_pnl")),
                     f"{_pnl(pct)}%" if pct is not None else ""))
    if not positions and not foreign:
        rows.append(("持倉", "無", "", ""))
    elif not positions:
        rows.append(("機器人持倉", "無", "", ""))
    lines.append(tg_format.pre_table(rows, align="llll"))
    return lines


def _big_events(rows: list, now: datetime) -> list:
    """The last 24h of event-radar alerts (Fed / war / regulation / hack /
    whale / price shock), compact. These already went out one-by-one as they
    broke; the point here is the morning re-read — "what moved while I slept"
    next to the day's account numbers, in one place."""
    import tg_format
    out = []
    for cat, headline in rows or []:
        out.append(f"  {cat} {tg_format.esc(headline[:96])}"
                   if cat else f"  • {tg_format.esc(headline[:96])}")
    return out


_WD = "一二三四五六日"


def build_report(data: dict, now: datetime) -> str:
    lines = [f"📈 每日報告 · {now.strftime('%Y-%m-%d')}（週{_WD[now.weekday()]}）", ""]
    lines += _acct_section("🟨", "Binance · S1/S2",
                           data.get("binance_snap"), data.get("binance_pnl"))
    lines.append("")
    lines += _acct_section("🟧", "Bybit · S3",
                           data.get("bybit_snap"), data.get("bybit_pnl"))

    market_bits = []
    btc = data.get("btc") or {}
    if btc.get("price"):
        market_bits.append(f"BTC {_n(btc['price'], 0)}（{_pnl(btc.get('change_pct'))}% 24h）")
    fng = data.get("fng") or {}
    if fng.get("value") is not None:
        import tg_format
        market_bits.append(f"貪婪指數 {fng['value']}（{tg_format.esc(fng.get('label'))}）")
    if market_bits:
        lines += ["", "🌡 市場", "  " + " · ".join(market_bits)]

    # 🗓 was "無 — 平靜的總經日" EVERY day because its only feed had been
    # answering 429; macro_events reads three sources and says so when it
    # cannot read any, instead of asserting a quiet week.
    import macro_events
    lines += ["", "🗓 今日 · 美國高影響數據"]
    lines += macro_events.today_lines(now, TZ)
    week = macro_events.lines(now, TZ, days=7, limit=6)
    if week:
        lines += ["", "📅 本週大事 — 別在這些時間點抱滿倉"]
        lines += week

    events = _big_events(data.get("events") or [], now)
    if events:
        lines += ["", "🌍 過去 24h 重大事件"]
        lines += events

    # Monday: each live engine must justify its slot with REAL numbers.
    if now.weekday() == 0:
        lines += ["", "🧪 引擎週檢 — 策略要持續賺到它的位置"]
        lines += _engine_verdict("Binance S1/S2", data.get("binance_pnl"))
        lines += _engine_verdict("Bybit S3", data.get("bybit_pnl"))
    return "\n".join(lines)


def _engine_verdict(name: str, s: dict) -> list:
    """One engine's weekly verdict from its real closed-trade summary."""
    if not (s or {}).get("ok") or not s.get("n_trades"):
        return [f"  {name}: 無成交紀錄"]
    pf = s.get("profit_factor")
    week = sum(d.get("net") or 0.0 for d in (s.get("daily") or [])[-7:])
    line = (f"  {name}: {s['n_trades']} 筆 · PF "
            f"{pf if pf is not None else '—'} · 7d {_pnl(week)} USDT")
    if s["n_trades"] >= 10 and pf is not None and pf < 1.0:
        line += "\n    ⚠️ 沒有支付它的風險 (PF<1) — 考慮暫停或縮小"
    elif pf is not None and pf >= 1.2:
        line += "\n    ✅ 有在賺它的位置"
    return [line]


# ── balance history (the real equity curve) ──────────────────────────────────
BALANCE_FILE = os.path.join(os.path.dirname(__file__), "balance_history.json")


def record_balance(data: dict, now: datetime) -> bool:
    """One row per day: both venues' real account totals. This file IS the
    equity curve — trades lie (phantom records), balances don't."""
    def _total(snap):
        try:
            return round(float(((snap or {}).get("balance") or {}).get("total")), 2)
        except (TypeError, ValueError):
            return None

    b, y = _total(data.get("binance_snap")), _total(data.get("bybit_snap"))
    if b is None and y is None:
        return False
    try:
        with open(BALANCE_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f) or []
    except Exception:  # noqa: BLE001 — first run
        hist = []
    today = now.strftime("%Y-%m-%d")
    hist = [r for r in hist if r.get("date") != today][-399:]
    hist.append({"date": today, "binance": b, "bybit": y,
                 "total": round((b or 0) + (y or 0), 2)})
    tmp = BALANCE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f)
    os.replace(tmp, BALANCE_FILE)
    return True


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
    # The calendar is read inside build_report via macro_events (three
    # sources, disk-cached), so it is no longer gathered here.
    try:
        import event_radar
        data["events"] = event_radar.recent_digest(hours=24.0, limit=5)
    except Exception as exc:  # noqa: BLE001 — one dead source ≠ no report
        print(f"[report] event radar unavailable: {exc}")
        data["events"] = []
    return data


# ── orchestrator (called once per scanner sweep) ─────────────────────────────
def tick() -> bool:
    """Send today's report if due; returns True only when one was sent."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _due(state, now):
        return False
    data = _gather()
    msg = build_report(data, now)
    try:
        record_balance(data, now)             # daily equity-curve point
    except Exception as exc:  # noqa: BLE001 — history must never block the report
        print(f"[report] balance record failed: {exc}")
    ok = telegram_utils.send_message(msg, parse_mode="HTML", force=True,
                                     channel="private")
    if ok:
        # Only mark done on a confirmed send — a Telegram blip retries next sweep.
        state["last_report"] = now.strftime("%Y-%m-%d")
        state["last_sent_ts"] = time.time()
        _save_state(state)
        print(f"[report] daily report sent for {state['last_report']}")
    return ok
