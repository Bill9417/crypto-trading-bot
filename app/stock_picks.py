"""
🎯 立即可進場 Top 5 — 台股 and 美股, ranked by how actionable they are NOW.

Both scans already produce everything this needs: tw_stocks.web_view() and
us_stocks.web_view() return the same field names (ref / sl / tp / price /
dist_pct / buy_zone / status), each with its own live quote already attached.
So this ranks work already done and fetches nothing.

WHAT "INSTANT" MEANS, AND WHAT IT REFUSES TO MEAN
────────────────────────────────────────────────
A setup is actionable when price is still inside the entry band both scans
define the same way: within a quarter of the stop distance of the pullback
reference. That is deliberately strict — it means "entering HERE still matches
the setup that was measured", not "the stock is somewhere above its stop".

THE LIST IS ALLOWED TO BE EMPTY, and most days it will be. 25 tracked setups
with none in their band is the normal state of a pullback strategy: the whole
point is waiting for price to come to you. Padding the list to five with
候選 that are 8% from entry would turn "top 5 opportunities" into "five stocks",
which is the one thing a page called 好進場點 must not do. Anything outside the
band is returned separately, labelled with how far away it is and what it is
waiting for.

THE MEASURED EDGE TRAVELS WITH EACH LIST, because the two markets do NOT share
one. The 台股 rules were ported to 美股 and MEASURED rather than assumed, and
they did not transfer: US needed its own rule (RSI14 < 30 above the 200-day),
and the low-volatility filter that helps 台股 actively HURTS on US.
"""
import os

TOP_N = int(os.getenv("STOCK_PICKS_TOP_N", "5"))
# How far outside the entry band a setup may be and still be worth listing as
# "coming up". Past this it is not a near-miss, it is a different trade.
WATCH_MAX_PCT = float(os.getenv("STOCK_PICKS_WATCH_PCT", "4.0"))


def _rank(rows: list) -> tuple:
    """(ready, watching) — open setups only, most actionable first."""
    open_rows = [r for r in (rows or [])
                 if r.get("status") in ("new", "tracking") and r.get("price")]
    ready, watch = [], []
    for r in open_rows:
        dist = r.get("dist_pct")
        if r.get("buy_zone"):
            ready.append(r)
        elif dist is not None and abs(dist) <= WATCH_MAX_PCT:
            watch.append(r)
    # Closest to the reference first. A setup sitting exactly on its entry is
    # more actionable than one 3% above it, and "new" breaks ties because a
    # fresh signal has more of its holding window left.
    key = lambda r: (abs(r.get("dist_pct") or 99), r.get("status") != "new")
    ready.sort(key=key)
    watch.sort(key=key)
    return ready, watch


def _row(r: dict, market: str) -> dict:
    """One pick, with the reason it is a pick."""
    dist = r.get("dist_pct")
    why = []
    if r.get("rsi") is not None:
        why.append(f"RSI {r['rsi']:.0f}")
    if r.get("risk_pct") is not None:
        why.append(f"風險 {r['risk_pct']:.1f}%")
    if r.get("rr"):
        why.append(f"風報比 1:{r['rr']}")
    if r.get("days") is not None or r.get("held") is not None:
        why.append(f"第 {r.get('days', r.get('held'))} 天")
    return {
        "market": market,
        "code": r.get("code"), "name": r.get("name"),
        "price": r.get("price"), "price_s": r.get("price_s"),
        "ref": r.get("ref"), "ref_s": r.get("ref_s"),
        "sl_s": r.get("sl_s"), "tp_s": r.get("tp_s"),
        "dist_pct": dist,
        "in_zone": bool(r.get("buy_zone")),
        # The one sentence that says what to do about it.
        "verdict": ("現在就在進場價附近" if r.get("buy_zone")
                    else (f"還要等 —— 距進場 {dist:+.1f}%" if dist is not None
                          else "等回踩")),
        "why": " · ".join(why),
        "status": r.get("status"),
    }


SESSIONS = {"tw": ("Asia/Taipei", (9, 0), (13, 30), "09:00–13:30 台北"),
            "us": ("America/New_York", (9, 30), (16, 0), "09:30–16:00 紐約")}


def _session(market: str) -> dict:
    """Clock only, and deliberately NO network. stocks_data._in_session is the
    same helper the /stocks board uses, so the two cannot disagree about when a
    market is open — but calling its snapshot would fire a quote fetch, and the
    whole point of this module is that it ranks work already done.

    The clock cannot see holidays. It does not need to: `blind` below is what
    actually decides, and a clock-open market with no prices is reported as
    exactly that, which is what 中秋 and July 4th look like."""
    tz, o, c, label = SESSIONS.get(market, (None, None, None, None))
    if not tz:
        return {}
    try:
        import stocks_data
        return {"state": "open" if stocks_data._in_session(tz, o, c) else "closed",
                "hours": label}
    except Exception:  # noqa: BLE001
        return {"hours": label}


def _market(mod_name: str, market: str, label: str) -> dict:
    try:
        mod = __import__(mod_name)
        v = mod.web_view() or {}
    except Exception as exc:  # noqa: BLE001 — one market must never break the other
        return {"market": market, "label": label, "ready": [], "watching": [],
                "error": str(exc)[:120], "open_count": 0}
    rows = v.get("setups") or []
    ready, watch = _rank(rows)
    sess = _session(market)
    priced = sum(1 for r in rows if r.get("price"))
    # THE DISTINCTION THAT MATTERS. "Nothing is near entry" and "we cannot see
    # any prices" produce the identical empty list, and only one of them is
    # information. Overnight every dist_pct is None, so without this the page
    # would report no opportunities every night for the wrong reason — the same
    # shape as a dead feed reading as a quiet market.
    blind = bool(rows) and priced == 0
    return {
        "market": market, "label": label,
        "ready": [_row(r, market) for r in ready[:TOP_N]],
        # Only enough to fill the list — the page is not a second scan report.
        "watching": [_row(r, market) for r in watch[:max(0, TOP_N - len(ready))]],
        "open_count": sum(1 for r in rows if r.get("status") in ("new", "tracking")),
        # Each market's OWN numbers. They do not share an edge: the 台股 rules
        # were measured on US data and did not transfer.
        "rule": v.get("rule") or v.get("strategy_rule"),
        "edge": v.get("edge") or v.get("measured"),
        "regime_ok": v.get("regime_ok", (v.get("regime") or {}).get("ok")),
        "regime_why": (v.get("regime") or {}).get("why"),
        "market_state": sess.get("state"),
        "hours": sess.get("hours"),
        "blind": blind,
        "priced": priced,
    }


def picks() -> dict:
    """Both markets, ready to render."""
    return {
        "tw": _market("tw_stocks", "tw", "🇹🇼 台股"),
        "us": _market("us_stocks", "us", "🇺🇸 美股"),
        "band": "進場區 = 距離參考價 ¼ 個停損距離內，代表「現在進場仍符合當初測過的設定」",
    }


def as_text(market: str = None) -> str:
    """/twbuy and /usbuy — the same content in one Telegram message."""
    p = picks()
    out = []
    for key in (("tw", "us") if market is None else (market,)):
        m = p.get(key) or {}
        out.append(f"\n{m.get('label', key)} · 立即可進場")
        if m.get("error"):
            out.append("  讀取失敗，稍後再試")
            continue
        if m.get("blind"):
            out.append(f"  🌙 {m.get('hours') or '收盤時間'}休市中，沒有即時報價 —— "
                       f"追蹤中 {m.get('open_count', 0)} 檔，開盤後才能判斷有沒有到價。")
            if m.get("edge"):
                out.append(f"  📐 {m['edge']}")
            continue
        if m.get("regime_ok") is False:
            out.append("  ⚠️ 大盤條件不符，策略今天不出手"
                       + (f"（{m['regime_why']}）" if m.get("regime_why") else ""))
        ready = m.get("ready") or []
        if ready:
            for i, r in enumerate(ready, 1):
                out.append(f"{i}. {r['name']} {r['code']} — {r['verdict']}")
                out.append(f"   現價 {r.get('price_s') or '—'} · 進場 "
                           f"{r.get('ref_s') or '—'} · 停損 {r.get('sl_s') or '—'} "
                           f"· 目標 {r.get('tp_s') or '—'}")
                if r["why"]:
                    out.append(f"   {r['why']}")
        else:
            # Said plainly. An empty list is the honest answer on most days and
            # the strategy's whole method — padding it would be the bug.
            out.append(f"  目前沒有任何一檔在進場區（追蹤中 {m.get('open_count', 0)} 檔）"
                       "—— 等它跌到價位才有意義，不是每天都有。")
        watch = m.get("watching") or []
        if watch:
            out.append("  接近中：" + "、".join(
                f"{w['name']}({w['dist_pct']:+.1f}%)" for w in watch))
        if m.get("edge"):
            out.append(f"  📐 {m['edge']}")
    out.append("\n⚠️ 只是掃描結果，不是投資建議。自己判斷。")
    return "\n".join(out).strip()
