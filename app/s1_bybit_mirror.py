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


def _s3_reserve() -> float:
    """Margin to leave untouched so S3 can still enter when its flag flips.

    Both engines share one sub-account, and S1 is the one that should give way:
    S3 trades a single symbol off a slow 30m signal and cannot re-enter if it
    misses the flip, while S1 fires often and skipping one mirror costs almost
    nothing. Without this S1 could hold 10 positions and leave S3 unable to
    find its 30 USDT.

    Only symbols S3 is FLAT on are reserved for — an open position's margin is
    already inside 'used', so counting it again would reserve twice. Fail-soft
    and conservative: if positions can't be read, reserve for everything.
    Override with S1_BYBIT_RESERVE_USDT (0 disables)."""
    import config
    override = os.getenv("S1_BYBIT_RESERVE_USDT")
    if override not in (None, ""):
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    total = 0.0
    for base in (getattr(config, "STRATEGY3_SYMBOLS", None) or []):
        try:
            margin = float(config.strategy3_params(base).get("margin") or 0.0)
        except Exception:  # noqa: BLE001 — unknown symbol, nothing to reserve
            continue
        try:
            if X.get_position(f"{base}/USDT:USDT"):
                continue                    # already open → margin already used
        except Exception:  # noqa: BLE001 — can't tell, so assume it needs room
            pass
        total += margin
    return total


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


def _set_tp(sym: str, tp_price: float) -> str:
    """Attach a Full-mode take-profit to the position server-side (same v5
    trading-stop endpoint set_stop uses; omitted fields stay unchanged, so
    this never disturbs the resting stop-loss). '' on success."""
    if not X.is_live():
        print(f"[s1-mirror][DRY-RUN] set TP {sym} → {tp_price:.6g} (no order sent)")
        return ""
    try:
        ex = X.client()
        market_id = ex.market(sym)["id"]
        body = {"category": "linear", "symbol": market_id, "positionIdx": 0,
                "tpslMode": "Full",
                "takeProfit": ex.price_to_precision(sym, tp_price)}
        setter = getattr(ex, "private_post_v5_position_trading_stop", None) or \
            getattr(ex, "privatePostV5PositionTradingStop", None)
        if setter is None:
            return "no trading-stop endpoint in this ccxt build"
        setter(body)
        return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:200]


# ── lifecycle hooks (called from bot.py — each never raises) ─────────────────
def mirror_open(binance_symbol: str, direction: str, price: float,
                sl_price: float, tp2_price: float = None) -> bool:
    """Binance entry FILLED → open the same trade on Bybit at fixed notional.
    Both the stop AND the final target rest server-side on Bybit, so the
    position closes itself even if this bot dies (TP1's half-close is the
    only client-driven step — if the bot is down at TP1, the whole position
    simply rides to the server-side TP2/SL instead)."""
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
            # Bybit lot-size pre-check with an HONEST message — without this a
            # BTC-priced symbol (min lot ≈ 118 USDT > the 100 USDT order) failed
            # deep in open_flip with a confusing STRATEGY3_MARGIN_USDT error.
            try:
                step, min_qty, min_notional = X._market_limits(sym)
                need = max(min_qty * float(price), min_notional or 0.0)
                if config.S1_BYBIT_ORDER_USDT < need:
                    _tg(f"{sym.split('/')[0]} 最小下單額 ≈{need:.0f} USDT > "
                        f"設定 {config.S1_BYBIT_ORDER_USDT:g} USDT，略過鏡單")
                    return False
            except Exception:  # noqa: BLE001 — open_flip still checks properly
                pass
            avail = _available_usdt()
            need = margin * MARGIN_BUFFER + _s3_reserve()
            if avail < need:
                _tg(f"{sym.split('/')[0]} 可用保證金不足（{avail:.1f} < "
                    f"{need:.1f} USDT，含保留給 S3 的額度），略過鏡單")
                return False

        res = X.open_flip(sym, d, float(price), float(sl_price),
                          margin, config.S1_BYBIT_LEVERAGE)
        if not res.get("ok"):
            _tg(f"⚠️ {sym.split('/')[0]} 開倉失敗：{res.get('error')}")
            return False
        if not res.get("dry"):
            state[sym] = {"side": d, "qty": float(res.get("qty") or 0),
                          "entry": float(price), "sl": float(sl_price),
                          "ts": time.time()}
            _save(state)
            # this row is deleted on close, so ownership is recorded separately
            # — otherwise the closed trade is indistinguishable from S3's and
            # the operator's own trades on the same account
            import strategy_ledger
            strategy_ledger.record_open("S1", sym, d)
            tp_note = ""
            if tp2_price:
                tp_err = _set_tp(sym, float(tp2_price))
                if tp_err:
                    # SL still protects the position; TP just falls back to the
                    # bot-driven close — surface it so the owner knows.
                    _tg(f"⚠️ {sym.split('/')[0]} 掛終標失敗（停損仍在）：{tp_err}")
                else:
                    tp_note = f" · 終標 {float(tp2_price):.6g}"
            _tg(f"{'🟢 做多' if d == 'long' else '🔴 做空'} {sym.split('/')[0]} "
                f"{config.S1_BYBIT_ORDER_USDT:g} USDT @ {price:.6g} · "
                f"停損 {sl_price:.6g}{tp_note}（皆掛在 Bybit 交易所端）")
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
            t["sl"] = float(entry_price)               # guardian re-arms at BE now
            _save(state)
        except Exception as exc:  # noqa: BLE001 — the guardian retries next pass
            _tg(f"⚠️ {sym.split('/')[0]} 停損移到進場價失敗：{str(exc)[:150]}")
            return False
        if X.is_live():
            _tg(f"{sym.split('/')[0]} TP1 — 已平一半、停損移到進場價 {entry_price:.6g}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[s1-mirror] mirror_tp1 error {binance_symbol}: {exc}")
        return False


# ── guardian: no mirror position may ever sit without a stop ─────────────────
GUARD_SEC = 300
_last_guard = 0.0


def guardian_tick() -> int:
    """Called every tracking pass (1 min), self-paced to GUARD_SEC. For every
    position WE opened: if its Bybit stop-loss is gone (attach failed once,
    or removed by hand), re-arm it at the tracked level (original SL, or
    breakeven after TP1). The same belt-and-braces S3 gives its own symbols —
    this is what makes the mirror safe to ignore. Returns stops re-armed."""
    global _last_guard
    if not enabled() or not X.is_live():
        return 0
    now = time.time()
    if now - _last_guard < GUARD_SEC:
        return 0
    _last_guard = now
    fixed = 0
    for sym, t in _load().items():
        try:
            pos = X.get_position(sym)
            if not pos or pos.get("side") != t.get("side") or pos.get("sl"):
                continue                     # flat / not ours / already protected
            slp = float(t.get("sl") or 0)
            if not slp:
                continue
            X.set_stop(sym, slp)
            fixed += 1
            _tg(f"⛑ {sym.split('/')[0]} 停損不見了 — 已重掛 @ {slp:.6g}")
        except Exception as exc:  # noqa: BLE001 — one symbol must not stop the sweep
            print(f"[s1-mirror] guardian error {sym}: {exc}")
    return fixed


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
        import strategy_ledger
        strategy_ledger.record_close("S1", sym)
        if X.is_live():
            note = f"已平倉 x{closed:g}" if closed else "Bybit 端已自行出場（停損先觸發）"
            _tg(f"{sym.split('/')[0]} 出場（{kind or 'close'}）— {note}")
        else:
            print(f"[s1-mirror][DRY-RUN] close {sym} ({kind})")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[s1-mirror] mirror_close error {binance_symbol}: {exc}")
        return False
