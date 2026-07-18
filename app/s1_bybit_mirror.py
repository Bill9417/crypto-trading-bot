"""
🪞 S1 → Bybit mirror — every S1 trade replicated on the REAL Bybit account
at a fixed order value (config.S1_BYBIT_ORDER_USDT, default 100 USDT notional).

Execution model (kept deliberately simple and sync-correct):
  • ENTRY  — market order the moment the Binance limit FILLS (not when it is
    queued), with S1's stop-loss attached server-side on Bybit. A Bybit
    position exists iff the Binance position exists; slippage on a 100 USDT
    market order in majors is bps.
  • TP1    — half the position closes at market, the Bybit stop moves to
    breakeven (entry) — mirroring exactly what the live Binance bracket does.
  • EXIT   — SL / TP2 / breakeven: close whatever remains. In the SL case
    Bybit's own attached stop usually fired server-side already at the same
    price; the close call then finds less/nothing and simply reconciles.

Safety rails (this account also runs Strategy 3 AND the owner's manual trades):
  • Tracked-state file: the mirror only ever CLOSES quantity it opened itself
    (s1_bybit_positions.json). It will never touch XAUT or a manual position.
  • If the symbol already has an untracked Bybit position, the mirror SKIPS
    the trade entirely (opening would merge positions in One-Way mode and the
    attached stop would apply to the whole merged position).
  • Free-margin pre-check before every entry (the -110007 guard).
  • Symbols Binance lists but Bybit doesn't are skipped with one note.
  • Obeys the LIVE_TRADING master switch via strategy3_exec.is_live() —
    everything dry-run-logs when it is off.
  • Every action/skip/error sends one 🪞 note to the owner's Alerts topic.

All entry points are exception-safe: a Bybit blip must never break S1's own
Binance execution or signal tracking.
"""
import json
import math
import os
import time

import config
import strategy3_exec as X

STATE_FILE = os.path.join(os.path.dirname(__file__), "s1_bybit_positions.json")

# margin the entry pre-check requires beyond the strict requirement
MARGIN_BUFFER = 1.3

_unlisted_warned: set = set()


def enabled() -> bool:
    return bool(config.S1_BYBIT_MIRROR and X.keys_present())


def _sym(binance_symbol: str) -> str:
    """'ETH/USDT:USDT' or 'ETH/USDT' (Binance form) → Bybit linear symbol."""
    base = str(binance_symbol or "").split("/")[0].upper()
    return f"{base}/USDT:USDT"


def _dir(direction: str) -> str:
    return "long" if str(direction).lower() in ("long", "buy") else "short"


# ── tracked state (what WE opened — the only thing we may close) ─────────────
def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = nothing tracked
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _tg(msg: str) -> None:
    try:
        import telegram_utils
        telegram_utils.send_message(f"🪞 S1鏡單(Bybit) · {msg}",
                                    force=True, channel="alerts")
    except Exception as exc:  # noqa: BLE001 — a note must never break execution
        print(f"[s1-mirror] telegram failed: {exc}")


def _available_usdt() -> float:
    """Bybit unified available balance; 0.0 on any error (→ entry is skipped,
    the safe direction)."""
    try:
        raw = X.client().fetch_balance()
        acct = (((raw.get("info") or {}).get("result") or {}).get("list") or [{}])[0]
        return float(acct.get("totalAvailableBalance") or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[s1-mirror] balance check failed: {exc}")
        return 0.0


def _reduce(sym: str, pos_side: str, qty: float) -> str:
    """Reduce-only market close of `qty` contracts. '' on success, error text
    otherwise. Dry-run logs when not live."""
    if not X.is_live():
        print(f"[s1-mirror][DRY-RUN] reduce {sym} {pos_side} x{qty} (no order sent)")
        return ""
    side = "sell" if pos_side == "long" else "buy"
    try:
        X.client().create_order(sym, "market", side, qty, params={
            "positionIdx": 0, "reduceOnly": True,
        })
        return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:300]


# ── lifecycle hooks (called from bot.py — each never raises) ─────────────────
def mirror_open(binance_symbol: str, direction: str, price: float,
                sl_price: float) -> bool:
    """Binance entry FILLED → open the same trade on Bybit at fixed notional."""
    if not enabled():
        return False
    try:
        sym = _sym(binance_symbol)
        d = _dir(direction)

        if sym not in (X.client().markets or {}):
            if sym not in _unlisted_warned:
                _unlisted_warned.add(sym)
                _tg(f"{sym.split('/')[0]} Bybit 未上市，略過（本次啟動只提醒一次）")
            return False

        state = _load()
        if sym in state:
            print(f"[s1-mirror] {sym} already mirrored — skip duplicate open")
            return False

        # NEVER merge into a manual / S3 position (One-Way mode would merge,
        # and our attached stop would then govern the whole merged position).
        if X.get_position(sym):
            _tg(f"{sym.split('/')[0]} 已有未追蹤的 Bybit 倉位（手動/S3），略過鏡單以保護它")
            return False

        margin = config.S1_BYBIT_ORDER_USDT / config.S1_BYBIT_LEVERAGE
        if X.is_live():
            avail = _available_usdt()
            if avail < margin * MARGIN_BUFFER:
                _tg(f"{sym.split('/')[0]} 可用保證金不足（{avail:.1f} < "
                    f"{margin * MARGIN_BUFFER:.1f} USDT），略過鏡單")
                return False

        res = X.open_flip(sym, d, float(price), float(sl_price),
                          margin, config.S1_BYBIT_LEVERAGE)
        if not res.get("ok"):
            _tg(f"⚠️ {sym.split('/')[0]} 開倉失敗：{res.get('error')}")
            return False
        if not res.get("dry"):
            state[sym] = {"side": d, "qty": float(res.get("qty") or 0),
                          "entry": float(price), "ts": time.time()}
            _save(state)
            _tg(f"{'🟢 做多' if d == 'long' else '🔴 做空'} {sym.split('/')[0]} "
                f"{config.S1_BYBIT_ORDER_USDT:g} USDT @ {price:.6g} · "
                f"停損 {sl_price:.6g}（已掛在 Bybit）")
        return bool(res.get("ok"))
    except Exception as exc:  # noqa: BLE001 — the mirror must never break S1
        print(f"[s1-mirror] mirror_open error {binance_symbol}: {exc}")
        return False


def mirror_tp1(binance_symbol: str, direction: str, entry_price: float) -> bool:
    """TP1 hit on S1 → close HALF on Bybit, move the stop to breakeven."""
    if not enabled():
        return False
    try:
        sym = _sym(binance_symbol)
        state = _load()
        t = state.get(sym)
        if not t:
            return False

        step, min_qty, _mn = X._market_limits(sym)
        half = math.floor((t["qty"] / 2.0) / step + 1e-9) * step
        half = round(half, 10)
        if half >= min_qty:
            err = _reduce(sym, t["side"], half)
            if err:
                _tg(f"⚠️ {sym.split('/')[0]} TP1 平一半失敗：{err}")
            else:
                t["qty"] = round(t["qty"] - half, 10)
                _save(state)
        # breakeven stop on the remainder either way (matches S1's live bracket)
        try:
            X.set_stop(sym, float(entry_price))
        except Exception as exc:  # noqa: BLE001 — stop move retries are manual
            _tg(f"⚠️ {sym.split('/')[0]} 停損移到進場價失敗：{str(exc)[:150]}")
            return False
        if X.is_live():
            _tg(f"{sym.split('/')[0]} TP1 — 已平一半、停損移到進場價 {entry_price:.6g}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[s1-mirror] mirror_tp1 error {binance_symbol}: {exc}")
        return False


def mirror_close(binance_symbol: str, kind: str = "") -> bool:
    """S1 closed (sl/tp2/be/manual) → close what remains of OUR tracked qty."""
    if not enabled():
        return False
    try:
        sym = _sym(binance_symbol)
        state = _load()
        t = state.pop(sym, None)
        if not t:
            return False

        pos = X.get_position(sym) if X.is_live() else None
        closed = 0.0
        if pos and pos.get("side") == t["side"]:
            qty = round(min(float(pos["qty"]), float(t["qty"])), 10)
            if qty > 0:
                err = _reduce(sym, t["side"], qty)
                if err:
                    state[sym] = t                      # keep tracking — retry-able
                    _save(state)
                    _tg(f"⚠️ {sym.split('/')[0]} 平倉失敗（{kind}）：{err} — 請檢查 Bybit")
                    return False
                closed = qty
        _save(state)
        if X.is_live():
            note = f"已平倉 x{closed:g}" if closed else "Bybit 端已自行出場（停損先觸發）"
            _tg(f"{sym.split('/')[0]} 出場（{kind or 'close'}）— {note}")
        else:
            print(f"[s1-mirror][DRY-RUN] close {sym} ({kind})")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[s1-mirror] mirror_close error {binance_symbol}: {exc}")
        return False
