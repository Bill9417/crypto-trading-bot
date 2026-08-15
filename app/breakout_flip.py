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
                  before: int = None) -> dict:
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
    """
    pivots = swing_highs(highs[:upto + 1], left, right, known_by=upto)
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
                             before=brk)
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
MEASURED_OOS = {"n": 192, "wr": 41.1, "exp": 0.169, "ci": (-0.03, 0.37),
                "pf": 1.30, "top3_share": 0.74,
                "drop_best_exp": 0.122, "drop_best_ci": (-0.08, 0.33),
                "window": "68 symbols × ~29 days, 2026-07→08"}

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
    return "\n".join(x for x in [
        F.headline("🚀 壓力翻支撐", base, "做多 LONG"),
        F.pre_table(rows),
        f"\n突破後回踩 {sig['zone_top']:.6g} 沒破，舊壓力變新支撐。",
        # The numbers are stated because the alert is the only place they will
        # be read, and an unqualified setup alert reads as a recommendation.
        f"📐 實測 {MEASURED['n']} 筆：勝率 {MEASURED['wr']:.0f}%、"
        f"期望值 {MEASURED['exp']:+.2f}R，信賴區間 "
        f"[{MEASURED['ci'][0]:+.2f}, {MEASURED['ci'][1]:+.2f}] <b>仍然包含 0</b>。",
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


def consider(sym: str, ohlcv: list, state: dict, now: float) -> dict:
    """One symbol, using candles the caller already has. {} unless it fired.

    The FORMING candle is dropped: strategy2_scanner patches a live price onto
    the last row, so its high/low/close are still moving and a flip 'confirmed'
    on it can un-confirm itself two minutes later.
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
    return {"symbol": sym, "base": sym.split("/")[0], **sig, "plan": pl,
            "oi": oi_context(sym, closed)}


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
