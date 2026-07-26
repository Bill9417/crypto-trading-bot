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


def record_close(strategy: str, symbol: str, ts: float = None) -> None:
    """Note that `strategy`'s position on `symbol` is flat."""
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
                break
        state["rows"] = rows
        _save(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[ledger] record_close failed {symbol}: {exc}")


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
    (EXCLUDE_TRADFI_PERPS), so this is a correctness override, not a guess."""
    import config
    sym = norm(symbol)
    base = sym[:-4] if sym.endswith("USDT") else sym
    if base in getattr(config, "MANUAL_ONLY_SYMBOLS", ()):
        return MANUAL
    s3_syms = {norm(s) + "USDT" if not norm(s).endswith("USDT") else norm(s)
               for s in (getattr(config, "STRATEGY3_SYMBOLS", None) or [])}
    if sym in s3_syms:
        return "S3"
    want = float(getattr(config, "S1_BYBIT_ORDER_USDT", 100.0) or 100.0)
    lev = float(getattr(config, "S1_BYBIT_LEVERAGE", 10) or 10)
    if notional and 0.9 * want <= notional <= 1.1 * want and leverage <= lev:
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
    return "\n".join(lines)
