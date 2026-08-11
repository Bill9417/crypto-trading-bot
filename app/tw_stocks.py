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
  exit    SL = entry − 3×ATR14 · TP = entry + 5×ATR14 · time-out 60 sessions

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
# Permanent record of every setup that reached an outcome. SEPARATE from the
# state file on purpose: state is working memory that gets pruned every scan,
# and the track record must never be prunable. See _append_outcome.
OUTCOMES_FILE = os.path.join(os.path.dirname(__file__), "tw_outcomes.json")
TZ = ZoneInfo("Asia/Taipei")

SEND_HOUR = 14          # Taipei — market closes 13:30, Yahoo bar is final
SL_ATR = 3.0
TP_ATR = 5.0
# Sessions before a setup is abandoned. Raised 40→60 on 2026-08-11 after a
# walk-forward on 5y of real TW50 bars (train ≤2024-12, test 2025-01→2026-08,
# same next-open fills, stop-first and Taiwan costs as the live rules):
#     hold 40   train +0.89%/trade PF 1.28   test +3.16% PF 1.87
#     hold 60   train +0.95%/trade PF 1.28   test +3.56% PF 1.93
# Better in 4 of the 5 yearly windows (2024 was −0.09%, inside the noise), and
# time-outs — exits taken because the clock ran out rather than because the plan
# resolved — roughly halve (24%→13% of trades). Longer still kept testing better
# (80 sessions, 8×ATR targets) but that variant COLLAPSES in the one bad year
# (2024: PF 1.04, WR 37.6%), which marks it as bull-market beta rather than edge.
# 60 was taken as the last setting that survives the bad window, not the best
# number on the full sample.
MAX_HOLD = 60
COOLDOWN_DAYS = 10      # don't re-alert the same stock while it likely still runs
MAX_SHOW = 10
RETRY_SEC = 1800        # min gap between failed fetch attempts
FETCH_GAP = 0.12        # polite spacing between Yahoo requests, success or not

# 🚀 Momentum breakout — the optimised winner of a 5y real-TW50 sweep (~30
# param sets): close breaks ABOVE the prior 60-session high while the market is
# in an uptrend. SL 3×ATR / TP 5×ATR was the balance point — 57% WR, PF ~1.9,
# ~+3%/trade, best mix of win-rate AND profit of everything tested. (Backtest is
# optimistic: 5y bull window, survivorship, in-sample pick — forward will be
# lower; see 台股策略回測 report.)
BO_LOOKBACK = 60
BO_SL_ATR = 3.0
BO_TP_ATR = 5.0
# (BO_MAX_HOLD used to live here at 60 while reconcile_setup only ever applied
# MAX_HOLD=40, so breakouts were abandoned 20 sessions before their own rule
# said to. MAX_HOLD is now 60 for both and the second constant is gone rather
# than left around to drift again.)

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


# ── outcome ledger — the permanent track record ─────────────────────────────
# Until 2026-08-11 there was NO record. A resolved setup was shown in one more
# digest and then dropped from active_setups, and active_setups was the only
# place outcomes lived — so every settled trade was deleted about a day after it
# settled. Two things followed:
#   • the Sunday scorecard tallied by SIGNAL week while trades take weeks to
#     resolve, so a trade almost never resolved inside its own signal week and
#     was purged long before any later Sunday could see it. It has been
#     reporting "0 達標 · 0 停損 · 0.0R" every week, structurally, since launch;
#   • after a month live there was no way to answer "do these signals work?"
# The ledger is append-only and keyed by (code, signal date) so re-running a
# scan, or the intraday watcher and the daily reconcile both stamping the same
# setup, cannot double-count it.
def _load_outcomes() -> list:
    try:
        with open(OUTCOMES_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:  # noqa: BLE001 — missing/corrupt ledger = empty history
        return []


def _save_outcomes(rows: list) -> None:
    tmp = OUTCOMES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUTCOMES_FILE)


def outcome_row(rec: dict, source: str = "live") -> dict:
    """One settled setup → one ledger row, with its R already worked out.

    R is measured against the setup's OWN risk (ref − sl), so a −24%-stop name
    and a −4%-stop name contribute on the same scale. A time-out is scored at
    the price it actually exited on when reconcile_setup captured one; without
    that price it is recorded as unscored (r=None) rather than as a zero,
    because "the clock ran out" is not the same claim as "it went nowhere"."""
    hit = rec.get("hit") or {}
    ref, sl, tp = rec.get("ref"), rec.get("sl"), rec.get("tp")
    risk = (ref - sl) if (ref and sl and ref > sl) else None
    kind = hit.get("kind")
    if kind == "tp":
        r = (tp - ref) / risk if (risk and tp) else None
    elif kind == "sl":
        r = -1.0
    else:                                   # timeout — scored only if we know where
        px = hit.get("price")
        r = (px - ref) / risk if (risk and px) else None
    return {"code": rec.get("code"), "name": rec.get("name"),
            "date": rec.get("date"), "strategy": rec.get("strategy") or "pullback",
            "ref": ref, "sl": sl, "tp": tp,
            "kind": kind, "exit_date": hit.get("date"), "exit_price": hit.get("price"),
            "r": round(r, 3) if r is not None else None,
            "held": rec.get("held"), "source": source}


def _append_outcome(rec: dict, source: str = "live") -> bool:
    """Record a settled setup once. Returns True when the ledger grew."""
    if not (rec.get("hit") or {}).get("kind"):
        return False
    rows = _load_outcomes()
    key = (rec.get("code"), rec.get("date"))
    if any((r.get("code"), r.get("date")) == key for r in rows):
        return False                        # already recorded — never double-count
    rows.append(outcome_row(rec, source))
    rows.sort(key=lambda r: (r.get("exit_date") or "", r.get("code") or ""))
    _save_outcomes(rows)
    return True


def record_stats(rows: list) -> dict:
    """Honest tally over ledger rows: counts, win rate, total/average R.

    Win rate is deliberately NOT the headline anywhere this is rendered — this
    repo has twice measured 77%-win-rate configurations that lost money."""
    tp = sum(1 for r in rows if r.get("kind") == "tp")
    sl = sum(1 for r in rows if r.get("kind") == "sl")
    to = sum(1 for r in rows if r.get("kind") == "timeout")
    scored = [r["r"] for r in rows if r.get("r") is not None]
    total_r = sum(scored)
    decided = tp + sl
    return {"total": len(rows), "wins": tp, "losses": sl, "timeouts": to,
            "scored": len(scored),
            "win_pct": round(tp / decided * 100, 1) if decided else None,
            "total_r": round(total_r, 2),
            "avg_r": round(total_r / len(scored), 3) if scored else None}


def all_time_record(source: str = None) -> dict:
    """Whole-ledger tally. source='live' for signals actually sent, 'replay'
    for rows reconstructed from history — never silently mixed, because one is
    a record of what this bot told people and the other is a backtest."""
    rows = _load_outcomes()
    if source:
        rows = [r for r in rows if r.get("source") == source]
    return record_stats(rows)


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


# How much NT$ a single setup is allowed to lose if it stops out. Sizing off a
# fixed LOSS rather than a fixed spend is what makes wildly different stops
# comparable: on 2026-08-11 the scan produced 南亞 at 190 with a 144 stop
# (−24%, because its ATR had genuinely blown out to 8% of price) alongside
# names stopping −4%. Told to "buy 南亞 at 190", a reader takes six times the
# intended risk. Told to buy the share count that loses NT$10,000 at the stop,
# both setups risk the same and the wide stop simply means fewer shares.
#
# Filtering the wide ones out instead was measured and rejected: on 5y of TW50
# bars, capping the stop at 15%/12%/10% made results WORSE in both walk-forward
# halves (test +3.16% → +2.81% → +2.44% → +1.76% per trade), and the 12–18%
# stop bucket was the single best one in the sample (PF 2.42). Only stops wider
# than ~18% were a wash (n=53, R +0.03, CI [−5.8,+6.6]). So the wide setups are
# kept and sized down, not dropped.
RISK_BUDGET = int(os.getenv("TW_RISK_BUDGET", "10000"))     # NT$ risked per setup


def position_size(ref: float, sl: float, budget: float = None) -> dict:
    """Shares to buy so that being stopped out costs `budget`, not more.

    Taiwan quotes in 張 (1,000 shares) but odd lots trade freely, so both are
    reported and the reader can pick. Returns empty when the geometry is
    unusable rather than guessing."""
    budget = RISK_BUDGET if budget is None else budget
    if not (ref and sl and ref > sl > 0 and budget > 0):
        return {}
    per_share = ref - sl
    shares = int(budget // per_share)
    if shares <= 0:
        return {}
    return {"shares": shares, "lots": round(shares / 1000, 2),
            "cost": round(shares * ref), "risk": round(shares * per_share),
            "budget": budget}


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
    tw_intraday when a level fires (same process — no write race).

    Also writes the outcome to the permanent ledger. This is the path that
    resolves a setup DURING the session; the daily reconcile catches whatever
    happened while the watcher was not polling. Both funnel into
    _append_outcome, which dedupes on (code, signal date), so a level seen live
    and then re-derived from the daily bar is recorded once."""
    state = _load_state()
    changed = False
    for s in (state.get("active_setups") or []):
        if s.get("code") == code and s.get("date") == setup_date and not s.get("hit"):
            # the touched level IS the exit price — that is what the plan says
            # to do when it trades there
            s["hit"] = {"kind": kind, "date": date, "time": hhmm,
                        "price": s.get("tp") if kind == "tp" else s.get("sl")}
            try:
                _append_outcome(s)
            except Exception:  # noqa: BLE001 — a ledger write must never lose the stamp
                pass
            changed = True
    if changed:
        _save_state(state)


def _ysym(code: str) -> str:
    """TWSE code → Yahoo symbol. OTC names live on .TWO, listed ones on .TW."""
    return f"{code}.TWO" if code in _TW_OTC else f"{code}.TW"


def reconcile_setup(rec: dict, rows: list) -> dict:
    """Settle one tracked setup against its own DAILY bars. Returns the record.

    tw_intraday only stamps a level it watched cross live, so anything that
    happened while it was not polling was never recorded: 2382 廣達 first traded
    below its 344.5 stop on 2026-07-17 and sat below it on 14 of the next 17
    sessions, still showing 未觸發 three weeks later. An unrecorded stop-out is
    not a cosmetic problem — the weekly scorecard counts it as open instead of
    as the loss it was, so the win rate it reports is not the strategy's.

    Stop first when both levels trade in the same session: the backtest resolves
    it that way, and a tracker that resolves it the other way would report a
    better record than the rules can actually produce.

    Sessions are counted from the DATA, not from the calendar, so the hold
    matches MAX_HOLD as backtested rather than a weekday guess."""
    if rec.get("hit"):
        return rec
    try:
        d0 = datetime.strptime(rec["date"], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return rec
    sl, tp = rec.get("sl"), rec.get("tp")
    held = 0
    for (t, o, h, l, c, v) in rows or []:
        d = datetime.fromtimestamp(t, TZ).date()
        if d <= d0:
            continue                     # entry is the NEXT session's open
        held += 1
        ds = d.strftime("%Y-%m-%d")
        if sl and l <= sl:
            rec["hit"] = {"kind": "sl", "date": ds, "time": "—", "price": sl}
            rec["held"] = held
            return rec
        if tp and h >= tp:
            rec["hit"] = {"kind": "tp", "date": ds, "time": "—", "price": tp}
            rec["held"] = held
            return rec
        if held >= MAX_HOLD:
            # A time-out exits at that session's CLOSE. Recording the price is
            # what lets the ledger score it: a trade that ran out of clock at
            # +2.4R and one that limped out at −0.3R are both "timeout", and
            # counting them as the same thing (or as zero) would misstate the
            # record in whichever direction the market happened to be going.
            rec["hit"] = {"kind": "timeout", "date": ds, "time": "—", "price": c}
            rec["held"] = held
            return rec
    rec["held"] = held
    return rec


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


def _iso_week_of(date_str: str):
    try:
        y, w, _ = datetime.strptime(date_str, "%Y-%m-%d").date().isocalendar()
        return (y, w)
    except (ValueError, TypeError):
        return None


def weekly_scorecard(state: dict, now, outcomes: list = None) -> dict:
    """This ISO week's honest tally: what RESOLVED this week, plus what is still
    running and how many new signals were issued.

    Tallied by EXIT date, out of the permanent ledger. The previous version
    counted by SIGNAL date out of active_setups, which could not work: a setup
    holds for up to MAX_HOLD sessions, so it essentially never resolves inside
    the same ISO week it fired, and resolved rows were dropped from
    active_setups a day after settling. Both errors point the same way — the
    card reported 0 wins and 0 losses every single week since launch, which
    reads as "nothing went wrong" rather than "nothing is being measured".

    Pure: pass `outcomes` to test without touching disk."""
    iso_year, iso_week, _ = now.isocalendar()
    rows = _load_outcomes() if outcomes is None else outcomes
    rows = [r for r in rows if r.get("source", "live") == "live"]
    closed = [r for r in rows if _iso_week_of(r.get("exit_date")) == (iso_year, iso_week)]

    setups = state.get("active_setups") or []
    still_open = sum(1 for s in setups if not (s.get("hit") or {}).get("kind"))
    new_signals = sum(1 for s in setups
                      if _iso_week_of(s.get("date")) == (iso_year, iso_week))

    card = record_stats(closed)
    card.update({"iso_year": iso_year, "iso_week": iso_week,
                 "closed": len(closed), "still_open": still_open,
                 "new_signals": new_signals})
    return card


def build_scorecard_plain(now, card: dict, record: dict = None) -> str:
    """Plain-Chinese weekly scorecard for LINE — leads with the honest R
    tally, not a bare win rate (a family reader shouldn't learn to chase
    win% any more than a trader should).

    `record` is the running all-time tally; it is what makes the card mean
    anything. One week is 2–5 trades, which is noise in either direction, so
    the week is reported and then immediately put next to the cumulative
    figure rather than left to stand on its own."""
    lines = [f"📋 本週台股訊號成績單 · 第{card['iso_week']}週", ""]
    if card["closed"] == 0:
        lines.append("本週沒有訊號結算（到目標／停損／到期）。")
        if card.get("new_signals"):
            lines.append(f"本週新增 {card['new_signals']} 檔訊號，仍在追蹤中。")
    else:
        lines.append(f"本週結算 {card['closed']} 檔：")
        lines.append(f"🎯 達標 {card['wins']} 檔")
        lines.append(f"🛑 停損 {card['losses']} 檔")
        if card.get("timeouts"):
            lines.append(f"⌛ 到期出場 {card['timeouts']} 檔（沒到目標也沒停損，時間到先走）")
        if card.get("total_r") is not None:
            sign = "+" if card["total_r"] >= 0 else ""
            lines += ["",
                      f"本週合計 {sign}{card['total_r']:.1f}R"
                      "（R = 每筆的風險單位；賺賠幅度的總和，比單純勝率更能反映實際結果）"]
    if card.get("still_open"):
        lines.append(f"⏳ 目前還有 {card['still_open']} 檔在追蹤中")

    if record and record.get("total"):
        lines += ["", f"📈 累計成績（共 {record['total']} 檔已結算）："]
        lines.append(f"　達標 {record['wins']} · 停損 {record['losses']}"
                     + (f" · 到期 {record['timeouts']}" if record.get("timeouts") else ""))
        if record.get("avg_r") is not None:
            sign = "+" if record["total_r"] >= 0 else ""
            lines.append(f"　合計 {sign}{record['total_r']:.1f}R"
                         f"（平均每筆 {record['avg_r']:+.2f}R）")
        if record["total"] < 30:
            lines.append("　⚠️ 樣本還太少，這個數字現在只能參考，不能當結論")

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
    plain = build_scorecard_plain(now, card, all_time_record(source="live"))
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
            "size": position_size(ref, sl),
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
    order = {"new": 0, "tracking": 1, "tp": 2, "sl": 2, "timeout": 2}
    setups.sort(key=lambda r: r["date"] or "", reverse=True)
    setups.sort(key=lambda r: (order.get(r["status"], 3), not r.get("buy_zone")))

    try:
        record = all_time_record(source="live")
        recent = [r for r in _load_outcomes() if r.get("source") == "live"][-12:]
    except Exception:  # noqa: BLE001 — the record is context, never load-bearing
        record, recent = {}, []

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
        "record": record, "recent_outcomes": list(reversed(recent)),
        "risk_budget": RISK_BUDGET,
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
    bars = {}                     # code → today's bars, reused by the settle pass
    for code, name_en in TW50:
        try:
            rows = _yahoo_daily(_ysym(code))
            bars[code] = rows
            s = setup_breakout(rows) or setup(rows)   # prefer the momentum breakout
        except Exception:  # noqa: BLE001 — one dead symbol must not kill the scan
            failures += 1
            continue
        finally:
            # Spacing belongs in `finally`: it used to sit after the try block,
            # so a FAILING symbol skipped it entirely. That inverted the intent —
            # the moment Yahoo started rejecting us we hammered all 50 symbols
            # back-to-back with no gap at all, which is exactly how a soft rate
            # limit becomes a hard one.
            time.sleep(FETCH_GAP)
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
          f"setups={len(setups)} sent={sent} trails_moved={trails_moved} "
          f"fetch_failures={failures}")

    for code, _n, _s in setups:
        alerted[code] = today
    cutoff = now.date()
    state["alerted"] = {c: d for c, d in alerted.items()
                        if (cutoff - datetime.strptime(d, "%Y-%m-%d").date()).days <= 60}
    # Structured copy of today's setups for the intraday watcher (tw_intraday
    # alerts when price hits a setup's SL/TP during the session).
    # Settle yesterday's trackers against their own daily bars before deciding
    # what to keep. Two things were wrong here:
    #   • a setup was retired after 30 CALENDAR days while the backtest holds
    #     MAX_HOLD=40 TRADING sessions (~56 calendar days) — live abandoned
    #     trades ~26 days before the rules say to, so the tracked record could
    #     not match the backtest that justifies the rules;
    #   • a setup that had already hit its stop stayed on the board for the
    #     rest of that window, because nothing dropped it once resolved.
    # reconcile_setup now stamps sl/tp/timeout from the daily bars, resolved
    # rows are shown once more and then dropped, and the hold is counted in
    # sessions. Dropping them is only safe because every resolution is written
    # to the permanent ledger FIRST — see _append_outcome.
    active = []
    settled = 0
    for s in (state.get("active_setups") or []):
        try:
            age_d = (cutoff - datetime.strptime(s["date"], "%Y-%m-%d").date()).days
        except (ValueError, KeyError, TypeError):
            continue
        if age_d > MAX_HOLD * 2:          # far past any possible session count
            continue
        if not s.get("hit"):
            try:
                # Reuse the bars the scan loop already pulled for this symbol.
                # This used to re-fetch every tracked setup from Yahoo a second
                # time in the same tick, doubling the request count for no new
                # information — and unlike the scan loop, with no spacing at all
                # between calls.
                rows = bars.get(s["code"])
                if rows is None:
                    rows = _yahoo_daily(_ysym(s["code"]))
                    time.sleep(FETCH_GAP)
                s = reconcile_setup(s, rows)
            except Exception:  # noqa: BLE001 — a quote blip must not drop a setup
                pass
        hit = s.get("hit") or {}
        if hit.get("kind"):
            try:
                if _append_outcome(s):        # permanent record BEFORE any pruning
                    settled += 1
            except Exception:  # noqa: BLE001 — never let the ledger break the scan
                pass
        if hit:
            # keep it one more digest so the outcome is visible, then let it go
            if hit.get("shown"):
                continue
            hit["shown"] = True
        active.append(s)
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
    if settled:
        print(f"[twstocks] {today}: {settled} setup(s) settled → outcome ledger")
    return bool(sent)
