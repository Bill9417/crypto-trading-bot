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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def account_snapshot() -> dict:
    """Read-only Bybit balance + open positions across config.STRATEGY3_SYMBOLS —
    same shape as executor.account_snapshot() so the web page can show BOTH
    live accounts side by side. Works whenever keys exist, independent of
    LIVE_TRADING/STRATEGY3_LIVE, so the page shows the real account even in
    alert-only mode. NEVER sends an order."""
    if not keys_present():
        return {"ok": False, "error": "No Bybit API keys configured.",
                "live": False, "balance": None, "positions": []}
    try:
        ex = client()
        raw = ex.fetch_balance()
        acct = (((raw.get("info") or {}).get("result") or {}).get("list") or [{}])[0]

        def _num(key):
            v = acct.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        balance = {
            "equity": _num("totalEquity"),
            "wallet": _num("totalWalletBalance"),
            "available": _num("totalAvailableBalance"),
            "unrealized_pnl": _num("totalPerpUPL"),
        }

        our_symbols = {f"{b}/{config.QUOTE_ASSET}:{config.QUOTE_ASSET}"
                       for b in config.STRATEGY3_SYMBOLS}
        positions = []
        for p in ex.fetch_positions(None, params={"settleCoin": config.QUOTE_ASSET}):
            if p.get("symbol") not in our_symbols:
                continue                      # ignore anything manually opened elsewhere
            qty = float(p.get("contracts") or 0)
            if not qty:
                continue
            sl = str((p.get("info") or {}).get("stopLoss") or "").strip()
            positions.append({
                "symbol": p.get("symbol"),
                "side": (p.get("side") or "").upper(),
                "contracts": qty,
                "notional": p.get("notional"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "liq": p.get("liquidationPrice"),
                "leverage": p.get("leverage"),
                "unrealized_pnl": p.get("unrealizedPnl"),
                "pnl_pct": p.get("percentage"),
                "sl": float(sl) if sl not in ("", "0") else None,
            })
        return {"ok": True, "error": None, "live": is_live(),
                "balance": balance, "positions": positions}
    except Exception as exc:  # noqa: BLE001 — a read-only page must never 500 on an API blip
        return {"ok": False, "error": str(exc)[:300], "live": is_live(),
                "balance": None, "positions": []}


def _closed_pnl_rows(limit: int = 200) -> list:
    """Raw Bybit v5 closed-pnl rows across ALL our symbols (settleCoin scope,
    not per-symbol), newest-first pages concatenated until `limit` or the API
    runs dry. Bybit paginates via nextPageCursor, not offset/since."""
    ex = client()
    rows = []
    cursor = ""
    while len(rows) < limit:
        params = {"category": "linear", "settleCoin": config.QUOTE_ASSET, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = ex.private_get_v5_position_closed_pnl(params)
        result = (r or {}).get("result") or {}
        page = result.get("list") or []
        rows.extend(page)
        cursor = result.get("nextPageCursor") or ""
        if not cursor or not page:
            break
    return rows[:limit]


def closed_pnl_history(limit: int = 80) -> dict:
    """Read-only realized-P&L history for the Bybit sub-account — Strategy 3
    flips only, since this key only ever trades config.STRATEGY3_SYMBOLS."""
    if not keys_present():
        return {"ok": False, "error": "No Bybit API keys configured.", "trades": []}
    try:
        trades = []
        for it in _closed_pnl_rows(limit):
            try:
                pnl = float(it.get("closedPnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            trades.append({"symbol": it.get("symbol"), "pnl": pnl,
                            "asset": config.QUOTE_ASSET,
                            "time": int(it.get("updatedTime") or 0)})
        trades.sort(key=lambda x: x["time"], reverse=True)
        return {"ok": True, "trades": trades}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "trades": []}


def closed_pnl_summary(limit: int = 1000) -> dict:
    """Ground-truth Bybit sub-account P&L for the /performance page's Bybit
    panel — same shape as executor.realized_pnl_summary() so both accounts'
    panels can share frontend rendering code.

    Unlike Binance's income stream (which reports REALIZED_PNL/COMMISSION/
    FUNDING_FEE as separate rows), Bybit's closed-pnl endpoint reports one
    already-net 'closedPnl' per closed position — there is no separate fee/
    funding breakdown available here, so commission/funding are always 0 and
    net == realized. (This account only ever trades STRATEGY3_SYMBOLS, so
    there is no cross-strategy contamination to worry about either way.)"""
    if not keys_present():
        return {"ok": False, "error": "No Bybit API keys configured."}
    try:
        rows = _closed_pnl_rows(limit)
        trades = []
        day_net: dict = {}
        tz = ZoneInfo("Asia/Taipei")
        for it in rows:
            try:
                pnl = float(it.get("closedPnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            ts = int(it.get("updatedTime") or 0)
            trades.append({"symbol": it.get("symbol"), "pnl": pnl, "time": ts})
            if ts:
                day = datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d")
                day_net[day] = day_net.get(day, 0.0) + pnl
        today = datetime.now(tz).date()
        daily = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            daily.append({"date": d, "net": round(day_net.get(d, 0.0), 4)})
        trades.sort(key=lambda x: x["time"], reverse=True)

        sym_agg: dict = {}
        for t in trades:
            s = sym_agg.setdefault(t["symbol"] or "?", {"net": 0.0, "n": 0, "wins": 0, "losses": 0})
            s["net"] += t["pnl"]; s["n"] += 1
            if t["pnl"] > 0: s["wins"] += 1
            elif t["pnl"] < 0: s["losses"] += 1
        by_symbol = [{"symbol": k, "net": round(v["net"], 4), "n": v["n"],
                      "wins": v["wins"], "losses": v["losses"]} for k, v in sym_agg.items()]
        by_symbol.sort(key=lambda x: x["net"])

        hour_agg = [{"hour": h, "net": 0.0, "wins": 0, "losses": 0} for h in range(24)]
        for t in trades:
            if not t["time"]:
                continue
            h = datetime.fromtimestamp(t["time"] / 1000, tz).hour
            b = hour_agg[h]
            b["net"] += t["pnl"]
            if t["pnl"] > 0: b["wins"] += 1
            elif t["pnl"] < 0: b["losses"] += 1
        for b in hour_agg:
            b["net"] = round(b["net"], 4)

        pnls = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        n = wins + losses
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        cum = peak = mdd = 0.0
        for t in sorted(trades, key=lambda x: x["time"]):
            cum += t["pnl"]; peak = max(peak, cum); mdd = min(mdd, cum - peak)
        streak = 0
        streak_type = None
        for p in pnls:
            res = "W" if p > 0 else ("L" if p < 0 else None)
            if res is None:
                continue
            if streak_type is None:
                streak_type, streak = res, 1
            elif res == streak_type:
                streak += 1
            else:
                break
        realized = sum(pnls)
        return {
            "ok": True,
            "realized": round(realized, 4), "commission": 0.0, "funding": 0.0,
            "net": round(realized, 4),
            "n_trades": n, "wins": wins, "losses": losses,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "gross_win": round(gross_win, 4),
            "gross_loss": round(gross_loss, 4),
            "profit_factor": (round(gross_win / gross_loss, 2) if gross_loss > 0
                              else (None if gross_win == 0 else float("inf"))),
            "avg_win": round(gross_win / wins, 4) if wins else 0.0,
            "avg_loss": round(-gross_loss / losses, 4) if losses else 0.0,
            "expectancy": round(realized / n, 4) if n else 0.0,
            "max_drawdown": round(mdd, 4),
            "streak": streak, "streak_type": streak_type,
            "best": round(max(pnls), 4) if pnls else 0.0,
            "worst": round(min(pnls), 4) if pnls else 0.0,
            "daily": daily,
            "by_symbol": by_symbol,
            "hourly": hour_agg,
            "trades": trades[:80],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


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


def open_flip(symbol: str, direction: str, price: float, sl_price: float,
              margin: float, leverage: int) -> dict:
    """Market entry with the emergency stop attached. margin/leverage come
    from config.strategy3_params for this symbol — they are NOT always the
    global STRATEGY3_MARGIN_USDT/LEVERAGE, since XAUT trades a different size.
    Returns {ok, dry, qty, error, leverage_warning}. Dry-run (logged only)
    unless is_live()."""
    lev = leverage
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
            # this trade could lock more margin than the configured margin
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
