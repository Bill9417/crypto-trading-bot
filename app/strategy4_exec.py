"""
📊 S4 → Bybit execution. Real orders, small size, TP and SL attached at entry.

OFF BY DEFAULT. S4_EXEC must be set to "bybit" and LIVE_TRADING must be true
before a single order is sent, and the scanner prints which mode it is in on
every startup. Arming real money is a decision, not a side effect of a deploy.

WHAT THE RECORD ACTUALLY SAYS, because this is the module that spends money on
it. S4's alerted book at the time of writing: n=121, +0.081R, CI
[-0.164, +0.327], PF 1.14 over 6.5 days — and the top 3 symbols carry 127% of
the profit, meaning everything else combined loses. That is not a proven edge,
it is a positive-looking sample whose interval contains zero. At 40 USDT
notional and a ~1.5% median stop that is ~0.9 USDT of risk per trade, which is
the right size for finding out and the wrong size for expecting anything.

REUSES strategy3_exec for every exchange primitive — client, live gating, lot
limits, qty flooring, position reads, bybit_mode's positionIdx retry. A second
implementation of any of those is how this repo got a 344x oversized order, an
unprotected naked position, and a symbol trading the wrong asset.

THE FOUR WAYS THIS CAN GO WRONG, all of them already paid for once:

  1. SAME TICKER, DIFFERENT ASSET. Binance ON was $0.2458 while Bybit's ON —
     a different token — was $84.52. Sizing from the wrong price asked for
     $17k of notional against $50 of buying power. Guarded by a price
     divergence check before any order.
  2. MERGING INTO SOMEONE ELSE'S POSITION. One-Way mode merges, and our stop
     would then govern a manual or S3 position too. Any existing position on
     the symbol means SKIP, and the owner is told.
  3. A NAKED POSITION. Binance conditional orders are invisible to the order
     API and closePosition rejects with -4130/-4509. On Bybit the stop rides on
     the order itself, and ensure_stop re-checks after the fill.
  4. LOT SIZE. A BTC-priced symbol has a minimum lot worth more than the whole
     order, which used to fail deep inside open_flip with a misleading error.
     Checked up front against BYBIT's own price and limits.
"""
import json
import os
import time

import config
import strategy3_exec as X

STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy4_exec_state.json")

ORDER_USDT = float(os.getenv("S4_BYBIT_ORDER_USDT", "40"))
LEVERAGE = int(os.getenv("S4_BYBIT_LEVERAGE", "5"))
# Any real cross-exchange basis on the same asset is well under 1%. 5% is a
# ticker collision, not a market.
MAX_PRICE_DIVERGENCE = float(os.getenv("S4_MAX_PRICE_DIVERGENCE", "0.05"))
# How many S4 positions may be open at once. NOT a tuned number — derived from
# what the account can lose if every one of them is wrong at the same time,
# which is the realistic case: the meter is regime-driven, so S4's signals
# arrive same-side in clusters rather than independently. Worst case per trade
# is ORDER_USDT x MAX_STOP_PCT = 40 x 4% = 1.6 USDT, so 8 concurrent risks
# ~12.8 USDT — under 3% of the ~458 USDT this sub-account holds. Raise it by
# raising the account, not by hoping the correlation is lower than it looks.
MAX_CONCURRENT = int(os.getenv("S4_MAX_CONCURRENT", "8"))
# Uppercase, like S1 and S3. Lowercase "s4" wrote rows that STRATEGIES,
# _LABEL and report() all match on exactly and none of them recognise —
# a record kept under a name nothing reads.
STRAT = "S4"
MARGIN_BUFFER = 1.15


# Bybit gates some contracts behind a per-product agreement the ACCOUNT must
# accept in the app — 110125 "You must agree to the Crude Oil Trading Terms".
# No amount of retrying fixes that, and S4 would re-attempt CL on every signal
# forever, so the symbol is remembered and skipped until the owner clears it.
# Matched on the retCode AND on the wording, because the code differs per
# product (crude oil, precious metals, index futures) while the sentence does
# not.
AGREEMENT_MARKERS = ("110125", "must agree", "trading terms", "agreement")


def _blocked() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("blocked") or {}
    except (OSError, ValueError):
        return {}


def _block(sym: str, why: str) -> None:
    """Remember a permanent refusal so it is not retried every sweep."""
    try:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f) or {}
        except (OSError, ValueError):
            state = {}
        state.setdefault("blocked", {})[sym] = {"why": why[:200],
                                                "ts": time.time()}
        tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — a bookkeeping failure never blocks
        print(f"[s4-exec] could not persist block for {sym}: {exc}")


def unblock(sym: str = None) -> int:
    """Clear one symbol, or all of them. For after the terms are accepted."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f) or {}
    except (OSError, ValueError):
        return 0
    blocked = state.get("blocked") or {}
    n = len(blocked) if sym is None else (1 if blocked.pop(sym, None) else 0)
    if sym is None:
        blocked = {}
    state["blocked"] = blocked
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)
    return n


def mode() -> str:
    return (os.getenv("S4_EXEC", "off") or "off").strip().lower()


def enabled() -> bool:
    """Live orders require BOTH the S4 switch and the global trading switch."""
    return mode() == "bybit" and bool(getattr(config, "LIVE_TRADING", False))


def _tg_owner(msg: str) -> None:
    """channel='trades' — the PRIVATE feed. Order sizes, margin and position
    state may never reach the joinable group; send_message() defaults to the
    public 'alerts' topic, which is how realised P&L was once broadcast."""
    try:
        import telegram_utils
        telegram_utils.send_message(msg, force=True, channel="trades")
    except Exception as exc:  # noqa: BLE001 — a failed message never blocks a trade
        print(f"[s4-exec] telegram failed: {exc}")


def bybit_symbol(sym: str):
    """The Bybit symbol for an S4 signal, or None if it does not list there.

    Checked against Bybit's OWN market list rather than assembled from the
    base name — "it probably exists" is how a ticker collision becomes an
    order.
    """
    try:
        markets = X.client().load_markets()
    except Exception as exc:  # noqa: BLE001
        print(f"[s4-exec] load_markets failed: {exc}")
        return None
    if sym in markets and (markets[sym] or {}).get("swap"):
        return sym
    return None


def s4_open_count() -> int:
    """How many S4 positions are open, counted FROM THE EXCHANGE.

    The ledger says which symbols are S4's; the exchange says which still
    exist. Counting ledger rows alone would repeat a bug this repo has already
    paid for — hand-closing a mirrored position left a row nothing ever cleared,
    and it silently blocked that symbol forever. A position that is gone from
    the exchange cannot occupy a slot, however stale the bookkeeping is.

    Returns -1 when the count cannot be made, which the caller treats as
    "cannot be sure" rather than "zero".
    """
    try:
        live = {p["symbol"] for p in X.client().fetch_positions()
                if float(p.get("contracts") or 0)}
    except Exception as exc:  # noqa: BLE001
        print(f"[s4-exec] position count failed: {exc}")
        return -1
    try:
        import strategy_ledger
        rows = strategy_ledger._load().get("rows") or []
        mine = {strategy_ledger.norm(r["symbol"]) for r in rows
                if r.get("strategy") == STRAT and not r.get("closed")}
        return sum(1 for sym in live if strategy_ledger.norm(sym) in mine)
    except Exception:  # noqa: BLE001 — no ledger: count everything, which
        return len(live)                   # binds sooner. Safe direction.


def preflight(sym: str, price: float) -> tuple:
    """(ok, reason). Everything that must be true before an order is sent."""
    if sym in _blocked():
        return False, "needs_agreement"
    if not bybit_symbol(sym):
        return False, "not_listed"

    # 🛑 The account-wide daily loss brake. S3, the S1 mirror and the copy
    # engine have all consulted this since it was written; S4 was the one live
    # engine that did not, so a day bad enough to halt every other engine left
    # S4 opening new positions into it. OFF unless MAX_DAILY_LOSS_USDT is set.
    try:
        import daily_risk
        halted = daily_risk.entry_blocked()
        if halted:
            return False, f"daily_loss_limit:{halted}"
    except Exception as exc:  # noqa: BLE001 — a brake that breaks must not
        print(f"[s4-exec] daily_risk unavailable: {exc}")   # stop trading


    # 2. Never merge into a manual / S3 / earlier-S4 position.
    try:
        if X.get_position(sym):
            return False, "position_open"
    except Exception as exc:  # noqa: BLE001 — cannot read = cannot be sure = skip
        return False, f"position_check_failed:{str(exc)[:60]}"

    # 1. Same ticker, different asset.
    try:
        t = X.client().fetch_ticker(sym)
        bybit_px = float((t or {}).get("last") or 0)
    except Exception:  # noqa: BLE001
        bybit_px = 0.0
    if bybit_px and price:
        div = abs(bybit_px - float(price)) / float(price)
        if div > MAX_PRICE_DIVERGENCE:
            return False, f"price_divergence:{div * 100:.0f}%"

    # 4. Lot size, against Bybit's own price and limits.
    try:
        step, min_qty, min_notional = X._market_limits(sym)
        need = max((min_qty or 0) * (bybit_px or price), min_notional or 0.0)
        if ORDER_USDT < need:
            return False, f"min_notional:{need:.0f}USDT"
    except Exception:  # noqa: BLE001 — open_flip checks properly too
        pass

    # LAST, deliberately. This is the only gate that needs the WHOLE position
    # book, and putting it first made it answer for gates that had not run yet:
    # a symbol with a position already open reported "position_count_failed"
    # instead of "position_open", so the specific, actionable reason was
    # replaced by a generic one. Cheapest and most specific first; the
    # portfolio-wide ceiling is neither.
    if MAX_CONCURRENT > 0:
        n = s4_open_count()
        if n < 0:
            return False, "position_count_failed"
        if n >= MAX_CONCURRENT:
            return False, f"max_concurrent:{n}/{MAX_CONCURRENT}"
    return True, "ok"


def open_trade(sig: dict) -> dict:
    """Send one S4 setup to Bybit with TP and SL attached.

    Returns {ok, skipped, reason, qty, order_id}. Never raises: a broken order
    must not take the scanner down with it.
    """
    out = {"ok": False, "skipped": False, "reason": None, "qty": 0.0,
           "order_id": None}
    sym = sig.get("symbol")
    plan = sig.get("plan") or {}
    entry, sl, tp = plan.get("entry"), plan.get("sl"), plan.get("tp")
    side = sig.get("side") or "long"
    if not sym or not entry or not sl or not tp:
        out.update(skipped=True, reason="incomplete_plan")
        return out

    # Geometry, checked here and not assumed. A stop on the wrong side of entry
    # would arm an instant loss and make every R meaningless.
    if side == "long" and not (sl < entry < tp):
        out.update(skipped=True, reason="bad_geometry")
        return out
    if side == "short" and not (tp < entry < sl):
        out.update(skipped=True, reason="bad_geometry")
        return out

    if not enabled():
        out.update(skipped=True, reason="disabled")
        print(f"[s4-exec][OFF] would {side} {sym} @ {entry:.6g} "
              f"SL {sl:.6g} TP {tp:.6g} · {ORDER_USDT:g} USDT "
              f"(set S4_EXEC=bybit + LIVE_TRADING=true to arm)")
        return out

    ok, reason = preflight(sym, entry)
    if not ok:
        out.update(skipped=True, reason=reason)
        base = sym.split("/")[0]
        if reason == "position_open":
            # Asked for explicitly: tell the owner rather than silently passing.
            _tg_owner(f"⏭️ S4 略過 {base} — Bybit 已有持倉，不重複下單也不併倉")
        elif reason.startswith("price_divergence"):
            _tg_owner(f"⚠️ S4 略過 {base} — Bybit 報價與訊號差距過大 ({reason})，"
                      f"很可能是同代號不同幣")
        elif reason.startswith("min_notional"):
            _tg_owner(f"⏭️ S4 略過 {base} — Bybit 最小下單額大於 "
                      f"{ORDER_USDT:g} USDT ({reason})")
        print(f"[s4-exec] skip {sym}: {reason}")
        return out

    margin = ORDER_USDT / max(LEVERAGE, 1)
    try:
        res = _send(sym, side, entry, sl, tp, margin)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        low = msg.lower()
        base = sym.split("/")[0]
        if any(m in low for m in AGREEMENT_MARKERS):
            # Permanent until the OWNER accepts the product terms in the Bybit
            # app. Retrying is pure noise, so remember it and say once what to
            # do — an error nobody can act on is worse than silence.
            _block(sym, msg)
            out.update(skipped=True, reason="needs_agreement")
            print(f"[s4-exec] {sym} blocked — needs a Bybit product agreement")
            _tg_owner(
                f"🚫 S4 已停用 {base} — Bybit 要求先同意這個商品的交易條款\n"
                f"要交易它：Bybit App → 該合約 → 同意條款，然後跟我說一聲我把它解鎖。\n"
                f"不處理也沒關係，S4 之後不會再試 {base}，其他幣照常。")
            return out
        out.update(reason=f"order_failed:{msg[:120]}")
        print(f"[s4-exec] order failed {sym}: {exc}")
        _tg_owner(f"❌ S4 {base} 下單失敗：{msg[:120]}")
        return out
    out.update(res)
    # Ownership, recorded AT OPEN. S4 was placing real Bybit orders without
    # ever registering them, so its trades fell through to the ledger's
    # notional-based infer() — the same blind spot that once reported an 80%
    # win rate for a book that was ~90% manual. Bookkeeping never blocks a
    # fill, so this sits after the order and cannot fail it.
    if res.get("ok"):
        try:
            import strategy_ledger
            strategy_ledger.record_open(STRAT, sym, side)
        except Exception as exc:  # noqa: BLE001
            print(f"[s4-exec] ledger record_open failed {sym}: {exc}")
    return out


def _send(sym: str, side: str, entry: float, sl: float, tp: float,
          margin: float) -> dict:
    """The order itself. TP and SL ride ON the entry, not as follow-ups."""
    import bybit_mode
    ex = X.client()
    step, min_qty, min_notional = X._market_limits(sym)
    qty, err = X.qty_for(entry, margin, LEVERAGE, step, min_qty, min_notional)
    if err:
        return {"ok": False, "skipped": True, "reason": f"qty:{err}", "qty": 0.0}

    try:
        ex.set_leverage(LEVERAGE, sym)
    except Exception as exc:  # noqa: BLE001 — 110043 "not modified" is normal
        if "110043" not in str(exc) and "not modified" not in str(exc).lower():
            print(f"[s4-exec] set_leverage warning {sym}: {exc}")

    order_side = "buy" if side == "long" else "sell"
    # Both brackets attached at entry. A TP sent afterwards leaves a window
    # where a fast move exits at neither level, and a SEPARATE conditional
    # order can be rejected while the position is already open — which is how
    # this repo produced naked positions on Binance.
    order = bybit_mode.send_with_mode(sym, side, lambda pidx:
        ex.create_order(sym, "market", order_side, qty, params={
            "positionIdx": pidx,
            "stopLoss": ex.price_to_precision(sym, sl),
            "takeProfit": ex.price_to_precision(sym, tp),
        }))

    # Belt and braces: confirm the stop really rests on the position. Bybit has
    # accepted an order and left the stop off it before.
    try:
        time.sleep(1.0)
        X.ensure_stop(sym, sl)
    except Exception as exc:  # noqa: BLE001
        print(f"[s4-exec] ensure_stop failed {sym}: {exc}")

    base = sym.split("/")[0]
    _tg_owner(f"✅ S4 已下單 {base} {'做多' if side == 'long' else '做空'}\n"
              f"進場 {entry:.6g} · 停損 {sl:.6g} · 停利 {tp:.6g}\n"
              f"{ORDER_USDT:g} USDT × {LEVERAGE}x")
    print(f"[s4-exec] OPENED {side} {sym} qty {qty} SL {sl:.6g} TP {tp:.6g}")
    return {"ok": True, "skipped": False, "reason": None, "qty": qty,
            "order_id": (order or {}).get("id")}


def status_line() -> str:
    """One line for the scanner's startup banner — never let the mode be a
    thing you have to infer from behaviour."""
    if mode() != "bybit":
        return "S4 執行：關閉（只發訊號）"
    if not getattr(config, "LIVE_TRADING", False):
        return "S4 執行：S4_EXEC=bybit 但 LIVE_TRADING=false → 仍然不會下單"
    blocked = _blocked()
    tail = (f" · 已停用 {len(blocked)} 檔（需同意條款）" if blocked else "")
    return (f"S4 執行：⚠️ BYBIT 實單 · {ORDER_USDT:g} USDT × {LEVERAGE}x · "
            f"附停損停利 · 已有持倉的幣會跳過{tail}")
