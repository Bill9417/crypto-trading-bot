"""
ETH 14-day momentum — PAPER forward-test of the one honest backtest winner.

pine/strategies/TV_strategy_ETH_MOM_2h.pine was the only ETH system that
made money honestly in our 2-year test (+770 on 500 USDT notional, 8/8
quarters, ~30% win rate, N=168). Before it ever sees real money it must
prove itself FORWARD: this module runs the exact rule on closed 2h candles —

    close > close[168] (14 days ago)  → LONG
    close < close[168]               → SHORT
    (stop-and-reverse: always in a position, exit = the flip)

— alerts each flip to the Signals topic, and keeps a paper ledger of every
completed leg so /mom can show the accumulating forward-test record. NOTHING
here trades; promotion to real money is a human decision made against this
ledger after 60-90 days. Kill switch: ETH_MOM_ALERTS=false.
"""
import json
import os
import time

SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "2h"
TF_SEC = 7200
N = 168                    # 14 days of 2h bars — the backtested lookback
STATE_FILE = os.path.join(os.path.dirname(__file__), "eth_mom_state.json")
ALERTS_ON = os.getenv("ETH_MOM_ALERTS", "true").strip().lower() \
    in ("1", "true", "yes", "on")


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ── pure rule (unit-tested) ──────────────────────────────────────────────────
def direction(closes: list, n: int = N):
    """'long' / 'short' on the last CLOSED bar; None with insufficient data
    or an exact tie (keep the previous direction on ties)."""
    if len(closes) < n + 1:
        return None
    now_c, then_c = closes[-1], closes[-1 - n]
    if now_c > then_c:
        return "long"
    if now_c < then_c:
        return "short"
    return None


def closed_bars(ohlcv: list, now: float, tf_sec: int = TF_SEC) -> list:
    """Drop the still-forming candle (same guard the S3 scanner uses)."""
    if not ohlcv:
        return []
    last_open = float(ohlcv[-1][0]) / 1000.0
    return ohlcv[:-1] if now < last_open + tf_sec else ohlcv


def leg_pnl_pct(dir_: str, entry: float, exit_px: float) -> float:
    raw = (exit_px / entry - 1) * 100
    return raw if dir_ == "long" else -raw


def ledger_stats(ledger: list) -> dict:
    n = len(ledger)
    if not n:
        return {"n": 0, "net_pct": 0.0, "wins": 0}
    net = sum(l["pct"] for l in ledger)
    return {"n": n, "net_pct": net, "wins": sum(1 for l in ledger if l["pct"] > 0)}


# ── orchestration ────────────────────────────────────────────────────────────
def tick(client) -> bool:
    """Check the rule once per closed 2h bar; alert + ledger on a flip."""
    if not ALERTS_ON:
        return False
    now = time.time()
    state = _load_state()
    try:
        ohlcv = client.call("fetch_ohlcv", SYMBOL, TIMEFRAME, None, N + 40)
    except Exception:  # noqa: BLE001 — next sweep retries
        return False
    bars = closed_bars(ohlcv or [], now)
    if not bars:
        return False
    last_bar = int(bars[-1][0])
    if state.get("last_bar") == last_bar:
        return False                            # this closed bar already judged
    state["last_bar"] = last_bar
    closes = [float(b[4]) for b in bars]
    want = direction(closes)
    price = closes[-1]
    flipped = False

    if want and want != state.get("dir"):
        ledger = state.setdefault("ledger", [])
        prev_dir, prev_entry = state.get("dir"), state.get("entry")
        leg_note = ""
        if prev_dir and prev_entry:
            pct = leg_pnl_pct(prev_dir, prev_entry, price)
            ledger.append({"dir": prev_dir, "entry": prev_entry, "exit": price,
                           "pct": round(pct, 2), "ts": now})
            leg_note = f"上一腿 {prev_dir.upper()} {pct:+.2f}%\n"
        state["dir"], state["entry"], state["entry_ts"] = want, price, now
        st = ledger_stats(state.get("ledger") or [])
        record = (f"紙上戰績 {st['n']} 腿 · 淨 {st['net_pct']:+.1f}% "
                  f"({st['wins']} 勝)" if st["n"] else "紙上戰績從這一腿開始")
        try:
            import telegram_utils
            telegram_utils.send_message(
                f"📐 ETH 14d-MOM 翻轉 → {want.upper()} @ {price:,.0f} (2h收盤)\n"
                f"{leg_note}{record}\n"
                f"⚠️ 純紙上前測 (60-90天後看戰績再談真錢) — 不會下單",
                force=True, channel="signals")
        except Exception as exc:  # noqa: BLE001 — ledger still updates
            print(f"[ethmom] alert failed: {exc}")
        print(f"[ethmom] flip → {want} @ {price:,.0f}")
        flipped = True

    _save_state(state)
    return flipped


def report() -> str:
    """/mom — the forward-test ledger so far."""
    state = _load_state()
    st = ledger_stats(state.get("ledger") or [])
    lines = ["📐 ETH 14d-MOM 紙上前測"]
    if state.get("dir"):
        age_d = (time.time() - (state.get("entry_ts") or time.time())) / 86400
        lines.append(f"目前 {state['dir'].upper()} @ {state.get('entry'):,.0f} "
                     f"({age_d:.1f} 天)")
    else:
        lines.append("等第一個訊號 (每根2h收盤檢查)")
    if st["n"]:
        lines.append(f"完成 {st['n']} 腿 · 淨 {st['net_pct']:+.1f}% · {st['wins']} 勝")
        for l in (state.get("ledger") or [])[-5:]:
            lines.append(f"  {l['dir'].upper():5s} {l['entry']:,.0f}→{l['exit']:,.0f} "
                         f"{l['pct']:+.2f}%")
    else:
        lines.append("尚無完成的腿")
    lines.append("(回測: 2年 +770/500名目, 8/8季正, ~30%勝率 — 前測就是在驗證它)")
    return "\n".join(lines)
