"""
🇹🇼 台股 daily scan — TW50 pullback setups into the Telegram "twstocks" topic.

One message per TWSE trading day, sent after the 13:30 close (14:00 Taipei):
the TAIEX regime verdict ("good time to enter" or "stand aside") plus every
stock that set up today, each with an entry reference, stop-loss and target.

The rules are EXACTLY the config that survived a 3-year TW50 backtest with
full Taiwan costs (0.1425% commission each way + 0.3% securities tax), fills
at the next open, and stop-first priority when TP and SL hit the same bar:

  regime  TAIEX close > its 100-day SMA  AND  higher than 20 sessions ago
  setup   close > 60-day SMA, today's low tags the 20-day SMA, close > open
          (an uptrend pullback that buyers bought back up)
  exit    SL = entry − 3×ATR14 · TP = entry + 5×ATR14 · time-out 40 sessions

Backtest (tune = 2024-05→2025-08 incl. two crashes / validation = last year):
  tune  n=178  WR 47.8%  avg +0.59%/trade  PF 1.16
  val   n=205  WR 58.0%  avg +5.00%/trade  PF 2.78
Chasing win rate instead was a trap here too: the tightest-TP configs showed
77% WR and LOST money after costs. The regime filter is what turned the tune
window positive — which is why a bearish-regime day sends "stand aside", not
setups.

Data: Yahoo daily candles (.TW / .TWO) — watch-only, nothing here trades.
Runs inside the strategy2_scanner loop; state in tw_stocks_state.json.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from stocks_data import TW50, _TW_OTC

STATE_FILE = os.path.join(os.path.dirname(__file__), "tw_stocks_state.json")
TZ = ZoneInfo("Asia/Taipei")

SEND_HOUR = 14          # Taipei — market closes 13:30, Yahoo bar is final
SL_ATR = 3.0
TP_ATR = 5.0
MAX_HOLD = 40           # sessions; the backtested time-out
COOLDOWN_DAYS = 10      # don't re-alert the same stock while it likely still runs
MAX_SHOW = 10
RETRY_SEC = 1800        # min gap between failed fetch attempts

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"}

BACKTEST_NOTE = (
    "🧪 回測 TW50 三年 (含手續費+證交稅, 隔日開盤進場):\n"
    "   前段(含兩次崩盤) 勝率48% · 每筆+0.6% · PF 1.16\n"
    "   近一年 勝率58% · 每筆+5.0% · PF 2.78\n"
    "⚠️ 勝率≠賺錢 — 同資料上 77% 勝率的緊TP參數是虧損的"
)


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
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── data ─────────────────────────────────────────────────────────────────────
def _yahoo_daily(ysym: str) -> list:
    """[(ts, o, h, l, c, v), ...] — 1y of daily bars, None-rows dropped."""
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
        params={"range": "1y", "interval": "1d"},
        headers=_UA, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        rows.append((t, o, h, l, c, q["volume"][i] or 0))
    return rows


# ── pure signal logic (unit-tested) ──────────────────────────────────────────
def _atr14(rows: list):
    prev_c, a, trs = None, None, []
    for (t, o, h, l, c, v) in rows:
        tr = h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
        if len(trs) == 14:
            a = sum(trs) / 14
        elif len(trs) > 14:
            a = (a * 13 + tr) / 14
    return a


def regime(taiex_rows: list) -> dict:
    """TAIEX filter: above 100-day SMA AND above its close 20 sessions ago."""
    cs = [r[4] for r in taiex_rows]
    if len(cs) < 121:
        return {"ok": False, "reason": "insufficient data"}
    c, sma100 = cs[-1], sum(cs[-100:]) / 100
    mom20 = c / cs[-21] - 1
    return {"ok": c > sma100 and mom20 > 0,
            "close": c, "sma100": sma100, "mom20": mom20}


def setup(rows: list):
    """Today's pullback setup on one stock, or None. Uses the LAST (closed) bar:
    close > SMA60, low tags SMA20, close > open. Levels off the close as the
    next-open entry reference (what the backtest actually fills at)."""
    if len(rows) < 75:
        return None
    cs = [r[4] for r in rows]
    t, o, h, l, c, v = rows[-1]
    s60 = sum(cs[-60:]) / 60
    s20 = sum(cs[-20:]) / 20
    if not (c > s60 and l <= s20 and c > o):
        return None
    atr = _atr14(rows)
    if not atr:
        return None
    return {"ref": c, "sl": c - SL_ATR * atr, "tp": c + TP_ATR * atr,
            "atr": atr, "turnover": c * v}


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


_WD = "一二三四五六日"


def build_digest(now, reg: dict, setups: list, pattern_blocked: int = 0,
                 cooldown_skipped: int = 0) -> str:
    """setups: [(code, name, setup-dict), ...] — already filtered + sorted."""
    lines = [f"🇹🇼 台股掃描 · {now.strftime('%Y-%m-%d')} (週{_WD[now.weekday()]})", ""]

    if reg.get("ok"):
        lines.append(f"✅ 大盤多頭 — TAIEX {_px(reg['close'])} 站上100日線 "
                     f"{_px(reg['sma100'])} · 20日 {reg['mom20'] * 100:+.1f}%")
        lines.append("→ 可進場時機 (策略只在此狀態出手)")
    else:
        if "close" in reg:
            lines.append(f"⛔ 大盤空頭/整理 — TAIEX {_px(reg['close'])} vs 100日線 "
                         f"{_px(reg['sma100'])} · 20日 {reg['mom20'] * 100:+.1f}%")
        else:
            lines.append("⛔ 大盤資料不足")
        lines.append("→ 觀望，不進場 (回測: 濾掉此時的訊號後才轉正)")
        if pattern_blocked:
            lines.append(f"   ({pattern_blocked} 檔符合型態但被大盤濾網擋下)")

    lines.append("")
    if reg.get("ok"):
        if setups:
            import tg_format
            shown = setups[:MAX_SHOW]
            lines.append(f"🎯 今日訊號 {len(setups)} 檔 — 回踩20日線收紅:")
            rows = [("代號", "名稱", "進場", "停損", "", "目標", "")]
            for code, name, s in shown:
                risk = (s["ref"] - s["sl"]) / s["ref"] * 100
                gain = (s["tp"] - s["ref"]) / s["ref"] * 100
                rows.append((code, name, _px(s["ref"]),
                             _px(s["sl"]), f"−{risk:.1f}%",
                             _px(s["tp"]), f"+{gain:.1f}%"))
            lines.append(tg_format.pre_table(rows, align="llrrrrr"))
            lines.append("進場=下一交易日開盤參考 · 停損 3×ATR · 目標 5×ATR")
            if len(setups) > MAX_SHOW:
                lines.append(f"…另有 {len(setups) - MAX_SHOW} 檔未列出")
            lines.append(f"⏱ 未到目標/停損時最長持有 {MAX_HOLD} 個交易日")
            if cooldown_skipped:
                lines.append(f"({cooldown_skipped} 檔 {COOLDOWN_DAYS} 日內已提醒過，未重複列出)")
        else:
            lines.append("🔍 今日無符合條件的個股 — 等站上60日線後回踩20日線的訊號")

    lines += ["", BACKTEST_NOTE]
    return "\n".join(lines)


def _due(state: dict, now) -> bool:
    """One send per TWSE trading day, after the close is final."""
    if now.weekday() >= 5 or now.hour < SEND_HOUR:
        return False
    return state.get("last_run_date") != now.strftime("%Y-%m-%d")


# ── orchestration ────────────────────────────────────────────────────────────
def _chinese_names() -> dict:
    """code → Chinese short name via the /stocks page cache; EN fallback."""
    try:
        import stocks_data
        tw, _err = stocks_data._cached("tw", 600, stocks_data._fetch_tw)
        return {r["code"]: r.get("name") or "" for r in (tw or {}).get("rows", [])}
    except Exception:  # noqa: BLE001 — names are decoration, never fatal
        return {}


def tick() -> bool:
    """Called every scanner sweep; does one scan per trading day. Returns True
    when a digest was sent."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _due(state, now):
        return False
    if time.time() - state.get("last_attempt", 0) < RETRY_SEC:
        return False
    state["last_attempt"] = time.time()
    today = now.strftime("%Y-%m-%d")

    try:
        taiex = _yahoo_daily("%5ETWII")
    except Exception as exc:  # noqa: BLE001 — retry next window
        print(f"[twstocks] TAIEX fetch failed: {exc}")
        _save_state(state)
        return False
    if not taiex or datetime.fromtimestamp(taiex[-1][0], TZ).strftime("%Y-%m-%d") != today:
        # Today's bar is missing. Before 15:00 that might just be Yahoo lag —
        # keep retrying (RETRY_SEC-paced); after 15:00 call it a TWSE holiday.
        if now.hour >= SEND_HOUR + 1:
            state["last_run_date"] = today
            print(f"[twstocks] {today}: TWSE holiday, skipped")
        else:
            print(f"[twstocks] {today}: no bar for today yet, will retry")
        _save_state(state)
        return False

    reg = regime(taiex)
    names = _chinese_names()
    alerted = state.get("alerted") or {}
    setups, blocked, skipped, failures = [], 0, 0, 0
    for code, name_en in TW50:
        try:
            rows = _yahoo_daily(f"{code}.TWO" if code in _TW_OTC else f"{code}.TW")
            s = setup(rows)
        except Exception:  # noqa: BLE001 — one dead symbol must not kill the scan
            failures += 1
            continue
        time.sleep(0.12)          # polite spacing for EVERY Yahoo request
        if not s:
            continue
        if not reg["ok"]:
            blocked += 1
            continue
        last = alerted.get(code)
        if last and (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days < COOLDOWN_DAYS:
            skipped += 1
            continue
        setups.append((code, names.get(code) or name_en, s))
    if failures > 20:
        print(f"[twstocks] aborted — {failures} symbol fetches failed")
        _save_state(state)
        return False

    setups.sort(key=lambda x: x[2]["turnover"], reverse=True)
    msg = build_digest(now, reg, setups, blocked, skipped)

    import telegram_utils
    sent = telegram_utils.send_message(msg, parse_mode="HTML", force=True,
                                       channel="twstocks")
    print(f"[twstocks] {today}: regime={'BULL' if reg.get('ok') else 'OFF'} "
          f"setups={len(setups)} sent={sent}")

    for code, _n, _s in setups:
        alerted[code] = today
    cutoff = now.date()
    state["alerted"] = {c: d for c, d in alerted.items()
                        if (cutoff - datetime.strptime(d, "%Y-%m-%d").date()).days <= 60}
    # Structured copy of today's setups for the intraday watcher (tw_intraday
    # alerts when price hits a setup's SL/TP during the session).
    active = [s for s in (state.get("active_setups") or [])
              if (cutoff - datetime.strptime(s["date"], "%Y-%m-%d").date()).days <= 30]
    active += [{"code": code, "name": name, "date": today,
                "ref": s["ref"], "sl": s["sl"], "tp": s["tp"]}
               for code, name, s in setups]
    state["active_setups"] = active
    state["last_run_date"] = today
    state["last_digest_text"] = msg
    _save_state(state)
    return bool(sent)
