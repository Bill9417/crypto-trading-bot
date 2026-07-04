"""
Strategy 3 — BYBIT execution layer.

Strategy 3 reads its signals from BINANCE 15m charts (same candles as the
TradingView chart the user watches) but places its orders on the user's BYBIT
account — completely separate from the Binance account the S1/S2 engines use,
so none of the shared Binance executor machinery applies here.

Credentials come from app/.env:
    BYBIT_API_KEY=...
    BYBIT_API_SECRET=...
(create the key on Bybit with ONLY the derivatives-trade permission — never
withdrawal). The account must be in ONE-WAY position mode (Bybit's default).

Safety model:
  • the global LIVE_TRADING gate applies here too — while it is false every
    order is logged as a DRY-RUN and nothing touches Bybit;
  • missing keys ⇒ alert-only (the scanner says so at startup);
  • every entry is a market order with a position stop-loss ATTACHED (Bybit
    v5 order-level stopLoss). ensure_stop() re-checks each loop and re-arms
    the stop via the trading-stop endpoint if it is ever missing;
  • MANUAL closes are respected: the scanner sees the position is gone and
    stands down until the NEXT flag (it never re-enters on the old one).
"""
import math
import os
import time

import ccxt

import config

_client = None


def keys_present() -> bool:
    return bool(os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET"))


def is_live() -> bool:
    """True only when orders will really reach Bybit."""
    return bool(config.LIVE_TRADING and keys_present())


def client():
    global _client
    if _client is None:
        _client = ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY", ""),
            "secret": os.getenv("BYBIT_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        _client.load_markets()
    return _client


def qty_for(price: float, margin: float, leverage: int,
            step: float, min_qty: float, min_notional: float) -> tuple:
    """(qty, error) — margin×leverage worth of contracts, floored to the lot
    step, checked against Bybit's minimum quantity AND minimum notional.

    Floors via floor(x + epsilon) rather than int(x): plain float division
    can land a hair BELOW an exact multiple (e.g. 0.3/0.1 == 2.9999999999999996
    in IEEE754), so int() truncates one whole step too low — a silent ~33%
    under-size in that example. The epsilon absorbs that without rounding a
    genuinely-fractional quantity up to the next step."""
    if price <= 0 or step <= 0:
        return 0.0, "bad price/step"
    raw = (margin * leverage) / price
    qty = math.floor(raw / step + 1e-9) * step
    qty = round(qty, 10)
    if qty < min_qty or qty <= 0:
        need = min_qty * price / leverage
        return 0.0, (f"qty {qty} < Bybit min {min_qty} — raise "
                     f"STRATEGY3_MARGIN_USDT to at least ~{need:.2f}")
    if min_notional and qty * price < min_notional:
        need = min_notional / leverage
        return 0.0, (f"notional {qty * price:.2f} < Bybit min {min_notional} — raise "
                     f"STRATEGY3_MARGIN_USDT to at least ~{need:.2f}")
    return qty, ""


def get_position(symbol: str) -> dict | None:
    """Our open Bybit position on symbol, or None. {'side','qty','entry','sl'}."""
    ex = client()
    for p in ex.fetch_positions([symbol]):
        qty = float(p.get("contracts") or 0)
        if qty > 0:
            sl = str((p.get("info") or {}).get("stopLoss") or "").strip()
            return {
                "side": p.get("side"),                      # 'long' / 'short'
                "qty": qty,
                "entry": float(p.get("entryPrice") or 0),
                "sl": float(sl) if sl not in ("", "0") else None,
            }
    return None


def _market_limits(symbol: str) -> tuple:
    m = client().market(symbol)
    lot = ((m.get("info") or {}).get("lotSizeFilter") or {})
    step = float(lot.get("qtyStep") or m.get("precision", {}).get("amount") or 0.001)
    min_qty = float(lot.get("minOrderQty") or (m.get("limits", {}).get("amount", {}).get("min") or 0))
    min_notional = float(lot.get("minNotionalValue") or 0)
    return step, min_qty, min_notional


def open_flip(symbol: str, direction: str, price: float, sl_price: float) -> dict:
    """Market entry with the emergency stop attached. Returns
    {ok, dry, qty, error, leverage_warning}. Dry-run (logged only) unless
    is_live()."""
    margin = config.STRATEGY3_MARGIN_USDT
    lev = config.STRATEGY3_LEVERAGE
    res = {"ok": False, "dry": not is_live(), "qty": 0.0, "error": None,
           "leverage_warning": None}

    if not is_live():
        # Keys may be absent (no market metadata to floor against) — fall back
        # to the unfloored estimate so the preview never crashes; with keys
        # present, show the SAME floored qty a live order would actually use.
        qty_preview = margin * lev / price
        if keys_present():
            try:
                step, min_qty, min_notional = _market_limits(symbol)
                floored, err = qty_for(price, margin, lev, step, min_qty, min_notional)
                if not err:
                    qty_preview = floored
            except Exception:  # noqa: BLE001 — preview only, never block dry-run
                pass
        res.update(ok=True, qty=round(qty_preview, 6))
        print(f"[s3-exec][DRY-RUN] {direction.upper()} {symbol} @ {price:.6g} "
              f"SL {sl_price:.6g} · {margin} USDT × {lev}x (no order sent — "
              f"{'LIVE_TRADING=false' if keys_present() else 'no Bybit keys'})")
        return res

    ex = client()
    step, min_qty, min_notional = _market_limits(symbol)
    qty, err = qty_for(price, margin, lev, step, min_qty, min_notional)
    if err:
        res["error"] = err
        return res

    try:
        ex.set_leverage(lev, symbol)
    except Exception as exc:  # noqa: BLE001 — 110043 'leverage not modified' is normal
        if "110043" not in str(exc) and "not modified" not in str(exc).lower():
            # Not benign: the account may stay on its PRIOR leverage tier, so
            # this trade could lock more margin than STRATEGY3_MARGIN_USDT
            # implies (same notional, less leverage ⇒ more margin required) —
            # surface it so the caller can tell the user, rather than a print()
            # nobody watches in real time.
            print(f"[s3-exec] set_leverage warning {symbol}: {exc}")
            res["leverage_warning"] = str(exc)[:200]

    side = "buy" if direction == "long" else "sell"
    try:
        order = ex.create_order(symbol, "market", side, qty, params={
            "positionIdx": 0,                       # one-way mode
            "stopLoss": ex.price_to_precision(symbol, sl_price),
        })
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "position idx not match" in msg.lower() or "10001" in msg:
            msg += " — is the Bybit account in One-Way position mode?"
        res["error"] = msg[:300]
        return res

    res.update(ok=True, qty=qty, order_id=(order or {}).get("id"))
    # belt-and-braces: make sure the stop really rests on the position
    try:
        time.sleep(1.0)
        ensure_stop(symbol, sl_price)
    except Exception as exc:  # noqa: BLE001
        print(f"[s3-exec] ensure_stop after entry failed {symbol}: {exc}")
    return res


def close_flip(symbol: str) -> dict:
    """Close the whole position at market (reduce-only). Dry-run unless live."""
    res = {"ok": False, "dry": not is_live(), "error": None}
    if not is_live():
        res["ok"] = True
        print(f"[s3-exec][DRY-RUN] CLOSE {symbol} (no order sent)")
        return res
    pos = get_position(symbol)
    if not pos:
        res["ok"] = True                              # already flat
        return res
    side = "sell" if pos["side"] == "long" else "buy"
    try:
        client().create_order(symbol, "market", side, pos["qty"], params={
            "positionIdx": 0, "reduceOnly": True,
        })
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)[:300]
    return res


def ensure_stop(symbol: str, sl_price: float = None, pos: dict = None) -> None:
    """Guardian: if our position has NO stop-loss (order-attach failed, or it
    was removed by hand), set one via the v5 trading-stop endpoint. sl_price
    None ⇒ derive from entry ± STRATEGY3_EMERGENCY_SL_PCT. Pass `pos` (a fresh
    get_position result) to skip the extra position fetch."""
    if not is_live():
        return
    if pos is None:
        pos = get_position(symbol)
    if not pos or pos.get("sl"):
        return
    ex = client()
    if sl_price is None:
        slp = config.STRATEGY3_EMERGENCY_SL_PCT
        sl_price = pos["entry"] * (1 - slp) if pos["side"] == "long" else pos["entry"] * (1 + slp)
    market_id = ex.market(symbol)["id"]
    body = {"category": "linear", "symbol": market_id, "positionIdx": 0,
            "tpslMode": "Full", "stopLoss": ex.price_to_precision(symbol, sl_price)}
    setter = getattr(ex, "private_post_v5_position_trading_stop", None) or \
        getattr(ex, "privatePostV5PositionTradingStop", None)
    if setter is None:
        print(f"[s3-exec] no trading-stop endpoint in this ccxt — naked position on {symbol}!")
        return
    setter(body)
    print(f"[s3-exec] guardian re-armed stop on {symbol} @ {sl_price:.6g}")
