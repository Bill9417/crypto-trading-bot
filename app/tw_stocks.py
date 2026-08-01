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

# 🚀 Momentum breakout — the optimised winner of a 5y real-TW50 sweep (~30
# param sets): close breaks ABOVE the prior 60-session high while the market is
# in an uptrend. SL 3×ATR / TP 5×ATR was the balance point — 57% WR, PF ~1.9,
# ~+3%/trade, best mix of win-rate AND profit of everything tested. (Backtest is
# optimistic: 5y bull window, survivorship, in-sample pick — forward will be
# lower; see 台股策略回測 report.)
BO_LOOKBACK = 60
BO_SL_ATR = 3.0
BO_TP_ATR = 5.0
BO_MAX_HOLD = 60

# 🏃 "Runner" trailing stop — the HIGHEST-PROFIT variant found (5y TW50):
# replace the fixed target with a 5×ATR stop trailing the highest high since
# entry. Backtest at 4 concurrent positions: ~40% CAGR, ~15% maxDD, PF ~5.9.
# It passed both robustness checks (trail 4–7 is a plateau, not a spike; both
# halves of the window profitable) — BUT it is the OPPOSITE of high win rate:
# only ~45% of trades win, the MEDIAN trade loses ~1%, and the top 5 trades
# produced 81% of all profit. Miss the best 3 and CAGR falls 40%→15%. It only
# works if every signal is taken and winners are held through deep pullbacks.
# Tracked here as extra info on each setup; the primary target stays TP_ATR.
TRAIL_ATR = 5.0

_KIND_TAG = {"pullback": "回踩", "breakout": "突破"}

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
            "atr": atr, "turnover": c * v, "kind": "pullback"}


def setup_breakout(rows: list):
    """Today's momentum-breakout setup on one stock, or None: the LAST (closed)
    bar's close breaks ABOVE the highest high of the PRIOR 60 sessions (a clean
    Donchian breakout, price-level independent). Levels off the close;
    SL 3×ATR, TP 5×ATR."""
    if len(rows) < BO_LOOKBACK + 15:
        return None
    t, o, h, l, c, v = rows[-1]
    prior_high = max(r[2] for r in rows[-BO_LOOKBACK - 1:-1])   # prior N, excl. today
    if c <= prior_high:
        return None
    atr = _atr14(rows)
    if not atr:
        return None
    return {"ref": c, "sl": c - BO_SL_ATR * atr, "tp": c + BO_TP_ATR * atr,
            "atr": atr, "turnover": c * v, "kind": "breakout"}


def update_trail(rec: dict, rows: list) -> bool:
    """Refresh one tracked setup's runner trail from fresh daily bars.

    Sets rec['peak'] (highest high since the entry bar) and rec['trail']
    (peak − TRAIL_ATR×ATR14, ratcheted — a trailing stop never moves down).
    Returns True when the record changed. Pure/fail-soft: unparseable dates or
    short histories leave the record untouched."""
    if not rows or not rec.get("date"):
        return False
    try:
        start = datetime.strptime(rec["date"], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return False
    since = [r for r in rows
             if datetime.fromtimestamp(r[0], TZ).date() >= start]
    if not since:
        return False
    atr = _atr14(rows)
    if not atr:
        return False
    peak = max(max(r[2] for r in since), rec.get("peak") or 0.0)
    trail = peak - TRAIL_ATR * atr
    # a trailing stop only ratchets UP, and never below the original stop
    trail = max(trail, rec.get("trail") or 0.0, rec.get("sl") or 0.0)
    changed = (rec.get("peak") != peak) or (rec.get("trail") != trail)
    rec["peak"], rec["trail"] = peak, trail
    return changed


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
            lines.append(f"🎯 今日訊號 {len(setups)} 檔（回踩買點＋突破動能）:")
            rows = [("代號", "名稱", "策略", "進場", "停損", "", "目標", "")]
            for code, name, s in shown:
                risk = (s["ref"] - s["sl"]) / s["ref"] * 100
                gain = (s["tp"] - s["ref"]) / s["ref"] * 100
                rows.append((code, name, _KIND_TAG.get(s.get("kind"), "回踩"),
                             _px(s["ref"]), _px(s["sl"]), f"−{risk:.1f}%",
                             _px(s["tp"]), f"+{gain:.1f}%"))
            lines.append(tg_format.pre_table(rows, align="lllrrrrr"))
            lines.append("進場=下一交易日開盤參考 · 回踩 SL3×/TP5× · 突破 SL3×/TP5× ATR")
            if len(setups) > MAX_SHOW:
                lines.append(f"…另有 {len(setups) - MAX_SHOW} 檔未列出")
            lines.append(f"⏱ 未到目標/停損時最長持有 {MAX_HOLD} 個交易日")
            if cooldown_skipped:
                lines.append(f"({cooldown_skipped} 檔 {COOLDOWN_DAYS} 日內已提醒過，未重複列出)")
        else:
            lines.append("🔍 今日無符合條件的個股 — 等站上60日線後回踩20日線的訊號")

    lines += ["", BACKTEST_NOTE]
    return "\n".join(lines)


def build_digest_plain(now, reg: dict, setups: list) -> str:
    """LINE version of the digest — plain text, no HTML/<pre> tables (LINE is
    a proportional font), written in everyday Chinese for a family reader."""
    lines = [f"🇹🇼 台股掃描 {now.strftime('%Y-%m-%d')} (週{_WD[now.weekday()]})", ""]
    if reg.get("ok"):
        lines.append(f"✅ 大盤多頭 — 加權指數 {_px(reg['close'])} 站上100日均線")
        if setups:
            lines.append(f"🎯 今日訊號 {len(setups)} 檔（回檔買點＋突破動能）:")
            for code, name, s in setups[:MAX_SHOW]:
                risk = (s["ref"] - s["sl"]) / s["ref"] * 100
                gain = (s["tp"] - s["ref"]) / s["ref"] * 100
                lines += ["",
                          f"■ {code} {name}（{_KIND_TAG.get(s.get('kind'), '回踩')}）",
                          f"　進場參考 {_px(s['ref'])}（明日開盤附近）",
                          f"　停損 {_px(s['sl'])}（約 −{risk:.1f}%）",
                          f"　目標 {_px(s['tp'])}（約 +{gain:.1f}%）"]
            if len(setups) > MAX_SHOW:
                lines += ["", f"…另有 {len(setups) - MAX_SHOW} 檔未列出"]
            lines += ["",
                      "📌 跌破停損就賣出、到目標就獲利了結",
                      f"⏱ 最多抱 {MAX_HOLD} 個交易日，沒到就先出場"]
        else:
            lines.append("🔍 今日沒有符合條件的股票，休息一天")
    else:
        lines.append("⛔ 大盤未達多頭條件 — 今日觀望，不進場")
        if "close" in reg:
            why = ("指數在100日均線之下" if reg["close"] <= reg["sma100"]
                   else "近20日走勢轉弱")
            lines.append(f"　加權指數 {_px(reg['close'])}（{why}）")
    lines += ["", "⚠️ 訊號來自歷史回測，過去績效不代表未來，請自行評估風險"]
    return "\n".join(lines)


def mark_hit(code: str, setup_date: str, kind: str, date: str, hhmm: str) -> None:
    """Stamp a tracked setup with its SL/TP touch (kind 'sl'|'tp') so every
    tracking view can show 已停損/已達標 with the touch date+time. Called by
    tw_intraday when a level fires (same process — no write race)."""
    state = _load_state()
    changed = False
    for s in (state.get("active_setups") or []):
        if s.get("code") == code and s.get("date") == setup_date and not s.get("hit"):
            s["hit"] = {"kind": kind, "date": date, "time": hhmm}
            changed = True
    if changed:
        _save_state(state)


def _due(state: dict, now) -> bool:
    """One send per TWSE trading day, after the close is final."""
    if now.weekday() >= 5 or now.hour < SEND_HOUR:
        return False
    return state.get("last_run_date") != now.strftime("%Y-%m-%d")


# ── weekly scorecard (honest tally, not a win-rate pitch) ────────────────────
SCORECARD_HOUR = int(os.getenv("TW_SCORECARD_HOUR", "9"))   # Sunday, Taipei local


def _scorecard_due(state: dict, now) -> bool:
    iso_year, iso_week, iso_weekday = now.isocalendar()
    if iso_weekday != 7 or now.hour < SCORECARD_HOUR:      # ISO weekday 7 = Sunday
        return False
    return state.get("scorecard_sent_week") != f"{iso_year}-W{iso_week:02d}"


def weekly_scorecard(state: dict, now) -> dict:
    """This ISO week's (Mon–Sun) honest TP/SL/still-open tally from
    active_setups. The R-multiple isn't a backtest guess — 'tp'/'sl' are only
    stamped when tw_intraday's LIVE quotes actually touched that exact level,
    so it reflects real observed price action, not a model assumption. Pure
    (no I/O), unit-testable without touching the state file."""
    setups = state.get("active_setups") or []
    iso_year, iso_week, _ = now.isocalendar()

    def _in_week(date_str):
        y, w, _ = datetime.strptime(date_str, "%Y-%m-%d").date().isocalendar()
        return (y, w) == (iso_year, iso_week)

    this_week = [s for s in setups if _in_week(s["date"])]
    wins = sum(1 for s in this_week if (s.get("hit") or {}).get("kind") == "tp")
    losses = sum(1 for s in this_week if (s.get("hit") or {}).get("kind") == "sl")
    return {"iso_year": iso_year, "iso_week": iso_week, "total": len(this_week),
            "wins": wins, "losses": losses,
            "still_open": len(this_week) - wins - losses,
            "total_r": wins * (TP_ATR / SL_ATR) - losses * 1.0}


def build_scorecard_plain(now, card: dict) -> str:
    """Plain-Chinese weekly scorecard for LINE — leads with the honest R
    tally, not a bare win rate (a family reader shouldn't learn to chase
    win% any more than a trader should)."""
    lines = [f"📋 本週台股訊號成績單 · 第{card['iso_week']}週", ""]
    if card["total"] == 0:
        lines.append("本週沒有新增訊號。")
    else:
        lines.append(f"本週共 {card['total']} 檔訊號：")
        lines.append(f"🎯 達標 {card['wins']} 檔")
        lines.append(f"🛑 停損 {card['losses']} 檔")
        if card["still_open"]:
            lines.append(f"⏳ 追蹤中 {card['still_open']} 檔（還沒到停損或目標）")
        sign = "+" if card["total_r"] >= 0 else ""
        lines += ["",
                  f"以停損/停利換算，本週約 {sign}{card['total_r']:.1f}R"
                  "（R = 每筆的風險單位；賺賠幅度的總和，比單純勝率更能反映實際結果）"]
    lines += ["", "⚠️ 訊號僅供參考，過去績效不代表未來"]
    return "\n".join(lines)


def scorecard_tick() -> bool:
    """Self-paced: Sunday-morning honest tally to LINE (no Telegram mirror —
    the group already gets /outcomes; this is the family-plain-Chinese
    equivalent). No-op every other sweep and every other day of the week."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _scorecard_due(state, now):
        return False
    card = weekly_scorecard(state, now)
    plain = build_scorecard_plain(now, card)
    import line_push
    sent = line_push.enabled() and line_push.send(plain)
    state["scorecard_sent_week"] = f"{card['iso_year']}-W{card['iso_week']:02d}"
    _save_state(state)
    return sent


# ── orchestration ────────────────────────────────────────────────────────────
def _chinese_names() -> dict:
    """code → Chinese short name via the /stocks page cache; EN fallback."""
    try:
        import stocks_data
        tw, _err = stocks_data._cached("tw", 600, stocks_data._fetch_tw)
        return {r["code"]: r.get("name") or "" for r in (tw or {}).get("rows", [])}
    except Exception:  # noqa: BLE001 — names are decoration, never fatal
        return {}


def _biz_days_since(date_str: str, now) -> int:
    """Rough trading-day age of a setup (Mon–Fri, TWSE holidays ignored — this
    is a friendly '第 N 天' counter for the web page, not a settlement figure)."""
    from datetime import timedelta
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return 0
    d1 = now.date()
    days, cur = 0, d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def web_view(now=None) -> dict:
    """Read-only payload for the /tw web page (爸爸的『好進場點』看板).

    Reads the persisted daily-scan state — NO network of its own — and overlays
    optional live TWSE quotes so each setup shows whether price is still in a
    good entry zone. Fully fail-soft: every branch degrades to a usable page
    rather than raising, so the route can never 500."""
    now = now or datetime.now(TZ)
    state = _load_state()
    reg = state.get("last_regime") or {}
    if not reg:                       # state predates last_regime: read the verdict
        plain = state.get("last_digest_plain") or ""   # marker out of the digest
        reg = {"ok": "✅ 大盤多頭" in plain}

    prices = {}
    try:
        import stocks_data
        prices = stocks_data.tw_quote_map()
    except Exception:  # noqa: BLE001 — live price is a bonus, never required
        prices = {}

    financials = {}
    try:
        import tw_financials
        for s in (state.get("active_setups") or []):
            code = s.get("code")
            if code and code not in financials:
                financials[code] = tw_financials.summary(code)
    except Exception:  # noqa: BLE001 — 財報 is a bonus, never required
        financials = {}

    last_run = state.get("last_run_date")
    setups = []
    for s in (state.get("active_setups") or []):
        ref, sl, tp = s.get("ref"), s.get("sl"), s.get("tp")
        if not (ref and sl and tp):
            continue
        hit = s.get("hit") or {}
        q = prices.get(s.get("code")) or {}
        price = q.get("price")
        status = (hit.get("kind") if hit else
                  ("new" if s.get("date") == last_run and reg.get("ok")
                   else "tracking"))
        row = {
            "code": s.get("code"), "name": s.get("name"), "date": s.get("date"),
            "days": _biz_days_since(s.get("date"), now),
            "strategy": s.get("strategy", "pullback"),
            "strategy_tag": _KIND_TAG.get(s.get("strategy"), "回踩"),
            "ref": ref, "sl": sl, "tp": tp,
            "ref_s": _px(ref), "sl_s": _px(sl), "tp_s": _px(tp),
            "risk_pct": round((ref - sl) / ref * 100, 1),
            "gain_pct": round((tp - ref) / ref * 100, 1),
            "rr": round((tp - ref) / (ref - sl), 1) if ref > sl else None,
            "status": status, "hit": hit or None,
            "price": price, "price_s": _px(price) if price else None,
            "change_pct": q.get("change_pct"),
            "fin": financials.get(s.get("code")) or None,
        }
        trail = s.get("trail")
        if trail and trail > sl:          # only show once it has ratcheted above SL
            row["trail"] = trail
            row["trail_s"] = _px(trail)
            row["trail_locked"] = trail > ref     # profit already locked in
        if price:
            row["dist_pct"] = round((price - ref) / ref * 100, 1)
            # "Near entry": price is still within ¼ of the stop distance of the
            # pullback reference — hasn't run up past it, hasn't faded most of the
            # way to the stop. Deliberately conservative so the badge means
            # "entering here still matches the setup", not just "above the stop".
            band = 0.25 * (ref - sl)
            row["buy_zone"] = bool(not hit and ref - band <= price <= ref + band)
        setups.append(row)

    # Open setups first (new before tracking), then closed. Within a status,
    # anything still IN its buy zone floats up — on a page called 好進場點 the
    # actionable ones belong at the top, not wherever their date lands. Newest
    # date breaks the remaining ties (the pre-sort is stable).
    order = {"new": 0, "tracking": 1, "tp": 2, "sl": 2}
    setups.sort(key=lambda r: r["date"] or "", reverse=True)
    setups.sort(key=lambda r: (order.get(r["status"], 3), not r.get("buy_zone")))

    return {
        "as_of": last_run,
        "generated_at": int(time.time()),
        "regime": reg,
        "regime_ok": bool(reg.get("ok")),
        "setups": setups,
        "open_count": sum(1 for r in setups if r["status"] in ("new", "tracking")),
        "new_count": sum(1 for r in setups if r["status"] == "new"),
        "buy_zone_count": sum(1 for r in setups if r.get("buy_zone")),
        "sl_atr": SL_ATR, "tp_atr": TP_ATR, "max_hold": MAX_HOLD,
    }


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
    # Runner trails are refreshed from the SAME bars this loop already fetches —
    # no extra Yahoo requests. Grouped by code so a symbol updates in one pass.
    tracking = {}
    for rec in (state.get("active_setups") or []):
        if not rec.get("hit"):
            tracking.setdefault(rec.get("code"), []).append(rec)
    trails_moved = 0
    setups, blocked, skipped, failures = [], 0, 0, 0
    for code, name_en in TW50:
        try:
            rows = _yahoo_daily(f"{code}.TWO" if code in _TW_OTC else f"{code}.TW")
            s = setup_breakout(rows) or setup(rows)   # prefer the momentum breakout
        except Exception:  # noqa: BLE001 — one dead symbol must not kill the scan
            failures += 1
            continue
        time.sleep(0.12)          # polite spacing for EVERY Yahoo request
        for rec in tracking.get(code, ()):        # ratchet this symbol's trails
            try:
                if update_trail(rec, rows):
                    trails_moved += 1
            except Exception:  # noqa: BLE001 — a trail must never break the scan
                pass
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
    plain = build_digest_plain(now, reg, setups)
    import line_push
    if line_push.enabled():                # 爸爸的 LINE — card carousel when
        line_push.send_tw_digest(now, reg, setups, plain)   # there are picks
    print(f"[twstocks] {today}: regime={'BULL' if reg.get('ok') else 'OFF'} "
          f"setups={len(setups)} sent={sent} trails_moved={trails_moved}")

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
                "ref": s["ref"], "sl": s["sl"], "tp": s["tp"],
                "strategy": s.get("kind", "pullback"),
                # runner trail seeded at entry; ratchets up on later scans
                "peak": s["ref"], "trail": s["ref"] - TRAIL_ATR * s["atr"]}
               for code, name, s in setups]
    state["active_setups"] = active
    state["last_run_date"] = today
    state["last_regime"] = reg             # structured verdict for the /tw page
    state["last_digest_text"] = msg
    state["last_digest_plain"] = plain     # served by the LINE 「訊號」 command
    _save_state(state)
    return bool(sent)
