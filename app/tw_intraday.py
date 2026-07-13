"""
🇹🇼 台股盤中即時 — real-time session updates into the twstocks topic.

Complements tw_stocks.py (the 14:00 post-close scan) with live coverage of
the TWSE session (09:00–13:30 台北), checked once per scanner sweep:

  🔔 opening bell    one message at the open: TAIEX gap + the setups being
                     monitored today (from previous scans' active_setups)
  🚀/📉 movers       a TW50 stock moves ≥ TW_MOVER_PCT (default 3.5%) —
                     alerted once per stock per day
  📊 TAIEX shock     the index itself moves ≥ TW_TAIEX_ALERT_PCT (1.5%)
  🎯/⚠️ level hits   a tracked setup's price touches its 目標 (TP) or falls
                     through its 停損 (SL) — once per level, remembered
                     across days so it never re-fires

All events found in one sweep go out as ONE message (no burst spam). Data:
TWSE MIS realtime quotes (the same free endpoint the /stocks page uses,
cached 60s). Quotes >30 min stale during the session = holiday → the whole
day is skipped. /twnow answers with a live snapshot on demand.
"""
import json
import os
import time
from datetime import datetime

import requests

import stocks_data
import tw_stocks
from tw_stocks import TZ, _px

STATE_FILE = os.path.join(os.path.dirname(__file__), "tw_intraday_state.json")

MOVER_PCT = float(os.getenv("TW_MOVER_PCT", "3.5"))
TAIEX_ALERT_PCT = float(os.getenv("TW_TAIEX_ALERT_PCT", "1.5"))
SESSION_START_MIN = 9 * 60          # 09:00 台北
SESSION_END_MIN = 13 * 60 + 35      # 13:35 — catch the closing print
STALE_QUOTE_SEC = 1800
HIT_RETAIN_DAYS = 45


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def in_session(now) -> bool:
    minutes = now.hour * 60 + now.minute
    return now.weekday() < 5 and SESSION_START_MIN <= minutes < SESSION_END_MIN


def detect_movers(rows: list, alerted: dict, today: str,
                  threshold: float = None) -> tuple:
    """TW50 rows moving beyond ±threshold, once per code per day.
    Returns (event lines biggest-move-first, {code: today} to record)."""
    threshold = MOVER_PCT if threshold is None else threshold
    events, hits = [], {}
    for r in rows:
        pct, price = r.get("change_pct"), r.get("price")
        if pct is None or not price or abs(pct) < threshold:
            continue
        if alerted.get(r["code"]) == today:
            continue
        hits[r["code"]] = today
        turnover = (r.get("volume_lots") or 0) * 1000 * price / 1e8
        emoji = "🚀" if pct > 0 else "📉"
        events.append((abs(pct),
                       f"{emoji} {r['code']} {r.get('name') or ''} {pct:+.1f}% "
                       f"@ {_px(price)} · 成交 {turnover:.1f}億"))
    events.sort(key=lambda e: e[0], reverse=True)
    return [e[1] for e in events], hits


def check_levels(rows_by_code: dict, setups: list, hits: dict, today: str) -> tuple:
    """SL/TP touches for tracked setups. A setup dated today is NOT yet live
    (entry is the next session's open). Each level fires exactly once, ever.
    Returns (event lines, {hit_key: today} to record)."""
    events, new_hits = [], {}
    for s in setups:
        if s.get("date") == today:
            continue
        row = rows_by_code.get(s.get("code"))
        price = row and row.get("price")
        if not price:
            continue
        checks = (("sl", s["sl"], price <= s["sl"], "⚠️", "跌破停損"),
                  ("tp", s["tp"], price >= s["tp"], "🎯", "到達目標"))
        for kind, level, touched, emoji, label in checks:
            key = f"{s['code']}:{s['date']}:{kind}"
            if touched and key not in hits and key not in new_hits:
                new_hits[key] = today
                events.append(f"{emoji} {s['code']} {s.get('name') or ''} "
                              f"{label} {_px(level)} (現價 {_px(price)}) "
                              f"— {s['date'][5:]} 的設定")
    return events, new_hits


def opening_text(now, taiex: dict, monitored: list) -> str:
    wd = "一二三四五六日"[now.weekday()]
    lines = [f"🔔 台股開盤 · {now.strftime('%Y-%m-%d')} (週{wd})"]
    if taiex:
        lines.append(f"TAIEX {_px(taiex['price'])} ({taiex['pct']:+.2f}% vs 昨收)")
    if monitored:
        lines.append(f"今日追蹤 {len(monitored)} 檔設定:")
        lines += [f"• {s['code']} {s.get('name') or ''} — 進 {_px(s['ref'])} / "
                  f"損 {_px(s['sl'])} / 標 {_px(s['tp'])}" for s in monitored[:8]]
    else:
        lines.append("目前無追蹤中的設定 — 等 14:00 收盤掃描")
    lines.append(f"(盤中警報: 個股 ±{MOVER_PCT:g}%、大盤 ±{TAIEX_ALERT_PCT:g}%、"
                 f"設定觸及停損/目標)")
    return "\n".join(lines)


# ── data ─────────────────────────────────────────────────────────────────────
def fetch_taiex():
    """TAIEX index level via the MIS realtime API (code t00)."""
    r = requests.get(
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        params={"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"},
        headers={**stocks_data._UA,
                 "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
        timeout=12)
    r.raise_for_status()
    m = (r.json().get("msgArray") or [{}])[0]
    last = stocks_data._f(m.get("z")) or stocks_data._f(m.get("pz"))
    prev = stocks_data._f(m.get("y"))
    if not (last and prev):
        return None
    return {"price": last, "prev": prev, "pct": (last - prev) / prev * 100}


def _quotes():
    tw, _err = stocks_data._cached("tw", 60, stocks_data._fetch_tw)
    return (tw or {}).get("rows") or [], (tw or {}).get("quote_ts") or 0


def snapshot_text() -> str:
    """/twnow — live TAIEX + TW50 leaders + tracked setups, any time of day."""
    now = datetime.now(TZ)
    rows, quote_ts = _quotes()
    if not rows:
        return "台股即時資料暫時抓不到，稍後再試。"
    lines = [f"🇹🇼 台股即時 · {now.strftime('%H:%M')} "
             f"({'盤中' if in_session(now) else '已收盤'})"]
    try:
        taiex = fetch_taiex()
    except Exception:  # noqa: BLE001 — index line is optional
        taiex = None
    if taiex:
        lines.append(f"TAIEX {_px(taiex['price'])} ({taiex['pct']:+.2f}%)")
    ranked = [r for r in rows if r.get("change_pct") is not None and r.get("price")]
    if ranked:
        up = sorted(ranked, key=lambda r: -r["change_pct"])[:3]
        dn = sorted(ranked, key=lambda r: r["change_pct"])[:3]
        lines.append("📈 " + " · ".join(f"{r['code']} {r['change_pct']:+.1f}%" for r in up))
        lines.append("📉 " + " · ".join(f"{r['code']} {r['change_pct']:+.1f}%" for r in dn))
    by_code = {r["code"]: r for r in rows}
    setups = tw_stocks._load_state().get("active_setups") or []
    if setups:
        lines.append("追蹤設定:")
        for s in setups[:8]:
            row = by_code.get(s["code"])
            px = f"現價 {_px(row['price'])}" if row and row.get("price") else "無報價"
            lines.append(f"• {s['code']} {s.get('name') or ''} {px} "
                         f"(進 {_px(s['ref'])} / 損 {_px(s['sl'])} / 標 {_px(s['tp'])})")
    if quote_ts and time.time() - quote_ts > STALE_QUOTE_SEC:
        age_min = int((time.time() - quote_ts) / 60)
        lines.append(f"(報價為 {age_min} 分鐘前的最後成交)")
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────
def tick() -> bool:
    """Once per scanner sweep during the TWSE session; one batched message
    when anything happened. Returns True if a message was sent."""
    now = datetime.now(TZ)
    if not in_session(now):
        return False
    state = _load_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("date") != today:
        hits = state.get("hits") or {}          # level hits survive the reset
        cutoff = now.date()
        hits = {k: d for k, d in hits.items()
                if (cutoff - datetime.strptime(d, "%Y-%m-%d").date()).days
                <= HIT_RETAIN_DAYS}
        state = {"date": today, "hits": hits}
    if state.get("holiday") == today:
        return False

    rows, quote_ts = _quotes()
    if not rows or not quote_ts:
        return False
    if time.time() - quote_ts > STALE_QUOTE_SEC:
        # stale during "session" = TWSE holiday — but give the feed until
        # 10:00 before writing the whole day off
        if now.hour >= 10:
            state["holiday"] = today
            _save_state(state)
            print(f"[twintraday] {today}: quotes stale, treating as holiday")
        return False

    setups = tw_stocks._load_state().get("active_setups") or []
    monitored = [s for s in setups if s.get("date") != today]
    parts = []

    try:
        taiex = fetch_taiex()
    except Exception:  # noqa: BLE001 — index data is optional
        taiex = None

    if not state.get("opened"):
        state["opened"] = True
        parts.append(opening_text(now, taiex, monitored))

    if (taiex and abs(taiex["pct"]) >= TAIEX_ALERT_PCT
            and state.get("taiex_alerted") != today):
        state["taiex_alerted"] = today
        arrow = "📈" if taiex["pct"] > 0 else "📉"
        parts.append(f"📊 TAIEX 大盤異動 {arrow} {taiex['pct']:+.2f}% "
                     f"@ {_px(taiex['price'])}")

    level_events, new_hits = check_levels({r["code"]: r for r in rows},
                                          monitored, state.get("hits") or {}, today)
    if new_hits:
        state.setdefault("hits", {}).update(new_hits)
        parts.append("\n".join(level_events))

    mover_events, new_movers = detect_movers(rows, state.get("movers") or {}, today)
    if new_movers:
        state.setdefault("movers", {}).update(new_movers)
        parts.append(f"⚡ 台股盤中 {now.strftime('%H:%M')}\n" + "\n".join(mover_events))

    sent = False
    if parts:
        import telegram_utils
        sent = telegram_utils.send_message("\n\n".join(parts), force=True,
                                           channel="twstocks")
        print(f"[twintraday] {now.strftime('%H:%M')}: "
              f"{len(parts)} block(s), sent={sent}")
    _save_state(state)
    return bool(sent)
