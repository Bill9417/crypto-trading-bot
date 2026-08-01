"""Which strategy owned which Bybit position — recorded when it OPENS.

S1 (the mirror), S3 (the flip engine) and the operator's own manual trades all
share one Bybit sub-account. Bybit's closed-pnl endpoint reports a single net
number per closed position with no idea who asked for it, so every account-wide
total silently added the three together — the "80% win rate, +38 USDT" headline
turned out to be ~90% manual stock-perp trades, with S1 and S3 contributing a
few dollars between them. Nothing recorded ownership because
s1_bybit_positions.json drops a position's row the moment it closes.

So: an append-only interval log — (strategy, symbol, opened, closed) — written
at the moment a bot opens a position and closed out when it exits. A closed
trade is then attributed by asking which interval covers its close time.

Every write is fail-soft. A ledger line is bookkeeping; losing one must never
interfere with a real order.
"""
import json
import os
import time

LEDGER_FILE = os.path.join(os.path.dirname(__file__), "strategy_ledger.json")
RETAIN_D = 400                  # keep ~a year of attribution history
GRACE_S = 900                   # exchange close time vs our own can differ

STRATEGIES = ("S1", "S3")
MANUAL = "manual"
UNKNOWN = "unknown"

# Order sizes S1's Bybit mirror used in the PAST, newest first. A closed
# trade's size fingerprint is whatever was configured when it was placed, so
# inference has to know the history — see infer()'s docstring. Append the old
# value here whenever S1_BYBIT_ORDER_USDT changes.
PAST_S1_ORDER_USDT = (100.0,)     # 100 → 50 on 2026-07-29


def norm(symbol: str) -> str:
    """'PENGU/USDT:USDT' | 'PENGU/USDT' | 'PENGUUSDT' → 'PENGUUSDT'."""
    s = str(symbol or "").upper().split(":")[0].replace("/", "")
    return s or ""


def _load() -> dict:
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = start fresh
        return {}


def _save(state: dict) -> None:
    tmp = LEDGER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, LEDGER_FILE)


def _prune(rows: list, now: float) -> list:
    cutoff = now - RETAIN_D * 86400
    return [r for r in rows if (r.get("closed") or r.get("opened") or 0) >= cutoff]


# ── recording (called from the execution paths) ──────────────────────────────
def record_open(strategy: str, symbol: str, side: str = "",
                ts: float = None) -> None:
    """Note that `strategy` just opened `symbol`. Safe to call twice."""
    try:
        now = ts or time.time()
        sym = norm(symbol)
        if not sym:
            return
        state = _load()
        rows = _prune(state.get("rows") or [], now)
        for r in rows:                       # already open → leave it alone
            if r.get("symbol") == sym and not r.get("closed"):
                return
        rows.append({"strategy": strategy, "symbol": sym, "side": side or "",
                     "opened": round(now, 3), "closed": None})
        state["rows"] = rows
        _save(state)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never blocks a trade
        print(f"[ledger] record_open failed {symbol}: {exc}")


def record_close(strategy: str, symbol: str, ts: float = None,
                 by: str = None) -> None:
    """Note that `strategy`'s position on `symbol` is flat.

    `by` records WHO ended it — the strategy itself (default) or MANUAL when
    the position was closed by hand on the exchange and the bot merely
    reconciled afterwards. Ownership at OPEN is not the whole story: on
    2026-07-28 the mirror opened ATOM and AVAX and the operator closed both by
    hand hours later, so S1's record silently absorbed somebody else's exit
    decision — ATOM ran to S1's TP1 on paper (+4.0%) but realised ~+0.7%. Any
    expectancy computed over a mixture of the two is measuring neither.
    """
    try:
        now = ts or time.time()
        sym = norm(symbol)
        if not sym:
            return
        state = _load()
        rows = _prune(state.get("rows") or [], now)
        for r in reversed(rows):
            if r.get("symbol") == sym and not r.get("closed"):
                r["closed"] = round(now, 3)
                r["closed_by"] = by or strategy
                break
        state["rows"] = rows
        _save(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[ledger] record_close failed {symbol}: {exc}")


def exit_kind(symbol: str, close_ts: float, rows: list = None):
    """'S1' | 'S3' | 'manual' for the interval covering `close_ts`, or None.

    Rows written before closed_by existed report the owning strategy, which is
    the old (optimistic) assumption — `clean_only` below is what excludes them
    from a strategy's honest record."""
    sym = norm(symbol)
    rows = rows if rows is not None else (_load().get("rows") or [])
    best = None
    for r in rows:
        if r.get("symbol") != sym:
            continue
        opened = r.get("opened") or 0
        closed = r.get("closed")
        if close_ts < opened - GRACE_S:
            continue
        if closed is not None and close_ts > closed + GRACE_S:
            continue
        if best is None or opened > (best.get("opened") or 0):
            best = r
    if not best:
        return None
    return best.get("closed_by") or best.get("strategy")


def intervention_stats(strategy: str = None, rows: list = None) -> dict:
    """How many of a strategy's CLOSED positions it actually got to finish.

    A high manual share means the live P&L is measuring the operator, not the
    strategy — report it next to any win rate rather than burying it."""
    rows = rows if rows is not None else (_load().get("rows") or [])
    out = {"total": 0, "by_strategy": 0, "manual": 0, "unknown": 0}
    for r in rows:
        if not r.get("closed"):
            continue
        if strategy and r.get("strategy") != strategy:
            continue
        out["total"] += 1
        by = r.get("closed_by")
        if by is None:
            out["unknown"] += 1          # predates closed_by — not assumed clean
        elif by == MANUAL:
            out["manual"] += 1
        else:
            out["by_strategy"] += 1
    out["manual_pct"] = (round(out["manual"] / out["total"] * 100, 1)
                         if out["total"] else 0.0)
    return out


# ── attribution ──────────────────────────────────────────────────────────────
def owner_of(symbol: str, close_ts: float, rows: list = None):
    """Strategy that owned the position on `symbol` closing at `close_ts`,
    or None when the ledger has no opinion (it wasn't running yet, or a human
    opened the trade)."""
    sym = norm(symbol)
    rows = rows if rows is not None else (_load().get("rows") or [])
    best = None
    for r in rows:
        if r.get("symbol") != sym:
            continue
        opened = r.get("opened") or 0
        closed = r.get("closed")
        if close_ts < opened - GRACE_S:
            continue
        if closed is not None and close_ts > closed + GRACE_S:
            continue
        if best is None or opened > (best.get("opened") or 0):
            best = r
    return best.get("strategy") if best else None


def infer(symbol: str, notional: float, leverage: float) -> str:
    """Fallback for trades that closed BEFORE the ledger existed. Inferred from
    the size/leverage fingerprint each engine leaves — S1 mirrors at a fixed
    ~100 USDT notional on ≤10x, S3 only ever touches XAUT. It is a guess and is
    labelled as one: it already mis-filed a real S1 position that happened to be
    mid-scale-out, which is exactly why the ledger above exists.

    2026-07-26: SPCX (a Bybit stock perp the owner scalps by hand) landed
    inside S1's notional/leverage window by coincidence and got tagged S1.
    config.MANUAL_ONLY_SYMBOLS is checked FIRST and wins outright — those
    symbols are TradFi tickers S1/S3 cannot structurally trade
    (EXCLUDE_TRADFI_PERPS), so this is a correctness override, not a guess.

    2026-07-29: S1_BYBIT_ORDER_USDT was lowered 100 → 50. Matching ONLY the
    current setting silently re-filed every historical ~100 USDT S1 trade as
    'manual' — the exact attribution corruption this module exists to
    prevent. Past order sizes are therefore matched too (PAST_S1_ORDER_USDT):
    a trade's fingerprint is whatever the size was WHEN IT WAS PLACED, and
    that history doesn't change just because today's config did. Add the old
    value here whenever the order size changes again."""
    import config
    sym = norm(symbol)
    base = sym[:-4] if sym.endswith("USDT") else sym
    if base in getattr(config, "MANUAL_ONLY_SYMBOLS", ()):
        return MANUAL
    s3_syms = {norm(s) + "USDT" if not norm(s).endswith("USDT") else norm(s)
               for s in (getattr(config, "STRATEGY3_SYMBOLS", None) or [])}
    if sym in s3_syms:
        return "S3"
    lev = float(getattr(config, "S1_BYBIT_LEVERAGE", 10) or 10)
    if notional and leverage <= lev:
        current = float(getattr(config, "S1_BYBIT_ORDER_USDT", 100.0) or 100.0)
        for want in (current, *PAST_S1_ORDER_USDT):
            if 0.9 * want <= notional <= 1.1 * want:
                return "S1"
    return MANUAL


def attribute(trades: list) -> list:
    """Tag each grouped closed trade with 'strategy' and 'attrib'
    ('recorded' | 'inferred'). Trades carry symbol, time (ms), pnl, and —
    when available — notional and lev."""
    rows = _load().get("rows") or []
    out = []
    for t in trades:
        ts = (t.get("time") or 0) / 1000.0
        who = owner_of(t.get("symbol") or "", ts, rows)
        if who:
            out.append({**t, "strategy": who, "attrib": "recorded"})
        else:
            out.append({**t, "attrib": "inferred",
                        "strategy": infer(t.get("symbol") or "",
                                          t.get("notional") or 0.0,
                                          t.get("lev") or 0.0)})
    return out


def split_summary(trades: list) -> dict:
    """strategy -> {n, wins, losses, win_rate, net, gross_win, gross_loss,
    profit_factor, avg, best, worst, recorded, inferred}."""
    out: dict = {}
    for t in attribute(trades):
        s = out.setdefault(t["strategy"], {
            "n": 0, "wins": 0, "losses": 0, "net": 0.0, "gross_win": 0.0,
            "gross_loss": 0.0, "best": 0.0, "worst": 0.0,
            "recorded": 0, "inferred": 0, "symbols": set()})
        p = float(t.get("pnl") or 0.0)
        s["n"] += 1
        s["net"] += p
        s["symbols"].add(norm(t.get("symbol") or "").replace("USDT", ""))
        s[t["attrib"]] += 1
        if p > 0:
            s["wins"] += 1
            s["gross_win"] += p
        elif p < 0:
            s["losses"] += 1
            s["gross_loss"] -= p
        s["best"] = max(s["best"], p)
        s["worst"] = min(s["worst"], p)
    for s in out.values():
        decided = s["wins"] + s["losses"]
        s["win_rate"] = round(100.0 * s["wins"] / decided, 1) if decided else 0.0
        s["profit_factor"] = (round(s["gross_win"] / s["gross_loss"], 2)
                              if s["gross_loss"] > 0 else None)
        s["avg"] = round(s["net"] / s["n"], 4) if s["n"] else 0.0
        for k in ("net", "gross_win", "gross_loss", "best", "worst"):
            s[k] = round(s[k], 4)
        s["symbols"] = sorted(s["symbols"])
    return out


def fee_drag(trades: list, strategy: str = None) -> dict:
    """What share of the gross edge the exchange takes.

    Bybit reports closedPnl already NET of fees, so a small account can look
    strategy-negative while actually being gross-positive and cost-negative —
    a different problem with a different fix (size up, trade less, or use
    limit entries), and one that is invisible from net P&L alone.

    {'n', 'gross', 'fees', 'net', 'fee_pct_of_gross', 'gross_avg', 'net_avg'}
    fee_pct_of_gross is None when gross profit is <= 0 — there is no edge for
    fees to be a fraction OF, and printing a percentage there would invent one.
    """
    rows = attribute(trades) if strategy else [dict(t) for t in (trades or [])]
    if strategy:
        rows = [t for t in rows if t.get("strategy") == strategy]
    n = len(rows)
    fees = sum(abs(float(t.get("fees") or 0.0)) for t in rows)
    net = sum(float(t.get("pnl") or 0.0) for t in rows)
    gross = net + fees
    return {
        "n": n,
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "net": round(net, 4),
        "fee_pct_of_gross": (round(fees / gross * 100, 1) if gross > 0 else None),
        "gross_avg": round(gross / n, 4) if n else 0.0,
        "net_avg": round(net / n, 4) if n else 0.0,
    }


def fee_report(trades: list, title: str = "🧾 手續費侵蝕") -> str:
    """The fee-drag line for /winrate — stated only when there are fees to
    report, and never as a percentage of a gross loss."""
    d = fee_drag(trades)
    if not d["n"] or d["fees"] <= 0:
        return ""
    lines = [title,
             f"毛利 {d['gross']:+.2f} − 手續費 {d['fees']:.2f} = 淨利 {d['net']:+.2f} USDT",
             f"平均每筆：毛 {d['gross_avg']:+.3f} → 淨 {d['net_avg']:+.3f} USDT"]
    if d["fee_pct_of_gross"] is not None:
        lines.append(f"手續費吃掉毛利的 <b>{d['fee_pct_of_gross']:.0f}%</b>")
        if d["fee_pct_of_gross"] >= 50:
            lines.append("⚠️ 超過一半 — 這是成本問題，不是策略問題："
                         "單筆下太小、進出太頻繁，或該用限價單")
    else:
        lines.append("（毛利為負，手續費佔比無意義 — 問題不在成本）")
    return "\n".join(lines)


_LABEL = {"S1": "S1 訊號跟單", "S3": "S3 翻轉引擎", MANUAL: "手動交易"}


def report(trades: list, title: str = "📒 各策略實際損益（Bybit）") -> str:
    """Per-strategy P&L — the number /winrate could never show, because it
    added the bots and the operator's own trades into one total."""
    stats = split_summary(trades)
    if not stats:
        return f"{title}\n尚無已平倉的紀錄。"
    import tg_format
    order = [k for k in ("S1", "S3", MANUAL) if k in stats]
    order += [k for k in stats if k not in order]
    rows = [("策略", "筆數", "淨損益", "勝率", "PF")]
    for k in order:
        s = stats[k]
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
        rows.append((_LABEL.get(k, k), s["n"], f"{s['net']:+.2f}",
                     f"{s['win_rate']:.0f}%", pf))
    lines = [title, tg_format.pre_table(rows, align="lrrrr")]

    inferred = sum(s["inferred"] for s in stats.values())
    total = sum(s["n"] for s in stats.values())
    if inferred:
        lines.append(f"⚠️ {inferred}/{total} 筆是用下單大小<b>推測</b>的（開始記錄前的舊單），"
                     f"不是實際歸屬 — 之後的新單才會是精確的")
    thin = [_LABEL.get(k, k) for k in order if stats[k]["n"] < 30 and k != MANUAL]
    if thin:
        lines.append(f"（{'、'.join(thin)} 樣本太少，還看不出有沒有優勢）")

    # A strategy's P&L only measures the STRATEGY when the strategy also chose
    # the exit. Hand-closing a mirrored trade files the operator's decision
    # under the bot's name, so the mix has to be stated next to the number.
    for strat in ("S1", "S3"):
        iv = intervention_stats(strat)
        if iv["manual"]:
            lines.append(
                f"✋ {_LABEL.get(strat, strat)}：{iv['total']} 筆中有 "
                f"<b>{iv['manual']} 筆是手動平倉</b>（{iv['manual_pct']:.0f}%）— "
                f"這些的損益反映的是人的判斷，不是策略本身")
    return "\n".join(lines)
