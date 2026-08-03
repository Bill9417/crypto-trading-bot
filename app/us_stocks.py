"""
🇺🇸 US100 daily scan — oversold-in-uptrend entries into the 台股/美股 topic.

One message per US trading day, after the close: the S&P regime verdict plus
every Nasdaq-100 name that set up, each with an entry reference, stop and
target. Watch-only — nothing here trades.

── WHY THESE RULES AND NOT THE TAIWAN ONES ─────────────────────────────────
The 台股 pullback and breakout rules were ported first and MEASURED, not
assumed. On 10 years of real US100 daily bars they produce +1.28% and +1.51%
per trade — and buying on a RANDOM day in the same regime, with the same
exits, produces +1.21% and +1.48%. Their actual contribution is +0.08% and
+0.03% per trade, below the 0.10% round-trip cost. They do not transfer.

That gap is the whole point: US100 over 10 years is a huge bull market and its
membership is survivorship-biased (today's index is yesterday's winners), so
ANY long rule shows a fat positive average. The only number that means
anything is picked-minus-unpicked — the same metric factor_lab uses.

  entry   RSI14 < 30  AND  close > 200-day SMA   (oversold inside an uptrend)
  regime  S&P 500 close > its 100-day SMA  AND  higher than 20 sessions ago
  exit    SL = entry − 3×ATR14 · TP = entry + 5×ATR14 · time-out 40 sessions

Measured on 10y of real US100 bars, next-open fills, stop-first when both
levels hit the same bar, 0.10% round-trip cost:

  n=3091   WR 54.4%   avg +2.53%/trade   random-day baseline +1.19%
  → real edge +1.34% per trade, 13× the round-trip cost

Robustness (all four checked before shipping):
  • plateau   RSI 20/25/30 → +1.25/+1.20/+1.34, decaying smoothly to +0.42 at
              45. A smooth region, not a spike.
  • walk-fwd  8 of 10 calendar years positive. The two negatives are 2018
              (−0.65%, n=189) and 2022 (−1.28%, n=49 — the regime filter had
              already cut the sample to almost nothing).
  • baseline  4 different random seeds → edge +1.15 to +1.44.
  • exits     +1.20 to +1.34 across six sensible SL/TP/hold combinations; only
              a very tight 2×/3×/20 falls off, to +0.53.

On win rate, since it is what gets asked for: 4×ATR/6×ATR/40 measures 57.3% WR
at +1.31% edge — three points more wins for essentially the same money. Here
the win rate is a free dial. That is NOT the usual case: on this project's own
crypto data, 46 of 48 mean-reversion configs at 55–78% WR lost money. Win rate
is reported here, never optimised for.

Also measured and rejected: a low-volatility filter, which HELPS this project's
crypto S1 entries, HURTS on US stocks (ATR<2% → −0.37%, ATR<1.5% → −0.64%).

Caveats that no backtest removes: survivorship (the universe is today's
Nasdaq-100), a mostly-bull decade, and the fact that RSI<30 was picked as the
best of eight candidates, which biases it upward. Forward results will be
lower than +1.34%.

Data: Yahoo daily candles. Runs inside the strategy2_scanner loop; state in
us_stocks_state.json.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from stocks_data import US100

STATE_FILE = os.path.join(os.path.dirname(__file__), "us_stocks_state.json")
TZ = ZoneInfo("Asia/Taipei")
TZ_NY = ZoneInfo("America/New_York")

# 台北 hour to scan. US closes 16:00 ET = 04:00/05:00 台北, so 09:00 台北 is
# comfortably after the daily bar is final and sits beside the 美股收盤 digest.
SEND_HOUR = int(os.getenv("US_STOCKS_SEND_HOUR", "9"))
RSI_MAX = float(os.getenv("US_STOCKS_RSI_MAX", "30"))
TREND_SMA = 200
SL_ATR = 3.0
TP_ATR = 5.0
MAX_HOLD = 40           # sessions; the backtested time-out
COOLDOWN_DAYS = 10      # don't re-alert the same name while the trade likely runs
MAX_SHOW = 8
RETRY_SEC = 1800
_UA = {"User-Agent": "Mozilla/5.0"}


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _yahoo_daily(ysym: str, rng: str = "2y") -> list:
    """[(ts, o, h, l, c, v), ...] daily bars, None-rows dropped."""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                     params={"range": rng, "interval": "1d"},
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


# ── pure signal logic (unit-tested, no network) ─────────────────────────────
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


def _rsi14(closes: list):
    """Wilder RSI on the last bar, or None. 100 when there is no down move at
    all — that is a real reading, not a divide-by-zero to swallow."""
    if len(closes) < 15:
        return None
    gains = losses = 0.0
    for i in range(1, 15):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / 14, losses / 14
    for i in range(15, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * 13 + max(d, 0.0)) / 14
        avg_l = (avg_l * 13 + max(-d, 0.0)) / 14
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def regime(spx_rows: list) -> dict:
    """S&P 500 trend verdict — the same shape tw_stocks uses for the TAIEX.
    A bearish tape sends 'stand aside' instead of setups: in the backtest the
    regime filter is what kept 2022 to 49 trades instead of a bloodbath."""
    if len(spx_rows) < 101:
        return {"ok": False, "why": "資料不足"}
    cs = [r[4] for r in spx_rows]
    c = cs[-1]
    s100 = sum(cs[-100:]) / 100
    above = c > s100
    rising = len(cs) > 20 and c > cs[-21]
    ok = above and rising
    why = ("S&P 站上 100 日均線且高於 20 日前" if ok else
           "S&P 跌破 100 日均線" if not above else "S&P 20 日來走低")
    return {"ok": ok, "why": why, "close": c, "sma100": s100}


def setup(rows: list):
    """Today's oversold-in-uptrend setup on one stock, or None.

    Uses the LAST closed bar. Levels are quoted off that close because the
    backtest fills at the NEXT open — the close is the reference, not a
    promised fill."""
    if len(rows) < TREND_SMA + 15:
        return None
    cs = [r[4] for r in rows]
    t, o, h, l, c, v = rows[-1]
    sma200 = sum(cs[-TREND_SMA:]) / TREND_SMA
    if c <= sma200:
        return None                      # only ever long inside an uptrend
    r = _rsi14(cs)
    if r is None or r >= RSI_MAX:
        return None
    atr = _atr14(rows)
    if not atr:
        return None
    return {"ref": c, "sl": c - SL_ATR * atr, "tp": c + TP_ATR * atr,
            "atr": atr, "rsi": r, "sma200": sma200,
            "turnover": c * v, "kind": "oversold"}


def _px(v: float) -> str:
    return f"{v:,.2f}" if v < 1000 else f"{v:,.0f}"


def build_digest(now, reg: dict, setups: list) -> str:
    """The daily 美股 message. setups: [(ticker, name, setup_dict), ...]."""
    head = f"🇺🇸 <b>美股進場掃描</b> · {now.strftime('%Y-%m-%d')}"
    if not reg.get("ok"):
        return (f"{head}\n\n⚠️ <b>今日不進場</b> — {reg.get('why')}\n"
                f"大盤轉弱時本策略不找標的（回測中這道濾網把 2022 年的交易數"
                f"壓到 49 筆，是它沒被拖垮的原因）。")
    lines = [head, f"\n✅ <b>可以進場</b> — {reg.get('why')}"]
    if not setups:
        lines.append("\n今天沒有標的觸發（RSI14 &lt; 30 且站上 200 日均線）。")
        return "\n".join(lines)
    lines.append(f"\n<b>{len(setups)} 檔觸發</b>（超賣回檔，趨勢仍在）")
    for ticker, name, s in setups[:MAX_SHOW]:
        lines.append(
            f"\n<b>{ticker}</b> {name}\n"
            f"<pre>參考 {_px(s['ref'])}  RSI {s['rsi']:.0f}\n"
            f"停損 {_px(s['sl'])}  停利 {_px(s['tp'])}</pre>")
    if len(setups) > MAX_SHOW:
        lines.append(f"\n…另外 {len(setups) - MAX_SHOW} 檔未顯示")
    lines.append(
        "\n進場=次一交易日開盤參考 · 停損 3×ATR · 停利 5×ATR · 最長持有 40 天"
        "\n📊 10 年回測: 勝率 54.4%，每筆 +2.53%，"
        "扣掉「同期隨機日進場」的 +1.19% 後，<b>真實優勢 +1.34%/筆</b>"
        "\n⚠️ 回測偏樂觀（成分股存活者偏差、十年多頭、事後選參數），"
        "實際會比這個數字低。這是觀察名單，不是投資建議。")
    return "\n".join(lines)


def _biz_days_since(date_str: str, now) -> int:
    """Weekday count since a YYYY-MM-DD stamp. Holidays are not modelled — this
    only drives display ageing and the hold cutoff, where being a day or two
    generous is harmless."""
    if not date_str:
        return 0
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return 0
    days, d = 0, d0
    today = now.date()
    while d < today:
        d = d.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            days += 1
    return days


def web_view(now=None) -> dict:
    """Read-only payload for the /us page's entry-setup section.

    Reads the persisted daily scan — NO network of its own — and overlays the
    cached US100 quotes so each setup shows whether price is STILL in a sane
    entry zone. Fail-soft in every branch: the route must never 500 because a
    quote feed hiccuped."""
    now = now or datetime.now(TZ)
    state = _load_state()
    reg = state.get("last_regime") or {}

    prices = {}
    try:
        import stocks_data
        for row in stocks_data.us_quote_rows() or []:
            t = row.get("ticker") or row.get("symbol")
            if t:
                prices[t] = row
    except Exception:  # noqa: BLE001 — live price is a bonus, never required
        prices = {}

    last_run = state.get("last_scan")
    rows = []
    for s in (state.get("active_setups") or []):
        ref, sl, tp = s.get("ref"), s.get("sl"), s.get("tp")
        if not (ref and sl and tp):
            continue
        q = prices.get(s.get("code")) or {}
        price = q.get("price") or q.get("last")
        row = {
            "code": s.get("code"), "name": s.get("name"), "date": s.get("date"),
            "days": _biz_days_since(s.get("date"), now),
            "rsi": s.get("rsi"),
            "ref": ref, "sl": sl, "tp": tp,
            "ref_s": _px(ref), "sl_s": _px(sl), "tp_s": _px(tp),
            "risk_pct": round((ref - sl) / ref * 100, 1),
            "gain_pct": round((tp - ref) / ref * 100, 1),
            "rr": round((tp - ref) / (ref - sl), 1) if ref > sl else None,
            "status": "new" if s.get("date") == last_run else "tracking",
            "price": price, "price_s": _px(price) if price else None,
            "change_pct": q.get("change_pct"),
        }
        if price:
            row["dist_pct"] = round((price - ref) / ref * 100, 1)
            # Reached its level? Said from the CURRENT quote only — there is no
            # intraday path here, so this is "price is now beyond the target",
            # not "the trade filled at it". Without this a setup that resolved
            # days ago keeps sitting on the page as a live idea for 40 sessions.
            if price >= tp:
                row["status"] = "tp"
            elif price <= sl:
                row["status"] = "sl"
            # "Still a valid entry": within a quarter of the stop distance of
            # the reference. Deliberately tight, so the badge means "entering
            # here still matches the tested setup" — not merely "above the stop".
            band = 0.25 * (ref - sl)
            row["buy_zone"] = bool(row["status"] in ("new", "tracking")
                                   and ref - band <= price <= ref + band)
        rows.append(row)

    # Actionable first: still inside its entry band floats to the top, then new,
    # then tracking, with anything already resolved last. Newest date breaks
    # remaining ties (the pre-sort is stable).
    order = {"new": 0, "tracking": 1, "tp": 2, "sl": 2}
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    rows.sort(key=lambda r: (order.get(r["status"], 3), not r.get("buy_zone")))
    return {
        "regime": reg, "setups": rows, "last_scan": last_run,
        "n_zone": sum(1 for r in rows if r.get("buy_zone")),
        "rule": f"RSI14 < {RSI_MAX:g} 且站上 200 日均線",
        "edge": "10 年回測真實優勢 +1.34%/筆（已扣掉同期隨機日進場的 +1.19%）",
    }


# ── scheduling ──────────────────────────────────────────────────────────────
def _due(state: dict, now) -> bool:
    return now.hour >= SEND_HOUR and state.get("last_scan") != now.strftime("%Y-%m-%d")


def scan(limit: int = None) -> tuple:
    """(regime, setups) from live Yahoo data. Network-bound; no state writes."""
    spx = _yahoo_daily("^GSPC")
    reg = regime(spx)
    if not reg.get("ok"):
        return reg, []
    setups = []
    for ticker, name in (US100[:limit] if limit else US100):
        try:
            rows = _yahoo_daily(ticker)
        except Exception:  # noqa: BLE001 — one bad symbol never stops the scan
            continue
        s = setup(rows)
        if s:
            setups.append((ticker, name, s))
        time.sleep(0.12)                 # be a good Yahoo citizen
    setups.sort(key=lambda x: -x[2]["turnover"])
    return reg, setups


def tick(client=None) -> bool:
    """Once per S2 sweep; sends at most one digest a day. Never raises."""
    try:
        now = datetime.now(TZ)
        state = _load_state()
        if not _due(state, now):
            return False
        if time.time() - float(state.get("last_try") or 0) < RETRY_SEC \
                and state.get("last_fail"):
            return False
        state["last_try"] = time.time()
        _save_state(state)

        reg, setups = scan()
        # cooldown: a name that already fired is likely still in its trade
        recent = state.get("recent") or {}
        cutoff = time.time() - COOLDOWN_DAYS * 86400
        recent = {k: v for k, v in recent.items() if v > cutoff}
        fresh = [s for s in setups if s[0] not in recent]

        import telegram_utils
        ok = telegram_utils.send_message(build_digest(now, reg, fresh),
                                         parse_mode="HTML", force=True,
                                         channel="twstocks")
        if not ok:
            state["last_fail"] = True
            _save_state(state)
            return False
        for ticker, _, _ in fresh:
            recent[ticker] = time.time()
        # Keep the setups so /us can show them all day (and for as long as the
        # backtested 40-session hold lasts) instead of only in the one Telegram
        # message that scrolls away.
        active = [s for s in (state.get("active_setups") or [])
                  if _biz_days_since(s.get("date"), now) < MAX_HOLD]
        today = now.strftime("%Y-%m-%d")
        active = [s for s in active if s.get("date") != today]
        for ticker, name, s in fresh:
            active.append({"code": ticker, "name": name, "date": today,
                           "ref": s["ref"], "sl": s["sl"], "tp": s["tp"],
                           "rsi": s["rsi"], "atr": s["atr"]})
        state.update({"last_scan": today, "recent": recent, "last_fail": False,
                      "active_setups": active[-60:], "last_regime": reg})
        _save_state(state)
        print(f"[us-stocks] digest sent — {len(fresh)} setups")
        return True
    except Exception as exc:  # noqa: BLE001 — a watch-only scan never kills the sweep
        print(f"[us-stocks] tick error: {exc}")
        return False


if __name__ == "__main__":  # pragma: no cover — operator tool
    import sys
    n = int(os.getenv("N", "0")) or None
    reg, setups = scan(limit=n)
    print(build_digest(datetime.now(TZ), reg, setups)
          .replace("<b>", "").replace("</b>", "")
          .replace("<pre>", "").replace("</pre>", "").replace("&lt;", "<"))
    if "--send" in sys.argv:
        import telegram_utils
        print("sent:", telegram_utils.send_message(
            build_digest(datetime.now(TZ), reg, setups),
            parse_mode="HTML", force=True, channel="twstocks"))
