"""
☀️ Public morning brief — one market snapshot per day into the group's
📈 Daily Report topic (channel="report").

That topic has been ORPHANED since 2026-07-16, when the owner's daily report
moved to a private DM (it carries real balances). This module gives the group
its daily heartbeat back with a PUBLIC-SAFE brief: market data only, never
account balances, positions or P&L. The steady morning cadence is what makes
the group feel alive to members — and it's the promo surface: prices, regime,
what macro prints land today, and how the signal engine actually scored.

Sections (each optional — a dead source degrades to absence, never a crash):
  • BTC / ETH / SOL price + 24h%          (scanner's own exchange client)
  • Fear & Greed vs a week ago            (market_intel, alternative.me)
  • BTC dominance                         (market_intel, CoinGecko)
  • Today's high-impact US macro prints   (market_intel, ForexFactory)
  • Signal tally: last-24h fires + ⭐     (strategy2_signals.json)
  • 7-day outcome stat                    (signal_outcomes — the honesty loop)

Sent once per local (Asia/Taipei) day, first sweep after MORNING_BRIEF_HOUR
(default 08:00 — lands right beside the owner's private report). State in
morning_brief_state.json; a confirmed send is required before the day is
marked done, so a Telegram blip retries next sweep (same as daily_report).
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import telegram_utils
import tg_format

STATE_FILE = os.path.join(os.path.dirname(__file__), "morning_brief_state.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")

BRIEF_HOUR = int(os.getenv("MORNING_BRIEF_HOUR", "8"))   # local hour (0-23)
TZ = ZoneInfo("Asia/Taipei")
COINS = (("BTC", "BTC/USDT:USDT"), ("ETH", "ETH/USDT:USDT"), ("SOL", "SOL/USDT:USDT"))

_WD = "一二三四五六日"


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _due(state: dict, now: datetime) -> bool:
    return now.hour >= BRIEF_HOUR and state.get("last_brief") != now.strftime("%Y-%m-%d")


# ── formatting (pure given `data` — unit-testable without network) ──────────
def _fng_zh(label: str) -> str:
    return {"Extreme Fear": "極度恐懼", "Fear": "恐懼", "Neutral": "中性",
            "Greed": "貪婪", "Extreme Greed": "極度貪婪"}.get(label or "", label or "")


def build_brief(data: dict, now: datetime) -> str:
    lines = [f"☀️ 早安市場快報 · {now.strftime('%Y-%m-%d')}（週{_WD[now.weekday()]}）"]

    px = data.get("prices") or {}
    px_bits = []
    for base, _sym in COINS:
        t = px.get(base) or {}
        if t.get("last"):
            chg = f"（{tg_format.pct(t['pct'])}）" if t.get("pct") is not None else ""
            px_bits.append(f"{base} {tg_format.fmt_price(t['last'])}{chg}")
    if px_bits:
        lines += ["", "💰 " + " · ".join(px_bits)]

    mood_bits = []
    fng = data.get("fng") or {}
    if fng.get("value") is not None:
        mood = f"恐懼貪婪 {fng['value']}（{_fng_zh(fng.get('label'))}）"
        if fng.get("week_ago") is not None:
            mood += f" · 週前 {fng['week_ago']}"
        mood_bits.append(mood)
    dom = (data.get("global") or {}).get("btc_dominance")
    if dom is not None:
        mood_bits.append(f"BTC 佔比 {dom:.1f}%")
    if mood_bits:
        lines += ["", "🌡 " + " · ".join(mood_bits)]

    cal = data.get("today_events") or []
    if cal:
        lines += ["", "🗓 今日美國高影響數據"] + cal
    else:
        lines += ["", "🗓 今日無高影響美國數據"]

    sig = data.get("signals") or {}
    if sig.get("n"):
        s_line = f"過去 24h 掃出 {sig['n']} 個訊號"
        if sig.get("premium"):
            s_line += f"（⭐ 精選 {sig['premium']} 個）"
        lines += ["", f"📊 {s_line} — 詳見 /signals"]

    oc = data.get("outcomes") or {}
    if (oc.get("n") or 0) >= 5:
        lines.append(f"📋 近 7 日已結算 {oc['n']} 個訊號 · "
                     f"先到目標 {oc['hit_pct']:.0f}% — 完整成績 /outcomes")

    lines += ["", f"🤖 指令 /help · 新朋友看 /guide · {tg_format.DISCLAIMER}"]
    return "\n".join(lines)


# ── data gathering (each piece optional) ─────────────────────────────────────
def _gather_prices(client) -> dict:
    """{base: {last, pct}} via ONE bulk fetch_tickers call; {} on any error."""
    out = {}
    try:
        tickers = client.call("fetch_tickers", [sym for _b, sym in COINS])
    except Exception:  # noqa: BLE001 — no prices ≠ no brief
        return out
    for base, sym in COINS:
        t = (tickers or {}).get(sym) or {}
        try:
            last = float(t.get("last") or t.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if last > 0:
            pct = t.get("percentage")
            out[base] = {"last": last,
                         "pct": float(pct) if pct is not None else None}
    return out


def _signal_tally(now_ts: float) -> dict:
    """Fires in the last 24h from the page feed (public info — it IS the feed)."""
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            sigs = (json.load(f) or {}).get("signals") or []
    except Exception:  # noqa: BLE001 — no file yet
        return {}
    fresh = [s for s in sigs if (s.get("ts") or 0) >= now_ts - 86400]
    return {"n": len(fresh),
            "premium": sum(1 for s in fresh if s.get("premium"))}


def _outcome_stat(now_ts: float) -> dict:
    """7-day 'reached first target' rate from the outcome tracker — the same
    numbers /outcomes shows, condensed to one honest line."""
    try:
        import signal_outcomes
        evaluated = (signal_outcomes._load_state().get("evaluated") or {}).values()
    except Exception:  # noqa: BLE001 — tracker state missing
        return {}
    recent = [v for v in evaluated if (v.get("sig_ts") or 0) >= now_ts - 7 * 86400]
    if not recent:
        return {}
    hits = sum(1 for v in recent if v.get("outcome") in ("tp2", "tp1", "tp1→sl"))
    return {"n": len(recent), "hit_pct": 100.0 * hits / len(recent)}


def _gather(client, now: datetime) -> dict:
    import daily_report
    import market_intel

    data = {"prices": _gather_prices(client)}
    for key, fn in (("fng", market_intel.fear_greed),
                    ("global", market_intel.global_market)):
        try:
            data[key] = fn()
        except Exception as exc:  # noqa: BLE001 — one dead source ≠ no brief
            print(f"[brief] {key} unavailable: {exc}")
            data[key] = None
    try:
        events = market_intel.econ_calendar().get("events") or []
        data["today_events"] = daily_report._today_events(events, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[brief] calendar unavailable: {exc}")
        data["today_events"] = []
    now_ts = time.time()
    data["signals"] = _signal_tally(now_ts)
    data["outcomes"] = _outcome_stat(now_ts)
    return data


# ── orchestrator (called once per scanner sweep) ─────────────────────────────
def tick(client) -> bool:
    """Send today's public brief if due; True only when one was sent. Also
    pins it (unpinning yesterday's) so the group's Report topic always shows
    today's brief at the top without anyone pinning by hand."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _due(state, now):
        return False
    msg = build_brief(_gather(client, now), now)
    mid = telegram_utils.send_message_and_pin(
        msg, force=True, channel="report", unpin_previous=state.get("pinned_mid"))
    if mid:
        # Only mark done on a confirmed send — a Telegram blip retries next sweep.
        state["last_brief"] = now.strftime("%Y-%m-%d")
        state["pinned_mid"] = mid
        _save_state(state)
        print(f"[brief] morning brief sent for {state['last_brief']}")
    return bool(mid)
