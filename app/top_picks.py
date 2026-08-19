"""
🏆 Top 3 買 / 賣 — every scanner in this repo, on one list, with its reasons.

WHAT THIS RANKS BY, AND WHY IT IS NOT A SCORE
---------------------------------------------
Agreement COUNT, not a weighted prediction. Every engine here has been measured
and not one of them has a confirmed edge:

    S2 triangle      -0.081R +/-0.018 over 22,631 signals   CI excludes 0
    壓力翻支撐        +0.145R +/-0.168 over 269 flips        straddles 0
      ⭐ full setup   +0.287R +/-0.264 over 108              fails robustness
    S4 setup         -0.022R +/-0.282 over 90               straddles 0
    OI 異常           lift +0.28R +/-0.40 as a filter         no effect
    Mover            -0.054R +/-0.087; SHORTS -0.211R        CI excludes 0

Multiplying numbers like those into a "confidence score" would manufacture a
precision none of them have, and the ordering would look authoritative while
resting on nothing. So the rank is: how many INDEPENDENT engines point the same
way right now, ties broken by how recently they said it.

That is a statement about agreement, which is true, rather than about
probability, which is not known. Each row carries the measured record of every
engine that voted for it, so the reason and the caveat arrive together.

DIRECTION HANDLING. 壓力翻支撐 is long-only by construction. Movers vote, but
their SHORT side is the one thing here measured confidently negative, so it is
recorded and labelled rather than counted — see MOVER_SHORT_NOTE.

NO NETWORK. Everything is read from the state files the scanners already
write, so this is a view over work already done, not another sweep.
"""
import json
import os
import time

_DIR = os.path.dirname(__file__)

# How stale a vote may be and still count. A 15m signal from nine hours ago is
# not a current opinion, it is history.
MAX_AGE = {
    "s2": float(os.getenv("PICKS_S2_AGE_H", "6")) * 3600,
    "flip": float(os.getenv("PICKS_FLIP_AGE_H", "4")) * 3600,
    "oi": float(os.getenv("PICKS_OI_AGE_H", "2")) * 3600,
    "s4": float(os.getenv("PICKS_S4_AGE_H", "6")) * 3600,
    "mover": float(os.getenv("PICKS_MOVER_AGE_H", "2")) * 3600,
}
TOP_N = int(os.getenv("PICKS_TOP_N", "3"))

# The measured record of each voter, shown next to whatever it claims.
# The engine's NAME is not in here — SRC_ZH supplies it, and the legend prints
# the two together. Carrying it in both produced "動能 動能實測 −0.054R…".
RECORD = {
    "s2": "實測 −0.081R/筆 (22,631 筆，信賴區間不含 0 → 確定為負)",
    "flip": "實盤 −0.18R/筆 (397 筆已結算，目前是虧的)",
    # Both flip lines lead with the LIVE settled record, which is NEGATIVE.
    # Storing full_setup (2026-08-19) woke a branch that had been dead since
    # it was written, so ⭐ rows silently began advertising a +0.287R backtest
    # cut that the module itself says fails every robustness check — roughly
    # double the claim, in the wrong direction, as a side effect of a
    # storage fix.
    "flip_full": "實盤 −0.18R/筆 (397 筆)；⭐ 這一段回測 +0.287R 但 108 筆、拿掉最賺的一檔就失效",
    "s4": "實測 −0.022R/筆 (90 筆，信賴區間含 0)",
    "oi": "當篩選條件實測沒有效果 (+0.28R ±0.40)",
    "mover": "實測 −0.054R/筆；做空 −0.211R (確定為負)",
}
MOVER_SHORT_NOTE = ("動能做空是本專案唯一「確定會賠」的訊號 "
                    "(−0.211R，信賴區間不含 0)，所以只列出、不計票。")

# Short chip labels — the row shows WHICH engines voted at a glance; the full
# sentence for each lives once in the legend.
SRC_ZH = {"s2": "S2", "flip": "翻轉", "oi": "OI", "s4": "S4", "mover": "動能",
          "flip_full": "⭐ 完整型態"}

OI_BULL = {"longs_opening": "新多單進場", "shorts_closing": "空單回補"}
OI_BEAR = {"shorts_opening": "新空單進場", "longs_closing": "多單平倉"}


def _load(name: str) -> dict:
    try:
        with open(os.path.join(_DIR, name), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _fresh(ts, kind: str, now: float) -> bool:
    return bool(ts) and (now - float(ts)) <= MAX_AGE[kind]


def collect(now: float = None) -> dict:
    """{base: {"long": [vote…], "short": [vote…]}} from every live source.

    A vote is {"src", "why", "record", "ts", "counts"}. `counts` is False for
    anything shown but deliberately not scored.
    """
    now = now if now is not None else time.time()
    votes: dict = {}

    def add(base, side, src, why, record, ts, counts=True, plan=None):
        if not base:
            return
        slot = votes.setdefault(base, {"long": [], "short": []})
        slot[side].append({"src": src, "why": why, "record": record,
                           "ts": ts, "counts": counts, "plan": plan})

    # ── S2 triangle ────────────────────────────────────────────────────────
    for s in (_load("strategy2_signals.json").get("signals") or []):
        if not _fresh(s.get("ts"), "s2", now):
            continue
        conv = s.get("score") if s.get("direction") == "long" else 100 - (s.get("score") or 50)
        tag = "⭐ " if s.get("premium") else ""
        why = f"{tag}S2 三角訊號 · 信心 {conv}/100"
        if s.get("against"):
            why += "（逆勢：與 BTC 方向相反）"
        plan = None
        if s.get("sl") and s.get("tp2"):
            plan = {"entry": s.get("entry") or s.get("price"),
                    "sl": s["sl"], "tp": s["tp2"]}
        add(s.get("base"), s.get("direction"), "s2", why, RECORD["s2"],
            s.get("ts"), plan=plan)

    # ── 壓力翻支撐 (long only by construction) ──────────────────────────────
    seen = set()
    for r in (_load("flip_outcomes.json").get("recent") or []):
        b = r.get("base")
        if b in seen or not _fresh(r.get("fired_ts"), "flip", now):
            continue
        seen.add(b)
        full = bool(r.get("full_setup"))
        room = ("上方無壓" if r.get("blue_sky")
                else f"上方壓力 {r.get('room_pct') or 0:.1f}%")
        why = ("⭐ 壓力翻支撐 · 完整型態（三角→翻轉→無壓）" if full
               else "壓力翻支撐 · 突破回踩不破") + f" · {room}"
        plan = ({"entry": r.get("entry"), "sl": r.get("sl"), "tp": r.get("tp")}
                if r.get("sl") else None)
        add(b, "long", "flip", why,
            RECORD["flip_full"] if full else RECORD["flip"],
            r.get("fired_ts"), plan=plan)

    # ── OI 異常 ─────────────────────────────────────────────────────────────
    for r in (_load("crowd_radar_state.json").get("recent") or []):
        if not _fresh(r.get("ts"), "oi", now):
            continue
        st = r.get("state")
        side = "long" if st in OI_BULL else "short" if st in OI_BEAR else None
        if not side:
            continue
        zh = OI_BULL.get(st) or OI_BEAR.get(st)
        pct = r.get("pctile")
        why = (f"OI 異常 · {zh} · 持倉 {r.get('oi_pct', 0):+.1f}%"
               + (f" · 前 {max(0.1, 100 - pct):.1f}%" if pct is not None else ""))
        add((r.get("symbol") or "").replace("USDT", ""), side, "oi", why,
            RECORD["oi"], r.get("ts"))

    # ── S4 setups ──────────────────────────────────────────────────────────
    for s in (_load("strategy4_signals.json").get("signals") or []):
        if not _fresh((s.get("bar_ts") or 0) / 1000, "s4", now):
            continue
        srcs = "+".join(s.get("div_sources") or []) or "背離"
        why = f"S4 · {srcs} 背離 + 結構位 · 品質 {s.get('quality', '—')}"
        pl = s.get("plan") or {}
        plan = ({"entry": pl.get("entry"), "sl": pl.get("sl"), "tp": pl.get("tp")}
                if pl.get("sl") else None)
        add(s.get("base"), s.get("side") or "long", "s4", why, RECORD["s4"],
            (s.get("bar_ts") or 0) / 1000, plan=plan)

    # ── movers ─────────────────────────────────────────────────────────────
    for m in (_load("strategy2_signals.json").get("movers") or []):
        if not _fresh(m.get("ts"), "mover", now):
            continue
        up = (m.get("chg_1h") or 0) >= 0
        side = "long" if up else "short"
        why = (f"動能 · 一小時 {m.get('chg_1h', 0):+.1f}% · "
               f"成交量 {m.get('vol_mult', 0):.1f}× 平常")
        # Counted long, listed-not-counted short — see MOVER_SHORT_NOTE.
        add(m.get("base"), side, "mover", why, RECORD["mover"], m.get("ts"),
            counts=up)
    return votes


def rank(votes: dict = None, now: float = None, top_n: int = None) -> dict:
    """Top N each way. Ordered by how many independent engines agree."""
    votes = collect(now) if votes is None else votes
    now = now if now is not None else time.time()
    top_n = TOP_N if top_n is None else top_n

    def build(side):
        rows = []
        for base, v in votes.items():
            mine = v[side]
            counted = [x for x in mine if x["counts"]]
            if not counted:
                continue
            other = len([x for x in v["long" if side == "short" else "short"]
                         if x["counts"]])
            newest = max((x["ts"] or 0) for x in mine)
            # ONE line per engine. Two S2 signals on a coin is one engine
            # saying the same thing twice, and printing both made a row look
            # like it had more behind it than it did (WET and CTSI each showed
            # "S2 三角訊號" twice while the agree count correctly said 1).
            # Newest wins; the rest collapse into a ×N marker.
            per_src, extra = {}, {}
            for x in sorted(mine, key=lambda x: -(x["ts"] or 0)):
                if x["src"] in per_src:
                    extra[x["src"]] = extra.get(x["src"], 1) + 1
                else:
                    per_src[x["src"]] = x
            for s, n in extra.items():
                per_src[s] = {**per_src[s], "repeats": n}
            rows.append({
                "base": base, "side": side,
                "agree": len({x["src"] for x in counted}),
                # Engines pointing the OTHER way at the same time. Shown, not
                # subtracted: a coin two engines like and one dislikes is a
                # different situation from one nobody contradicts, and hiding
                # that would make the list look cleaner than the data is.
                "conflict": other,
                "reasons": sorted(per_src.values(), key=lambda x: -(x["ts"] or 0)),
                "srcs": sorted({x["src"] for x in counted}),
                "plan": next((x["plan"] for x in counted if x["plan"]), None),
                "newest_ts": newest,
                "age_min": (now - newest) / 60 if newest else None,
                "starred": any("⭐" in x["why"] for x in counted),
                # EVERY contributing engine's record, deduped. Showing only the
                # newest one attached whichever caveat happened to arrive last
                # — a row carried by S2 could display the mover's numbers and
                # look like it had been vouched for by something it never was.
                "records": list(dict.fromkeys(x["record"] for x in mine)),
            })
        rows.sort(key=lambda r: (-r["agree"], -r["starred"], r["conflict"],
                                 -(r["newest_ts"] or 0)))
        return rows[:top_n]

    buy, sell = build("long"), build("short")
    # The records move to ONE legend instead of repeating under every row. Six
    # identical italic lines is how a caveat becomes wallpaper: the reader stops
    # seeing it, which is the opposite of why it is there. Stated once, keyed to
    # the chip on each row, it stays readable AND stays attached.
    used = []
    for r in buy + sell:
        for s in r["srcs"]:
            if s not in used:
                used.append(s)
        # The ⭐ tier has its OWN numbers (+0.287R over 108, and failing every
        # robustness check). A starred row under the plain flip record would be
        # quoting the wrong figure at the row most likely to be acted on.
        if r["starred"] and "flip_full" not in used:
            used.append("flip_full")
    return {"buy": buy, "sell": sell,
            "ts": now, "mover_short_note": MOVER_SHORT_NOTE,
            "legend": [{"src": s, "label": SRC_ZH.get(s, s), "record": RECORD[s]}
                       for s in used if s in RECORD],
            "basis": "依「幾個獨立訊號同時指向同一邊」排序，不是預測機率"}


def as_text(picks: dict = None) -> str:
    """The /picks Telegram reply — same content, one message."""
    p = picks or rank()
    out = ["🏆 綜合前三名 · 買 / 賣", f"— {p['basis']} —"]
    for side, title in (("buy", "🟢 買進候選"), ("sell", "🔴 放空候選")):
        rows = p[side]
        out.append(f"\n{title}")
        if not rows:
            out.append("  目前沒有任何訊號")
            continue
        for i, r in enumerate(rows, 1):
            out.append(f"{i}. <b>{r['base']}</b> · {r['agree']} 個訊號同時指向"
                       + (f" · ⚠️ 另有 {r['conflict']} 個反向" if r["conflict"] else ""))
            for why in r["reasons"]:
                rep = f" ×{why['repeats']}" if why.get("repeats") else ""
                out.append(f"   · {why['why']}{rep}")
    if p.get("legend"):
        out.append("\n📐 各引擎實測（每次都一樣，所以只列一次）")
        for lg in p["legend"]:
            out.append(f"   {lg['label']}：{lg['record']}")
    out.append("\n⚠️ 排序=幾個訊號同時同向，不是勝率也不是預測。"
               "本專案每一個引擎實測都沒有確定的優勢，數字附在各列下方。")
    return "\n".join(out)
