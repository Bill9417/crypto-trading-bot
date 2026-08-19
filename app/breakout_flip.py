"""
🚀 Resistance→Support flip — the "broke out, retested, nothing above it" setup.

Asked for on 2026-08-13 off a MYX chart: price clears a resistance zone, comes
back to it, the old ceiling holds as a floor, and then it runs because there is
nothing overhead to stop it. Four separate conditions, and the setup is only
that setup when all four are true:

  1. ZONE     a price band that rejected price repeatedly — not one high
  2. BREAK    a CLOSE above it, not a wick through it
  3. FLIP     price returns INTO the band and closes back above it
  4. CLEAR    no prior swing high overhead for the move to run into

Condition 4 is the one the request was really about ("it will fly so high due
to no resistance"), and it is the one a normal breakout scanner leaves out.
A break into the middle of an old range has a ceiling 2% up; the same break
into blue sky does not. Same pattern, different thing.

LOOKAHEAD IS THE WHOLE DIFFICULTY HERE, in three separate places, and each one
would make this look far better than it is:

  · A swing high at bar i is not KNOWN at bar i. It needs `right` bars to its
    right to be confirmed a pivot, so it only exists from bar i+right onward.
    Building zones from unconfirmed pivots means drawing resistance using bars
    that had not printed yet.
  · The forming candle is not a candle. strategy2_scanner patches a live price
    onto the last row, so `ohlcv[-1]` is a partial bar whose high/low/close all
    still move. Everything here works on CLOSED bars only.
  · The confirming bar cannot fill its own trade. The flip is read at that
    bar's close, so entry is the NEXT bar — measurement uses the following bar
    onward, exactly like strategy4_outcomes.

NOT A VALIDATED STRATEGY. It detects a shape. Whether that shape pays is a
separate question with a separate answer, measured by measure() below and
reported honestly — this repo has already found 46 of 48 high-win-rate setups
losing money, and "it looks right on the chart" is precisely the evidence that
finding invalidates.
"""
import os

# ── shape parameters ─────────────────────────────────────────────────────────
# A pivot needs this many bars either side. Bigger = fewer, more meaningful
# levels, and `right` is also how long the level takes to become knowable.
PIVOT_LEFT = int(os.getenv("BFLIP_PIVOT_LEFT", "3"))
PIVOT_RIGHT = int(os.getenv("BFLIP_PIVOT_RIGHT", "3"))

# Pivots within this % of each other are the same zone. A "level" that price
# hit at 0.0750 and 0.0754 is one zone, not two.
ZONE_TOL_PCT = float(os.getenv("BFLIP_ZONE_TOL_PCT", "0.8"))

# A zone needs this many touches to count as resistance. One high is a high;
# resistance is a price that has rejected more than once.
MIN_TOUCHES = int(os.getenv("BFLIP_MIN_TOUCHES", "2"))

# The break must CLOSE this far above the zone top — a wick through and a close
# back inside is a failed break, and it is the most common outcome.
BREAK_MARGIN_PCT = float(os.getenv("BFLIP_BREAK_MARGIN_PCT", "0.3"))

# The retest must come back within this much of the zone. Wider and any
# pullback counts; narrower and only a perfect touch does.
RETEST_TOL_PCT = float(os.getenv("BFLIP_RETEST_TOL_PCT", "0.6"))

# How long after the break the flip may happen, and how fresh the flip must be
# to still be worth reporting.
MAX_BARS_TO_RETEST = int(os.getenv("BFLIP_MAX_BARS_TO_RETEST", "24"))
MAX_BARS_SINCE_FLIP = int(os.getenv("BFLIP_MAX_BARS_SINCE_FLIP", "4"))

# Overhead is "clear" when the nearest confirmed swing high above is at least
# this far up — or when there is none at all in the window (blue sky).
CLEAR_OVERHEAD_PCT = float(os.getenv("BFLIP_CLEAR_OVERHEAD_PCT", "4"))

LOOKBACK = int(os.getenv("BFLIP_LOOKBACK", "300"))


# ── pivots ───────────────────────────────────────────────────────────────────
def swing_highs(highs: list, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT,
                known_by: int = None) -> list:
    """[(index, price)] of confirmed swing highs.

    `known_by` is the anti-lookahead lever: pass a bar index and only pivots
    already CONFIRMED at that bar are returned (a pivot at i needs bars up to
    i+right, so it is invisible until then). Omitting it returns every pivot in
    the series, which is correct for a chart and wrong for a decision.
    """
    out = []
    n = len(highs)
    for i in range(left, n - right):
        if known_by is not None and i + right > known_by:
            break
        h = highs[i]
        if all(h >= highs[j] for j in range(i - left, i)) and \
           all(h > highs[j] for j in range(i + 1, i + right + 1)):
            out.append((i, h))
    return out


def cluster_zones(pivots: list, tol_pct: float = ZONE_TOL_PCT) -> list:
    """Group nearby pivot highs into zones, strongest (most touches) first.

    Clustered on price rather than on time: a level that rejected price in
    March and again in August is the same wall, and treating those as two
    one-touch levels would disqualify the very thing that makes it resistance.
    """
    zones = []
    for idx, price in sorted(pivots, key=lambda p: p[1]):
        placed = False
        for z in zones:
            if abs(price - z["price"]) / z["price"] * 100 <= tol_pct:
                z["prices"].append(price)
                z["idxs"].append(idx)
                z["price"] = sum(z["prices"]) / len(z["prices"])
                z["top"] = max(z["prices"])
                z["bottom"] = min(z["prices"])
                placed = True
                break
        if not placed:
            zones.append({"price": price, "top": price, "bottom": price,
                          "prices": [price], "idxs": [idx]})
    for z in zones:
        z["touches"] = len(z["prices"])
        z["last_idx"] = max(z["idxs"])
    zones.sort(key=lambda z: (-z["touches"], -z["last_idx"]))
    return zones


def overhead_room(highs: list, price: float, upto: int,
                  left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT,
                  before: int = None, since: int = 0) -> dict:
    """How much clear air is above `price`.

    {'blue_sky': bool, 'nearest': float|None, 'room_pct': float|None}

    blue_sky means no confirmed swing high above at all inside the window —
    the "nothing left to stop it" case. Everything is measured on pivots
    confirmed by bar `upto`, so a high printed after the decision cannot
    retroactively become the ceiling that was in the way.

    `before` excludes pivots at or after that index, and it is REQUIRED for
    this pattern rather than optional. A breakout always prints a new local
    high, and three bars later that high is a confirmed pivot sitting above the
    retest price — so without this the move's own spike counts as the ceiling
    blocking it and essentially every genuine flip is disqualified by itself.
    Overhead resistance means resistance that existed BEFORE the break.

    `since` is the SAME window the zone search uses, and passing it is not
    optional either. Without it this scanned from bar 0 while detect() limited
    zones to `at - LOOKBACK`, so a swing high from outside the detector's own
    lookback could veto a flip — and whether it existed depended entirely on
    how many candles the caller happened to pass in. Same bar, same lookback,
    different verdict: Q/USDT fired on a 400-bar array and was silent on the
    750-bar array production actually uses (2026-08-16). Three call sites meant
    three different detectors sharing one set of measured numbers.
    """
    pivots = swing_highs(highs[:upto + 1], left, right, known_by=upto)
    pivots = [(i, p) for i, p in pivots if i >= since]
    if before is not None:
        pivots = [(i, p) for i, p in pivots if i < before]
    above = [p for _, p in pivots if p > price]
    if not above:
        return {"blue_sky": True, "nearest": None, "room_pct": None}
    nearest = min(above)
    return {"blue_sky": False, "nearest": nearest,
            "room_pct": (nearest - price) / price * 100}


# ── the pattern ──────────────────────────────────────────────────────────────
def detect(ohlcv: list, at: int = None, **kw) -> dict:
    """Is bar `at` the confirmation of a resistance→support flip?

    `at` is the index of the last CLOSED bar to consider. Everything is decided
    from data at or before it. Returns {} when the shape is absent, otherwise
    the anatomy of it.
    """
    p = {"pivot_left": PIVOT_LEFT, "pivot_right": PIVOT_RIGHT,
         "zone_tol": ZONE_TOL_PCT, "min_touches": MIN_TOUCHES,
         "break_margin": BREAK_MARGIN_PCT, "retest_tol": RETEST_TOL_PCT,
         "max_bars_to_retest": MAX_BARS_TO_RETEST,
         "max_bars_since_flip": MAX_BARS_SINCE_FLIP,
         "clear_overhead": CLEAR_OVERHEAD_PCT, "lookback": LOOKBACK, **kw}

    if not ohlcv or len(ohlcv) < 60:
        return {}
    at = (len(ohlcv) - 1) if at is None else at
    if at < 50 or at >= len(ohlcv):
        return {}

    lo_i = max(0, at - p["lookback"])
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]
    closes = [float(c[4]) for c in ohlcv]
    price = closes[at]

    # 1. ZONES — only pivots confirmed by `at`, only zones now BELOW price.
    pivots = [(i, h) for i, h in
              swing_highs(highs, p["pivot_left"], p["pivot_right"], known_by=at)
              if i >= lo_i]
    zones = [z for z in cluster_zones(pivots, p["zone_tol"])
             if z["touches"] >= p["min_touches"] and z["top"] < price]
    if not zones:
        return {}

    best = None
    for z in zones:
        top = z["top"]
        need = top * (1 + p["break_margin"] / 100)

        # 2. BREAK — first CLOSE above the zone after the zone's last touch.
        brk = None
        for i in range(z["last_idx"] + 1, at + 1):
            if closes[i] >= need:
                brk = i
                break
        if brk is None:
            continue

        # 3. FLIP — a later bar dips back INTO the zone and closes above it.
        tol = top * (1 + p["retest_tol"] / 100)
        flip = None
        for i in range(brk + 1, min(brk + 1 + p["max_bars_to_retest"], at + 1)):
            if lows[i] <= tol and closes[i] > top:
                flip = i
                break
            if closes[i] < z["bottom"]:
                break               # fell back through — the break failed
        if flip is None or (at - flip) > p["max_bars_since_flip"]:
            continue

        # 4. CLEAR — what is left overhead, as known at `at`.
        # `before=brk`: prior structure only. The break's own spike high is
        # part of this move, not a wall in front of it.
        room = overhead_room(highs, price, at, p["pivot_left"], p["pivot_right"],
                             before=brk, since=lo_i)
        clear = room["blue_sky"] or (room["room_pct"] or 0) >= p["clear_overhead"]
        if not clear:
            continue

        cand = {"zone_top": top, "zone_bottom": z["bottom"],
                "touches": z["touches"], "break_idx": brk, "flip_idx": flip,
                "bars_since_flip": at - flip, "price": price,
                "break_pct": (closes[brk] - top) / top * 100,
                **room, "at": at, "ts": int(ohlcv[at][0])}
        # Prefer the zone with the most touches; ties go to the more recent
        # flip, which is the fresher piece of information.
        if best is None or (cand["touches"], cand["flip_idx"]) > \
                           (best["touches"], best["flip_idx"]):
            best = cand
    return best or {}


def plan(sig: dict, atr: float = None) -> dict:
    """Entry / stop / target for a detected flip.

    The stop belongs BELOW the flipped zone, because the zone holding is the
    entire premise — if price closes back inside it, the setup did not merely
    stall, it was wrong. That is a structural stop, not a percentage picked to
    make the R look good.
    """
    if not sig:
        return {}
    entry = sig["price"]
    sl = sig["zone_bottom"] * (1 - 0.15 / 100)
    risk = entry - sl
    if risk <= 0:
        return {}
    return {"entry": round(entry, 8), "sl": round(sl, 8),
            "tp": round(entry + 2 * risk, 8), "rr": 2.0,
            "stop_pct": round(risk / entry * 100, 2),
            "side": "long"}


# ── alerting ─────────────────────────────────────────────────────────────────
# MEASURED 2026-08-13 on real 15m candles: 60 symbols × ~10.4 days, 163 flips.
#   all              n=163  WR 39.3%  +0.081R  CI[-0.12,+0.29]  PF 1.14
#   blue sky only    n=140  WR 41.4%  +0.137R  CI[-0.08,+0.36]  PF 1.26
#   ceiling <4% away n= 23  WR 26.1%  -0.261R  (tiny sample)
#
# Every interval straddles zero, so the honest reading is INCONCLUSIVE, not
# "slightly profitable" — and it is one ~10-day window, i.e. ONE regime, which
# is exactly the shape of evidence that made S1 look fine before it failed
# walk-forward. Do NOT tune these thresholds on those 163 trades; that is how
# the 46-of-48 losing setups in this repo were built.
#
# The blue-sky split matching the original intuition is encouraging and is not
# proof: with n=140 and that interval it cannot be distinguished from noise.
MEASURED = {"n": 163, "wr": 39.3, "exp": 0.081, "ci": (-0.12, 0.29), "pf": 1.14,
            "blue_n": 140, "blue_exp": 0.137, "blue_ci": (-0.08, 0.36),
            "window": "60 symbols × ~10.4 days, one regime"}

# ── OUT-OF-SAMPLE re-measurement, 2026-08-15 ────────────────────────────────
# A second, non-overlapping window: ~29 days × 68 symbols, fresh candles, same
# detector, same plan(), same strategy4_outcomes.settle as production.
#
#   flip alone, production universe   n=192  WR 41.1%  +0.169R ±0.199  PF 1.30
#
# So the interval STILL straddles zero — the original n=163 verdict holds and
# the pattern remains a hypothesis. Two things worth knowing before anyone
# reads that +0.169 as encouraging:
#
#  · CONCENTRATION. Top 3 of 40 symbols carry 74% of the total R. Removing the
#    single best symbol drops it to +0.122R ±0.205. An "edge" that one symbol
#    can delete is a sample artefact until proven otherwise.
#  · THE BLUE-SKY SPLIT DID NOT REPLICATE. Its most interesting original claim
#    (blue sky +0.137 vs ceiling −0.261) inverts here: blue-sky-only scores
#    +0.106R against +0.169R for all flips. Do not gate on blue_sky.
#
# Including TradFi perps would have read n=323 +0.170R with the CI EXCLUDING
# zero — a false positive produced entirely by symbols production never trades
# (EXCLUDE_TRADFI_PERPS). Any future measurement here must filter the universe
# to what actually gets alerted.
# RE-MEASURED 2026-08-16 after the overhead_room window bug. Every number
# above described a detector that vetoed a flip whenever a swing high from
# OUTSIDE its own lookback sat overhead — which silently killed 47% of the
# fires (192 -> 359 on the identical window and universe). The old figures are
# left in place because they are what the shipped alerts were scored against,
# but they do not describe what runs now.
MEASURED_OOS = {"n": 359, "wr": 38.7, "exp": 0.116, "ci": (-0.03, 0.26),
                "pf": 1.20, "top3_share": 0.82,
                "drop_best_exp": 0.080, "drop_best_ci": (-0.07, 0.23),
                "window": "68 symbols × ~29 days, 2026-07→08, post-fix",
                "pre_fix": {"n": 192, "exp": 0.169}}

# ── OI confirmation: MEASURED, and it does NOT work ─────────────────────────
# Asked for directly ("resistance to support and OI should have a bonus flag").
# Tested on the sample above, where every flip carries the OI reading the crowd
# radar would have computed for it (same SPAN_BARS, same percentile method):
#
#   flip alone                 n=192  +0.169R ±0.199
#   + OI percentile >=80       n= 93  −0.072R ±0.276     WORSE than baseline
#   + OI percentile >=90       n= 63  −0.033R ±0.339     WORSE than baseline
#   + OI percentile >=95       n= 39  +0.221R ±0.456     n too small, CI huge
#   + OI merely rising         n=123  +0.270R ±0.257
#
# The percentile ladder is NOT monotonic (−0.072 → −0.033 → +0.221 → −0.009 at
# 98). A real effect strengthens as the gate tightens; this wanders, which is
# the signature of noise being sliced.
#
# The one cut that looked alive — "OI merely rising" — fails every robustness
# check that matters:
#   drop best symbol   +0.213R ±0.265   straddles zero
#   drop top-3 symbols +0.129R ±0.284   straddles zero
#   LIFT vs the flips it REJECTS: +0.281R ±0.402 — indistinguishable from zero,
#   and picked-minus-unpicked is the only comparison that judges a FILTER.
#
# Roughly eight variants were tried; one clearing p<0.05 by chance is expected.
# So OI is attached to the alert as CONTEXT and gates nothing. If it ever earns
# the right to be a gate, that will be because flip_outcomes scored it forward,
# not because this table was re-sliced.
MEASURED_OI = {"lift": 0.281, "lift_ci": (-0.12, 0.68), "verdict": "no effect",
               "tested": "pctile 80/90/95/98, rising, radar floor"}

# ── the SEQUENCE the owner trades (measured 2026-08-16) ─────────────────────
# "long triangle, then it breaks resistance, the old resistance holds as
# support, nothing overhead, and it flies." Measured as an ordered sequence on
# 29 days x 68 symbols, production universe, fixed detector — 269 flips:
#
#   all flips                       n=269  +0.145R +/-0.168
#   + blue sky                      n=203  +0.145R          <- adds NOTHING
#     (ceiling overhead)            n= 66  +0.146R          <- identical
#   + a LONG triangle first         n=142  +0.216R
#     (no triangle)                 n=127  +0.067R
#   TRIANGLE + FLIP + BLUE SKY      n=108  +0.287R +/-0.264  CI excludes 0
#     ... + OI rising               n= 81  +0.372R +/-0.311  CI excludes 0
#
# Two things worth being blunt about:
#
#  · "上面沒有壓力" contributes NOTHING on its own — blue sky scores +0.145R
#    against +0.146R for flips WITH a ceiling. The triangle is what carries the
#    sequence. Blue sky is kept in the tier because it is what the owner is
#    looking at and it costs nothing, not because it earns anything.
#  · The tier is not proven BETTER than the flips it excludes. Lift over the
#    rejected ones is +0.237R +/-0.342 — indistinguishable — and it fails every
#    robustness check: drop the best symbol and it straddles zero, drop the top
#    three and it is +0.135R, and the second half of the sample straddles zero.
#    Top 3 of 37 symbols carry 59% of the profit.
#
# So this is a TIER, not a verdict: rarer (3.7/day across 68 coins), cleaner to
# look at, and shipped with these numbers attached.
MEASURED_SEQ = {"n": 108, "wr": 44.4, "exp": 0.287, "ci": (0.02, 0.55),
                "pf": 1.57, "with_oi_n": 81, "with_oi_exp": 0.372,
                "lift": 0.237, "lift_ci": (-0.11, 0.58),
                "blue_alone": 0.145, "ceiling_alone": 0.146,
                "drop_top3_exp": 0.135, "per_day": 3.7,
                "window": "68 symbols x ~29 days, 2026-07->08"}


COOLDOWN_SEC = float(os.getenv("BFLIP_COOLDOWN_SEC", "14400"))


def format_alert(sym: str, sig: dict, pl: dict) -> str:
    """House style — see tg_format. HTML, so the sender MUST pass
    parse_mode='HTML' (strategy4 shipped without it and Telegram printed the
    tags as text)."""
    import tg_format as F
    import crowd_radar as CR
    OI_ZH = CR.READ_ZH
    base = sym.split("/")[0]
    room = ("上方無壓（前高已全部突破）" if sig["blue_sky"]
            else f"最近壓力還有 {sig['room_pct']:.1f}%")
    rows = [("進場", F.fmt_price(pl["entry"])),
            ("停損", f"{F.fmt_price(pl['sl'])}  −{pl['stop_pct']:.2f}%"),
            ("目標", f"{F.fmt_price(pl['tp'])}  {pl['rr']:g}R"),
            ("翻轉區", f"{F.fmt_price(sig['zone_bottom'])}–"
                       f"{F.fmt_price(sig['zone_top'])}"),
            ("測試次數", f"{sig['touches']} 次"),
            ("上方空間", room)]
    # OI CONTEXT — never a gate. Measured (MEASURED_OI): the lift over the
    # flips an OI filter would have REJECTED is +0.281R ±0.402, i.e. nothing,
    # and tightening the percentile made it worse rather than better. It is
    # shown because it is real information about who is positioned, and it is
    # labelled 參考 so it is not read as confirmation.
    oi = sig.get("oi") or {}
    if oi.get("oi_pct") is not None:
        rows.append(("持倉量 2h", f"{oi['oi_pct']:+.1f}%"
                                  f"{'  第%d百分位' % round(oi['pctile']) if oi.get('pctile') is not None else ''}"))
        if oi.get("state"):
            rows.append(("資金動向", OI_ZH.get(oi["state"], oi["state"])))
    # The two readings the owner reads off the chart anyway. Shown so the alert
    # answers the question instead of prompting a trip to TradingView; NOT a
    # gate, and the line below says so with the numbers.
    ctx = sig.get("context") or {}
    # `is not None`, not truthiness. Bare .get() cannot tell False from absent,
    # so a context dict missing these keys rendered "在訊號線下方・柱狀轉弱" —
    # an unmade measurement printed as an assertion, which is the exact
    # anti-pattern the comment above this block was written to avoid.
    if ctx.get("macd_above") is not None:
        macd = "在訊號線上方" if ctx["macd_above"] else "在訊號線下方"
        if ctx.get("macd_rising") is not None:
            macd += "・柱狀轉強" if ctx["macd_rising"] else "・柱狀轉弱"
        rows.append(("MACD", macd))
    if ctx.get("vol_bar_mult") is not None:
        rows.append(("本根量", f"{ctx['vol_bar_mult']:.1f}× "
                               f"（前 {ctx.get('vol_base_bars', VOL_BASE_BARS)} 根均量）"))
    # Which bar the readings describe. A flip up to MAX_BARS_SINCE_FLIP bars
    # old still alerts, so "9.9×" can belong to a bar an hour after the
    # breakout — and the climax-bar reasoning below only applies to the
    # breakout candle itself.
    if sig.get("bars_since_flip"):
        rows.append(("距翻轉", f"{sig['bars_since_flip']} 根前翻轉，上面數字是最新那根"))
    return "\n".join(x for x in [
        F.headline("⭐🚀 壓力翻支撐 · 完整型態" if sig.get("full_setup")
                   else "🚀 壓力翻支撐", base, "做多 LONG"),
        # The tier the owner actually trades: triangle first, then the flip,
        # then nothing overhead. Stated with its numbers because it is the one
        # cut here that measures better than the rest AND still fails every
        # robustness check — see MEASURED_SEQ.
        ("⭐ 三角訊號 → 突破回踩 → 上方無壓，三個條件都到齊。\n"
         f"實測 {MEASURED_SEQ['n']} 筆：勝率 {MEASURED_SEQ['wr']:.0f}%、"
         f"期望值 {MEASURED_SEQ['exp']:+.2f}R (PF {MEASURED_SEQ['pf']})；"
         f"但跟其他翻轉相比只差 {MEASURED_SEQ['lift']:+.2f}R ±"
         f"{MEASURED_SEQ['lift_ci'][1] - MEASURED_SEQ['lift']:.2f}"
         "（統計上分不出來），拿掉最賺的一檔幣就掉回沒把握的範圍。"
         if sig.get("full_setup") else ""),
        F.pre_table(rows),
        f"\n突破後回踩 {sig['zone_top']:.6g} 沒破，舊壓力變新支撐。",
        # The numbers are stated because the alert is the only place they will
        # be read, and an unqualified setup alert reads as a recommendation.
        # STATED FIRST and stated plainly. The message used to recite a +0.08R
        # backtest and a +0.12R re-test and never mention that the live
        # forward book — the only number here that is not a replay — is
        # negative. Three neutral-to-positive headline expectancies and no
        # baseline is not an honest summary of what this pattern has done.
        (f"🔴 <b>實盤紀錄（唯一非回測的數字）</b>：{MEASURED_LIVE['n']} 筆已結算，"
         f"期望值 <b>{MEASURED_LIVE['exp']:+.2f}R</b> ±{MEASURED_LIVE['se']:.2f}、"
         f"勝率 {MEASURED_LIVE['wr']:.0f}%、獲利因子 {MEASURED_LIVE['pf']:.2f} —— "
         f"目前<b>是虧的</b>，而且「上方無壓」那半邊更差（{MEASURED_LIVE['blue_exp']:+.2f}R "
         f"vs 有壓 {MEASURED_LIVE['ceiling_exp']:+.2f}R）。截至 {MEASURED_LIVE['asof']}。"),
        f"📐 以下是回測。實測 {MEASURED['n']} 筆：勝率 {MEASURED['wr']:.0f}%、"
        f"期望值 {MEASURED['exp']:+.2f}R，信賴區間 "
        f"[{MEASURED['ci'][0]:+.2f}, {MEASURED['ci'][1]:+.2f}] <b>仍然包含 0</b>。",
        # Same reason as the OI note: showing a number invites reading it as
        # confirmation, and here the measurement says the opposite.
        (f"📉 MACD 跟成交量也只是<b>參考</b>。實測 {MEASURED_CONTEXT['n']} 筆："
         f"MACD 在訊號線上方的有 {MEASURED_CONTEXT['macd_above']['n']} 筆"
         "（等於這個型態本來就會成立，不是第二個意見）；成交量 ≥3× 的反而更差 "
         f"{MEASURED_CONTEXT['vol_3x']['exp']:+.2f}R；三個條件<b>全部到齊</b>的最差 "
         f"{MEASURED_CONTEXT['all_three']['exp']:+.2f}R"
         f"（差距 {MEASURED_CONTEXT['all_three']['lift']:+.2f}R "
         f"±{MEASURED_CONTEXT['all_three']['lift_se']:.2f}，統計上確定更差）。"
         "爆量的那根通常是高潮棒。"
         if sig.get("context") else ""),
        # Stated because the OI row above invites exactly this inference.
        ("📊 持倉量只是<b>參考</b>，不是加分條件 —— 實測加上 OI 條件後，"
         "跟沒加的差距是 +0.28R ±0.40（等於沒差別），把門檻調更嚴反而更差。"
         if (sig.get("oi") or {}).get("oi_pct") is not None else ""),
        # Second, independent window. Reported because "we tested it again and
        # it still straddles zero" is a stronger warning than the first alone.
        f"🔁 另一段 29 天、68 檔的獨立重測："
        f"{MEASURED_OOS['n']} 筆、期望值 {MEASURED_OOS['exp']:+.2f}R，"
        f"<b>信賴區間一樣包含 0</b>；而且獲利集中在 3 檔幣"
        f"（占 {MEASURED_OOS['top3_share']*100:.0f}%），拿掉最好的一檔就掉到 "
        f"{MEASURED_OOS['drop_best_exp']:+.2f}R。",
        "⚠️ 兩段行情都測不出穩定優勢 —— 這是「形態偵測」，不是已驗證的策略。"
        "本專案量過 48 組高勝率設定有 46 組在賠錢。自己判斷，不會自動下單。",
        F.bybit_line(base),
    ] if x)


TRIANGLE_WINDOW_SEC = float(os.getenv("BFLIP_TRIANGLE_WINDOW_SEC", str(6 * 3600)))


def consider(sym: str, ohlcv: list, state: dict, now: float,
             recent_long_ts: float = None) -> dict:
    """One symbol, using candles the caller already has. {} unless it fired.

    The FORMING candle is dropped: strategy2_scanner patches a live price onto
    the last row, so its high/low/close are still moving and a flip 'confirmed'
    on it can un-confirm itself two minutes later.

    `recent_long_ts` is when this symbol last fired a LONG triangle, if inside
    TRIANGLE_WINDOW_SEC. Triangle-then-flip-into-blue-sky is the sequence the
    owner actually trades, and it is the one cut in this module that measures
    better than the rest — see MEASURED_SEQ. The caller passes it because the
    scanner already has that history; recomputing it here would be ~24 extra
    compute_signal calls per flip for a fact already on disk.
    """
    closed = ohlcv[:-1] if ohlcv else []
    if len(closed) < 60:
        return {}
    last = (state.get("last") or {}).get(sym)
    if last and now - last < COOLDOWN_SEC:
        return {}
    sig = detect(closed)
    if not sig:
        return {}
    pl = plan(sig)
    if not pl:
        return {}
    state.setdefault("last", {})[sym] = now
    out = {"symbol": sym, "base": sym.split("/")[0], **sig, "plan": pl,
           "oi": oi_context(sym, closed)}
    out["triangle_ts"] = recent_long_ts
    out["full_setup"] = bool(recent_long_ts) and bool(sig.get("blue_sky"))
    out["context"] = confirm_context(closed)
    return out


# The volume baseline window, as ONE number. It was two independent literals
# (a 97-slice and a /96) for a single quantity, which is how a window change
# breaks an average silently. 96 bars only means "24h" on 15m candles, so the
# timeframe is named here rather than assumed by the label.
VOL_BASE_BARS = int(os.getenv("BFLIP_VOL_BASE_BARS", "96"))
VOL_BASE_LABEL = "24h"

# Measured 2026-08-19 on the live flip record, 348 replayable alerts 08-13→18.
# A dict rather than literals in the alert string: every other measured claim
# here is one (MEASURED, MEASURED_OOS, MEASURED_OI, MEASURED_SEQ) precisely so
# the alert and the dashboard cannot quote different numbers.
MEASURED_CONTEXT = {
    "n": 348,
    "macd_above": {"n": 329, "exp": -0.147, "lift": 0.063, "lift_se": 0.320},
    "macd_rising": {"n": 264, "exp": -0.183, "lift": -0.135, "lift_se": 0.172},
    "vol_3x": {"n": 133, "exp": -0.255, "lift": -0.168, "lift_se": 0.144},
    "all_three": {"n": 107, "exp": -0.354, "lift": -0.293, "lift_se": 0.145},
}

# The LIVE forward record for the flip itself, as of 2026-08-19 — 397 settled
# alerts. Stated because it is the only non-replayed number the alert has, and
# without it the message recited a +0.08R backtest and a +0.12R re-test while
# the actual forward book was negative.
MEASURED_LIVE = {"n": 397, "exp": -0.180, "se": 0.066, "wr": 27.7, "pf": 0.75,
                 "blue_exp": -0.219, "ceiling_exp": -0.070, "asof": "2026-08-19"}


def confirm_context(closed: list, at: int = None) -> dict:
    """MACD and volume AT THE DECISION BAR. CONTEXT, not a gate.

    `at` mirrors detect()'s parameter and exists for the same reason: this
    module's whole header is about lookahead, and a replay that paired
    detect(hist, at=i) with a context read from hist[-1] would compute the
    reading from bars that had not printed at decision time. The volume ratio
    is the flattering one on a runner, so that mistake would make any
    re-measurement look far better than reality. Defaults to the last CLOSED
    bar, which is what consider() decides on.

    NOTE ON WHICH BAR. This is the bar the signal is stamped at, not
    necessarily the breakout candle — detect() accepts a flip up to
    MAX_BARS_SINCE_FLIP bars old, so they can be up to an hour apart on 15m.
    The alert prints 距翻轉 alongside, because "9.9x volume" means something
    different if it is not the breakout bar.

    Asked for on 2026-08-19 off a TRIA chart that ran +19% ("MACD plus volume
    is up"). Both were then measured on the live flip record — 348 replayable
    alerts — and neither helps; requiring all three is significantly WORSE.
    See MEASURED_CONTEXT. The reason is mechanical rather than statistical: a
    flip is a close above resistance, so MACD is above its signal on 329 of
    348 of them — a restatement of the setup, not a second opinion. And a
    3x-volume flip is the climax bar. TRIA fired on 9.86x against a 2.20x
    median, which is why it is memorable and also why it is not evidence.

    So these are SHOWN and STORED, never required.
    """
    # The WHOLE body is guarded, like oi_context(). Only the macd_series call
    # used to be, which left the volume arithmetic exposed: one candle with a
    # null volume raised out of here, out of consider(), and into the
    # scanner's blanket handler — but consider() has ALREADY stamped the
    # cooldown by then, so the alert was lost and the symbol stayed suppressed
    # for COOLDOWN_SEC. A context read must never be able to cost the alert it
    # annotates, which is what the old comment claimed and the old scope did
    # not deliver.
    try:
        end = len(closed) - 1 if at is None else int(at)
        window = closed[:end + 1]
        if len(window) < 120 or len(window) < VOL_BASE_BARS + 2:
            return {}                   # not measured — never a neutral default
        closes = [float(c[4]) for c in window]
        vols = [float(c[5]) for c in window]
        import strategy4
        macd_line, sig_line = strategy4.macd_series(closes)
        if not macd_line or len(macd_line) < 3:
            return {}
        hist = [a - b for a, b in zip(macd_line, sig_line, strict=False)]
        base_win = vols[-(VOL_BASE_BARS + 1):-1]
        base = sum(base_win) / len(base_win) if len(base_win) == VOL_BASE_BARS else 0
        return {"macd_above": macd_line[-1] > sig_line[-1],
                "macd_rising": hist[-1] > hist[-2],
                # NOT the same quantity as the Pump Radar's vol_mult, which is
                # last-HOUR volume over an hourly average. Same 3x figure, ~4x
                # apart in scale; distinct name so nothing ever joins them.
                "vol_bar_mult": round(vols[-1] / base, 2) if base > 0 else None,
                "vol_base_bars": VOL_BASE_BARS}
    except Exception as exc:  # noqa: BLE001 — annotation must not cost the alert
        # Printed because this can only fire on a genuine defect now. Silent
        # abstention here would drop both rows and the whole caveat paragraph,
        # and the only symptom would be a slightly shorter alert — the same
        # shape as the drift that once produced three days of zero signals.
        print(f"[flip] confirm_context failed: {exc}")
        return {}


def oi_context(sym: str, closed: list) -> dict:
    """Who is positioned behind this flip. CONTEXT ONLY — see MEASURED_OI.

    One extra API call, and only on a fire: flips are rare (a handful per
    sweep), so this cannot become the rate-limit incident this repo already
    had. Any failure returns {} — a missing reading must never cost the alert,
    and it must never be filled with a 0 that reads as "OI did not move".
    """
    try:
        import crowd_radar as CR
        # Both series come from the SAME rows, so they cannot misalign. Pairing
        # OI against candle closes would silently offset the two whenever the
        # OI endpoint skips a bar.
        oi_series, px_series = CR.oi_history(
            sym.replace("/", "").replace(":USDT", ""))
        if len(oi_series) < CR.SPAN_BARS + 2:
            return {}
        a = CR.assess(oi_series, px_series)
        return {k: a.get(k) for k in ("oi_pct", "px_pct", "pctile", "state")}
    except Exception:  # noqa: BLE001 — context is a bonus, never a blocker
        return {}
