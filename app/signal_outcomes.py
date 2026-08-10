"""
Signal outcome tracker — did the alerts actually work? Measured, not felt.

Every fired S2 signal carries an Entry/SL/TP plan, but nothing ever checked
what happened next. This module replays each signal ≥48h old against the
real candles that followed (pessimistic: SL checked before TP inside the
same candle) and records the outcome:

    tp2      final target hit before the stop
    tp1      first target hit, window ended before tp2/sl
    tp1→sl   first target hit, then stopped out (partial winner)
    sl       stopped out first
    none     nothing hit inside the 48h window

A weekly scorecard (Sunday evening) goes to the Signals topic, and /outcomes
answers on demand. This is the honesty loop: if the scorecard says the
high-conviction alerts stop out more than they pay, believe it.

2026-07-16 fix: strategy2_signals.json only RETAINS signals for 24h (it is
the /strategy2 page feed), but evaluation waits 48h — so nothing was EVER
evaluated (signal_outcomes.json didn't exist after 3 days live). Every tick
now SNAPSHOTS new signals into this module's own state first; evaluation
reads the snapshot, not the page feed, so page retention can't starve it.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

STATE_FILE = os.path.join(os.path.dirname(__file__), "signal_outcomes.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")
TZ = ZoneInfo("Asia/Taipei")

WINDOW_H = 48                 # evaluation window after the signal
MIN_AGE_H = 48                # evaluate only once the window is complete
MAX_AGE_D = 10                # too old to fetch candles for — skip
EVAL_PER_TICK = 3             # API-friendly trickle when the queue is short
EVAL_MAX_PER_TICK = 12        # ...but never let a backlog age out unmeasured
RETAIN_D = 30
WEEKLY_HOUR = 20              # Sunday ≥20:00 台北


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── exit-rule laboratory ─────────────────────────────────────────────────────
# The alert publishes ONE plan: hold for TP2, full stop at SL. These are the
# alternatives scored on the SAME candles at the same time, in R (multiples of
# the risk the plan took). Point: pick the exit rule from live results instead
# of opinion. 'hold' is the rule we actually publish — everything is judged
# against it.
EXIT_RULES = ("hold", "be", "partial", "partial_be", "tp1_only", "trail_1r")
BASE_RULE = "hold"

_RULE_ZH = {
    "hold":       "抱到 TP2（現行）",
    "be":         "碰 TP1 後停損移到成本",
    "partial":    "TP1 出一半，其餘照原停損",
    "partial_be": "TP1 出一半，其餘移到成本",
    "tp1_only":   "只做到 TP1 就跑",
    "trail_1r":   "移動停損 1R（讓它跑）",
}


def _new_rules() -> dict:
    return {k: {"open": True, "r": 0.0, "size": 1.0, "booked": 0.0,
                "stop": -1.0, "hit1": False, "peak": 0.0} for k in EXIT_RULES}


def _step(rules: dict, best: float, worst: float, r1: float, r2: float) -> None:
    """Advance every still-open rule by one candle. best/worst are the candle's
    most/least favourable excursions in R. Ordering inside a candle:
      1. the stop — pessimistic, it wins every same-candle tie;
      2. TP1 — nearer than TP2, so price passes through it first;
      3. TP2;
      4. stop TIGHTENING, which only takes effect on the NEXT candle — inside
         one candle you cannot know whether the pullback came before or after
         the touch that moved the stop, so assuming it triggers immediately
         would be circular."""
    for name, st in rules.items():
        if not st["open"]:
            continue
        if worst <= st["stop"]:                                   # 1
            st["r"] = st["booked"] + st["size"] * st["stop"]
            st["open"] = False
            continue
        if best >= r1 and not st["hit1"]:                         # 2
            st["hit1"] = True
            if name in ("partial", "partial_be"):
                st["booked"] += 0.5 * r1        # half off the table at TP1
                st["size"] = 0.5
            elif name == "tp1_only":
                st["r"] = r1
                st["open"] = False
                continue
        if name not in ("trail_1r", "tp1_only") and best >= r2:    # 3
            st["r"] = st["booked"] + st["size"] * r2
            st["open"] = False
            continue
        if name in ("be", "partial_be") and st["hit1"]:            # 4
            st["stop"] = max(st["stop"], 0.0)
        elif name == "trail_1r":
            st["peak"] = max(st["peak"], best)
            st["stop"] = max(st["stop"], st["peak"] - 1.0)


def _settle(rules: dict, adv_close: float) -> dict:
    """Anything still open at the end of the window is marked to the last close
    — counting it as a flat 0R would quietly flatter every rule that dawdles."""
    return {name: round(st["r"] if not st["open"]
                        else st["booked"] + st["size"] * adv_close, 4)
            for name, st in rules.items()}


# ── pure evaluation (unit-tested) ────────────────────────────────────────────
def evaluate(sig: dict, candles: list, window_h: float = WINDOW_H):
    """Outcome of one signal against the candles that FOLLOWED it.
    candles: [(ts_ms, o, h, l, c, v), ...]. Pessimistic: within a candle the
    stop is assumed to hit before any target. None = not resolvable (no plan
    or no candles).

    Also returns 'r1'/'r2' (the plan's geometry) and 'exits' (R per rule in
    EXIT_RULES) whenever the signal carries a usable entry — without those the
    outcome buckets alone can only ever produce a win rate, never expectancy."""
    sl, tp1, tp2 = sig.get("sl"), sig.get("tp1"), sig.get("tp2")
    ts_ms = (sig.get("ts") or 0) * 1000
    if not (sl and tp1 and tp2 and ts_ms):
        return None
    end_ms = ts_ms + window_h * 3600 * 1000
    is_long = sig.get("direction") == "long"

    entry = sig.get("entry")
    risk = abs(entry - sl) if entry else 0.0
    sign = 1.0 if is_long else -1.0

    def adv(p):                       # price → advance in R, sign-corrected
        return sign * (p - entry) / risk

    r1 = r2 = 0.0
    lab = risk > 0
    if lab:
        r1, r2 = adv(tp1), adv(tp2)
        lab = r1 > 0 and r2 > 0       # a plan with targets on the wrong side
    rules = _new_rules() if lab else {}

    tp1_hit = False
    outcome = hours = last_adv = None
    for (t, o, h, l, c, v) in candles:
        if t <= ts_ms:
            continue
        if t >= end_ms:
            break
        stop = l <= sl if is_long else h >= sl
        full = h >= tp2 if is_long else l <= tp2
        part = h >= tp1 if is_long else l <= tp1
        if outcome is None:                       # headline outcome, unchanged
            if stop:
                outcome = "tp1→sl" if tp1_hit else "sl"
                hours = round((t - ts_ms) / 3.6e6, 1)
            elif full:
                outcome = "tp2"
                hours = round((t - ts_ms) / 3.6e6, 1)
            elif part:
                tp1_hit = True
        if not lab:
            if outcome is not None:
                break
            continue
        last_adv = adv(c)
        _step(rules, adv(h if is_long else l), adv(l if is_long else h), r1, r2)

    res = {"outcome": outcome or ("tp1" if tp1_hit else "none"), "hours": hours}
    if lab and last_adv is not None:
        res["r1"], res["r2"] = round(r1, 3), round(r2, 3)
        res["exits"] = _settle(rules, last_adv)
    return res


def expectancy(outcomes: list) -> dict:
    """rule -> {n, exp, total, wr, pf} over every record carrying exit data.
    Expectancy (R per signal) is the number that decides whether a rule makes
    money; win rate is reported alongside precisely because it disagrees."""
    rows = [o["exits"] for o in outcomes if isinstance(o.get("exits"), dict)]
    out: dict = {}
    for name in EXIT_RULES:
        rs = [r[name] for r in rows if isinstance(r.get(name), (int, float))]
        if not rs:
            continue
        n = len(rs)
        gain = sum(r for r in rs if r > 0)
        loss = -sum(r for r in rs if r < 0)
        out[name] = {"n": n, "total": sum(rs), "exp": sum(rs) / n,
                     "wr": 100.0 * sum(1 for r in rs if r > 0) / n,
                     "pf": (gain / loss) if loss > 0 else None}
    return out


def totals_stats(totals: dict) -> dict:
    """Same shape as expectancy(), rebuilt from the lifetime tally."""
    out: dict = {}
    for name in EXIT_RULES:
        t = (totals or {}).get(name) or {}
        n = t.get("n") or 0
        if n <= 0:
            continue
        loss = t.get("loss") or 0.0
        out[name] = {"n": n, "total": t.get("sum") or 0.0,
                     "exp": (t.get("sum") or 0.0) / n,
                     "wr": 100.0 * (t.get("wins") or 0) / n,
                     "pf": ((t.get("gain") or 0.0) / loss) if loss > 0 else None}
    return out


_COHORT_ZH = {"all": "全部", "long": "做多", "short": "做空",
              "hc": "高信心", "premium": "⭐ 精選"}


def cohort_table(totals: dict, rule: str = BASE_RULE,
                 title: str = "📊 哪一種訊號在賺錢") -> str:
    """Expectancy of the CURRENT exit rule, split by signal type. If a cohort
    sold as 'the good ones' has the worst expectancy, that is the finding."""
    rows = [("類型", "單數", "每單", "勝率")]
    body = []
    for key, label in _COHORT_ZH.items():
        t = ((totals or {}).get(key) or {}).get(rule) or {}
        n = t.get("n") or 0
        if n <= 0:
            continue
        exp = (t.get("sum") or 0.0) / n
        wr = 100.0 * (t.get("wins") or 0) / n
        body.append((label, n, f"{exp:+.3f}R", f"{wr:.0f}%"))
    if not body:
        return ""
    import tg_format
    lines = [title, tg_format.pre_table(rows + body, align="lrrr")]
    ranked = [(k, ((totals.get(k) or {}).get(rule) or {}))
              for k in ("hc", "premium") if (totals.get(k) or {}).get(rule)]
    base = (totals.get("all") or {}).get(rule) or {}
    bn = base.get("n") or 0
    for key, t in ranked:
        n = t.get("n") or 0
        if not (n and bn):
            continue
        if (t["sum"] / n) < (base["sum"] / bn):
            lines.append(f"⚠️「{_COHORT_ZH[key]}」的期望值比全部訊號還<b>差</b>"
                         f" — 這層篩選目前沒有加分")
    return "\n".join(lines)


def exit_table(outcomes: list, title: str = "🧪 出場規則實測") -> str:
    """Rank the exit rules by expectancy on real signals. Empty string when
    nothing has been evaluated with exit data yet."""
    return _rule_table(expectancy(outcomes), title)


def _rule_table(stats: dict, title: str) -> str:
    if not stats:
        return ""
    import tg_format
    n = stats.get(BASE_RULE, next(iter(stats.values())))["n"]
    ranked = sorted(stats.items(), key=lambda kv: -kv[1]["exp"])
    rows = [("規則", "每單", "勝率", "PF")]
    for name, s in ranked:
        mark = "▸" if name == BASE_RULE else " "
        pf = f"{s['pf']:.2f}" if s["pf"] is not None else "—"
        rows.append((f"{mark}{_RULE_ZH.get(name, name)}",
                     f"{s['exp']:+.3f}R", f"{s['wr']:.0f}%", pf))
    lines = [title,
             f"同一批 {n} 個訊號、同一批K線，只有出場規則不同（▸=現在在用的）",
             tg_format.pre_table(rows, align="lrrr")]
    best, bs = ranked[0]
    base = stats.get(BASE_RULE)
    if base and base["exp"] < 0 and bs["exp"] <= 0:
        # Ranking six losing rules would read as a recommendation. It isn't one.
        lines.append("⚠️ <b>每一種出場規則都是負的</b> — 問題不在出場，在進場："
                     "這批訊號本身沒有優勢，換出場方式救不回來")
    else:
        if base and best != BASE_RULE and bs["exp"] - base["exp"] >= 0.05:
            lines.append(f"→ 最好的是「{_RULE_ZH.get(best, best)}」，"
                         f"每單比現行多 {bs['exp'] - base['exp']:+.3f}R")
        if base and base["exp"] < 0:
            lines.append("⚠️ 現行規則的期望值是<b>負的</b> — 勝率再高也是在賠錢")
    if n < 100:
        lines.append(f"（只有 {n} 個樣本 — 還不夠下結論，先繼續收）")
    lines.append("（同一天的訊號會一起漲跌 — 樣本數不等於獨立次數，"
                 "要跨過幾種行情才算數）")
    return "\n".join(lines)


def summarize(outcomes: list, title: str = "📋 訊號成績單 (7天)",
              lab: bool = True) -> str:
    """outcomes: [{"outcome", "direction", "score", "base", ...}, ...]
    lab=False drops the exit-rule table (callers that show the lifetime one
    instead, so the same comparison isn't printed twice)."""
    if not outcomes:
        return f"{title}\n尚無已評估的訊號 — 訊號滿48小時後才會結算。"
    import tg_format
    counts: dict = {}
    for o in outcomes:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    n = len(outcomes)
    wins = counts.get("tp2", 0)
    partial = counts.get("tp1", 0) + counts.get("tp1→sl", 0)
    stops = counts.get("sl", 0) + counts.get("tp1→sl", 0)
    rows = [
        ("🎯 到 TP2", wins, f"{100 * wins / n:.0f}%"),
        ("◐ 碰到 TP1", partial, f"{100 * partial / n:.0f}%"),
        ("⚠️ 停損", stops, f"{100 * stops / n:.0f}%"),
        ("➖ 都沒碰到", counts.get("none", 0), ""),
    ]
    hc = [o for o in outcomes if o.get("hc")]
    if hc:
        hw = sum(1 for o in hc if o["outcome"] == "tp2")
        rows.append(("高信心→TP2", f"{hw}/{len(hc)}", f"{100 * hw / len(hc):.0f}%"))
    prem = [o for o in outcomes if o.get("premium")]
    if prem:
        pw = sum(1 for o in prem
                 if o["outcome"] in ("tp2", "tp1", "tp1→sl"))
        rows.append(("⭐ 精選→TP1", f"{pw}/{len(prem)}",
                     f"{100 * pw / len(prem):.0f}%"))
    lines = [title,
             f"共 {n} 個訊號（進場=訊號價 · 48h 窗口 · 同根K線先算停損）",
             tg_format.pre_table(rows, align="lrr")]
    if n < 20:
        lines.append(f"（樣本只有 {n} 個 — 先當參考，別當結論）")
    tbl = exit_table(outcomes) if lab else ""
    if tbl:
        lines += ["", tbl]
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────
_SNAP_KEYS = ("symbol", "base", "direction", "score", "ts",
              "entry", "sl", "tp1", "tp2", "premium")


def _snapshot(state: dict, signals: list, now: float) -> None:
    """Copy new signals into our OWN state so the page feed's 24h retention
    can never starve the 48h evaluation window. Prunes snapshots that are
    already evaluated or too old to ever evaluate."""
    snaps = state.setdefault("signals", {})
    evaluated = state.get("evaluated") or {}
    for s in signals:
        ts = s.get("ts") or 0
        key = f"{s.get('symbol')}:{int(ts)}"
        if key not in snaps and key not in evaluated and s.get("sl"):
            snaps[key] = {k: s.get(k) for k in _SNAP_KEYS if s.get(k) is not None}
    cutoff = now - MAX_AGE_D * 86400
    kept = {k: v for k, v in snaps.items()
            if k not in evaluated and (v.get("ts") or 0) >= cutoff}
    lost = sum(1 for k, v in snaps.items()
               if k not in evaluated and (v.get("ts") or 0) < cutoff)
    if lost:
        # Never let this be silent: a signal that ages out is a signal the
        # scorecard never counted, and they age out in bursts — which biases
        # the sample by exactly the market conditions that cause backlogs.
        print(f"[outcomes] ⚠ {lost} signals aged out unevaluated "
              f"(>{MAX_AGE_D}d) — missing from the scorecard")
    state["signals"] = kept


def cohorts_of(rec: dict) -> list:
    """Which buckets one evaluated signal belongs to. 'Should I keep taking
    this KIND of signal?' is the question with money attached, so the tally is
    kept per cohort and not just in aggregate."""
    out = ["all"]
    if rec.get("direction") in ("long", "short"):
        out.append(rec["direction"])
    if rec.get("premium"):
        out.append("premium")
    if rec.get("hc"):
        out.append("hc")
    return out


def _accumulate(state: dict, res: dict) -> None:
    """Roll one evaluated signal into the lifetime cohort→rule tally. The
    detailed records are pruned after RETAIN_D days — without this, the
    long-run expectancy (the only version of the number with enough samples to
    mean anything) would be thrown away along with them."""
    exits = res.get("exits")
    if not isinstance(exits, dict):
        return
    totals = state.setdefault("totals", {})
    for coh in cohorts_of(res):
        bucket = totals.setdefault(coh, {})
        for name, r in exits.items():
            if not isinstance(r, (int, float)):
                continue
            t = bucket.setdefault(name, {"n": 0, "sum": 0.0, "wins": 0,
                                         "gain": 0.0, "loss": 0.0})
            t["n"] += 1
            t["sum"] = round(t["sum"] + r, 4)
            # Sum of squares — the one figure that makes a real confidence
            # interval possible later. Without it the lifetime tally can say
            # what the average was but never how sure it is, and the per-trade
            # R values are gone the moment RETAIN_D prunes the record. Added
            # 2026-08-10 for /reality; buckets that predate it simply carry a
            # smaller sumsq than n implies, so reality.ci() falls back to its
            # lower-bound basis until a bucket is majority-covered.
            t["sumsq"] = round(t.get("sumsq", 0.0) + r * r, 4)
            t["sumsq_n"] = t.get("sumsq_n", 0) + 1
            if r > 0:
                t["wins"] += 1
                t["gain"] = round(t["gain"] + r, 4)
            else:
                t["loss"] = round(t["loss"] - r, 4)
    state["totals_since"] = state.get("totals_since") or res.get("eval_ts")


def _pending(snaps: dict, evaluated: dict, now: float) -> list:
    """Due signals, oldest first — the ones closest to ageing out go first."""
    out = []
    for key, s in snaps.items():
        if key in evaluated or not s.get("sl"):
            continue
        age_h = (now - (s.get("ts") or 0)) / 3600
        if MIN_AGE_H <= age_h <= MAX_AGE_D * 24:
            out.append((key, s))
    out.sort(key=lambda kv: kv[1].get("ts") or 0)
    return out


def _batch_size(backlog: int) -> int:
    """How many to evaluate this sweep. A flat 3/sweep drained slower than
    signals arrived (~864/day against ~1100/day), so the queue grew until the
    oldest silently aged out. Scale with the backlog instead, capped so a
    burst can't turn into an API storm."""
    if backlog <= EVAL_PER_TICK * 4:
        return EVAL_PER_TICK
    return min(EVAL_MAX_PER_TICK, max(EVAL_PER_TICK, backlog // 50))


def tick(client) -> int:
    """Evaluate a few due signals per sweep; send the Sunday scorecard."""
    now = time.time()
    state = _load_state()
    evaluated = state.setdefault("evaluated", {})
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            signals = (json.load(f) or {}).get("signals") or []
    except Exception:  # noqa: BLE001 — no signal file yet
        signals = []
    before = set(state.get("signals") or {})
    _snapshot(state, signals, now)
    snapped = set(state["signals"]) != before   # new/pruned snaps must persist

    done = 0
    due = _pending(state["signals"], evaluated, now)
    for key, sig in due[:_batch_size(len(due))]:
        try:
            candles = client.call("fetch_ohlcv", sig["symbol"], "15m",
                                  int(sig["ts"] * 1000), 250)
        except Exception:  # noqa: BLE001 — retry on a later sweep
            continue
        res = evaluate(sig, candles or [])
        if res is None:
            res = {"outcome": "none", "hours": None}
        import config
        hc = ((sig.get("direction") == "long"
               and (sig.get("score") or 0) >= config.STRATEGY2_LIVE_MIN_SCORE)
              or (sig.get("direction") == "short"
                  and (sig.get("score") or 100) <= 100 - config.STRATEGY2_LIVE_MIN_SCORE))
        evaluated[key] = {**res, "base": sig.get("base"), "score": sig.get("score"),
                          "direction": sig.get("direction"), "hc": hc,
                          "premium": bool(sig.get("premium")),
                          "sig_ts": sig.get("ts"), "eval_ts": now}
        _accumulate(state, evaluated[key])
        done += 1
        r = (res.get("exits") or {}).get(BASE_RULE)
        print(f"[outcomes] {sig.get('base')} {sig.get('direction')} → "
              f"{res['outcome']}" + (f" ({r:+.2f}R)" if r is not None else ""))

    cutoff = now - RETAIN_D * 86400
    state["evaluated"] = {k: v for k, v in evaluated.items()
                          if (v.get("sig_ts") or 0) >= cutoff}

    local = datetime.now(TZ)
    today = local.strftime("%Y-%m-%d")
    if (local.weekday() == 6 and local.hour >= WEEKLY_HOUR
            and state.get("last_weekly") != today):
        state["last_weekly"] = today
        recent = [v for v in state["evaluated"].values()
                  if (v.get("sig_ts") or 0) >= now - 7 * 86400]
        import telegram_utils
        # Public channel: the scorecard only. The exit-rule research stays on
        # /outcomes for the owner until he decides to publish it.
        telegram_utils.send_message(summarize(recent, lab=False),
                                    parse_mode="HTML",
                                    force=True, channel="signals")
        print(f"[outcomes] weekly scorecard sent ({len(recent)} signals)")

    if done or snapped or state.get("last_weekly") == today:
        _save_state(state)
    return done


def report(owner: bool = False) -> str:
    """/outcomes — the last 7 days. owner=True appends the exit-rule laboratory
    and the cohort breakdown: research about whether the signals are worth
    following at all, which is the owner's to read first and to publish (or
    not) deliberately — it is not something to surprise the group with."""
    state = _load_state()
    now = time.time()
    recent = [v for v in (state.get("evaluated") or {}).values()
              if (v.get("sig_ts") or 0) >= now - 7 * 86400]
    out = summarize(recent, title="📋 訊號成績單 (最近7天)", lab=False)
    if not owner:
        return out
    totals = state.get("totals") or {}
    life = _rule_table(totals_stats(totals.get("all") or {}),
                       "🧪 出場規則實測（全部歷史）")
    if life:
        since = state.get("totals_since")
        if since:
            stamp = datetime.fromtimestamp(since, TZ).strftime("%Y-%m-%d")
            life += f"\n（累計自 {stamp}，不受30天清理影響）"
        out += "\n\n" + life
    coh = cohort_table(totals)
    if coh:
        out += "\n\n" + coh
    return out
