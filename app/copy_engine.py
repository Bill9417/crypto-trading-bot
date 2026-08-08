"""
🪞 Copy-trading engine — mirrors each live Strategy-3 (Bybit) action onto every
ENABLED follower's own Bybit account, sized by THAT follower's chosen margin.

Called from strategy3_scanner after a real S3 open / close / stop-move. Design
rules that keep other people's money safe:

  • Master switch: nothing fires unless config.COPY_TRADING_LIVE is true AND the
    encryption vault is available. Off ⇒ dry-run logs only.
  • Per-follower isolation: one follower's bad key / low balance / One-Way-mode
    error NEVER aborts the loop or affects the master or other followers.
  • The master's own account is untouched here — S3 already traded it.
  • Sizing uses the follower's margin_usdt with the SAME leverage/limits as the
    master trade (reuses strategy3_exec.qty_for / _market_limits — market
    metadata is account-independent).
  • Runtime status is written to copy_status.json (scanner-owned) so the web
    process can show health without this process ever writing copy_followers.json.
"""
import json
import os
import time

import ccxt

import bybit_mode
import config
import copy_store
import strategy3_exec as X

STATUS_FILE = os.path.join(os.path.dirname(__file__), "copy_status.json")
_clients: dict = {}          # api_key → ccxt client (markets loaded once)


def live() -> bool:
    """True only when real follower orders will be sent."""
    return bool(getattr(config, "COPY_TRADING_LIVE", False)) and _vault_ok()


def _vault_ok() -> bool:
    try:
        import copy_vault
        return copy_vault.available()
    except Exception:  # noqa: BLE001
        return False


def _client(api_key: str, api_secret: str):
    """One ccxt client per API key (markets loaded once, then reused)."""
    c = _clients.get(api_key)
    if c is None:
        c = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        c.load_markets()
        _clients[api_key] = c
    return c


def _drop_client(api_key: str) -> None:
    _clients.pop(api_key, None)


# ── status file (scanner-owned) ─────────────────────────────────────────────
def _read_status() -> dict:
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_status(user_id: int, **fields) -> None:
    st = _read_status()
    rec = st.get(str(user_id), {})
    rec.update(fields)
    rec["last_ts"] = time.time()
    st[str(user_id)] = rec
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATUS_FILE)


def status_for(user_id: int) -> dict:
    return _read_status().get(str(user_id), {})


# ── validation (read-only, used at enrollment) ──────────────────────────────
def _probe_client(api_key: str, api_secret: str):
    """A FRESH (uncached) client for the read-only validation probe, so a bad
    key can't poison the mirror cache. Its own seam so tests can stub the
    network without stubbing validate()'s logic."""
    return ccxt.bybit({"apiKey": api_key, "secret": api_secret,
                       "enableRateLimit": True, "options": {"defaultType": "swap"}})


def validate(api_key: str, api_secret: str) -> dict:
    """Read-only connectivity check: can these keys read the balance? Returns
    {ok, equity, error}. Sends NO order."""
    try:
        c = _probe_client(api_key.strip(), api_secret.strip())
        raw = c.fetch_balance()
        acct = (((raw.get("info") or {}).get("result") or {}).get("list") or [{}])[0]
        eq = acct.get("totalEquity")
        return {"ok": True, "equity": float(eq) if eq not in (None, "") else None,
                "error": None}
    except ccxt.AuthenticationError as e:
        return {"ok": False, "equity": None,
                "error": f"金鑰無效或權限不足：{str(e)[:120]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "equity": None, "error": f"{type(e).__name__}: {e}"[:160]}


# ── the mirror actions ──────────────────────────────────────────────────────
def _each_follower():
    """Yield (rec_public_id, username, key, secret) for enabled+decryptable
    followers, recording a decrypt failure to status and skipping it."""
    for rec in copy_store.enabled_records():
        uid = rec.get("user_id")
        key, secret, err = copy_store.decrypt_creds(rec)
        if err:
            _write_status(uid, state="error", last_error=err,
                          last_action="decrypt")
            continue
        yield uid, rec.get("username"), key, secret, rec.get("margin_usdt")


def mirror_open(symbol: str, direction: str, price: float, sl_price: float,
                leverage: int) -> dict:
    """Open the same direction on every enabled follower, sized by their margin.
    Returns {live, done, failed, total}."""
    summary = {"live": live(), "done": 0, "failed": 0, "total": 0}
    followers = list(_each_follower())
    if not followers:
        return summary

    # 🛑 Account-wide daily loss limit. The trigger is OUR account's day, which
    # is the right reference: followers mirror our decisions, so if we have
    # stopped opening, they stop too.
    try:
        import daily_risk
        day_halt = daily_risk.entry_blocked()
    except Exception as exc:  # noqa: BLE001 — fails open
        print(f"[copy] daily-risk gate error ({exc}) — allowing entry")
        day_halt = ""
    if day_halt:
        print(f"[copy] open skipped — {day_halt}")
        _announce("開倉", symbol, direction, summary)
        return summary

    # Exchange lot-step / minimums are only needed to place a REAL order. In
    # dry-run we just log an unfloored estimate, so we never touch the exchange.
    step = min_qty = min_notional = None
    if summary["live"]:
        try:
            step, min_qty, min_notional = X._market_limits(symbol)
        except Exception as exc:  # noqa: BLE001 — no metadata ⇒ can't size anyone
            print(f"[copy] market limits failed {symbol}: {exc}")
            for uid, name, *_ in followers:
                summary["total"] += 1
                summary["failed"] += 1
                _write_status(uid, state="error", last_error=f"市場資料讀取失敗：{exc}",
                              last_action=f"OPEN {symbol} {direction}")
            return summary

    side = "buy" if direction == "long" else "sell"
    for uid, name, key, secret, margin in followers:
        summary["total"] += 1
        if not summary["live"]:
            qty = round(margin * leverage / price, 6)      # rough dry-run estimate
            summary["done"] += 1
            print(f"[copy][DRY-RUN] {name}: {direction.upper()} {symbol} "
                  f"~qty {qty} ({margin} USDT × {leverage}x) — COPY_TRADING_LIVE=false")
            _write_status(uid, state="dry", last_error=None, last_qty=qty,
                          last_action=f"OPEN {symbol} {direction} (dry)")
            continue
        qty, err = X.qty_for(price, margin, leverage, step, min_qty, min_notional)
        if err:
            summary["failed"] += 1
            _write_status(uid, state="error", last_error=f"下單量不足：{err}",
                          last_action=f"OPEN {symbol} {direction}")
            print(f"[copy] {name}: size error {symbol}: {err}")
            continue
        try:
            c = _client(key, secret)
            try:
                c.set_leverage(leverage, symbol)
            except Exception as exc:  # noqa: BLE001 — 110043 not-modified is benign
                if "110043" not in str(exc) and "not modified" not in str(exc).lower():
                    print(f"[copy] {name}: set_leverage warn {symbol}: {exc}")
            # Followers run their own accounts, so their position mode is
            # cached under their own uid — never inherited from ours.
            order = bybit_mode.send_with_mode(
                bybit_mode.scope_key(uid, symbol), direction,
                lambda pidx, c=c, qty=qty, side=side:
                c.create_order(symbol, "market", side, qty, params={
                    "positionIdx": pidx,
                    "stopLoss": c.price_to_precision(symbol, sl_price),
                }))
            summary["done"] += 1
            _write_status(uid, state="active", last_error=None, last_qty=qty,
                          last_order_id=(order or {}).get("id"),
                          last_action=f"OPEN {symbol} {direction}")
            print(f"[copy] {name}: OPENED {direction} {symbol} qty {qty}")
        except Exception as exc:  # noqa: BLE001 — isolate: one bad account only
            summary["failed"] += 1
            _mark_order_error(uid, name, key, exc, f"OPEN {symbol} {direction}")
    _announce("開倉", symbol, direction, summary)
    return summary


def mirror_close(symbol: str, why: str = "") -> dict:
    """Reduce-only close of the follower's position on `symbol` (if any)."""
    summary = {"live": live(), "done": 0, "failed": 0, "total": 0}
    for uid, name, key, secret, _margin in _each_follower():
        summary["total"] += 1
        if not summary["live"]:
            summary["done"] += 1
            print(f"[copy][DRY-RUN] {name}: CLOSE {symbol} — COPY_TRADING_LIVE=false")
            _write_status(uid, state="dry", last_action=f"CLOSE {symbol} (dry)")
            continue
        try:
            c = _client(key, secret)
            pos = _position(c, symbol)
            if not pos:
                summary["done"] += 1                    # already flat ⇒ success
                _write_status(uid, state="active", last_error=None,
                              last_action=f"CLOSE {symbol} (flat)")
                continue
            close_side = "sell" if pos["side"] == "long" else "buy"
            # pos["side"], not close_side — the index names the position being
            # reduced. Getting this backwards on a follower's account would
            # leave their position open with somebody else's money at risk.
            bybit_mode.send_with_mode(
                bybit_mode.scope_key(uid, symbol), pos["side"],
                lambda pidx, c=c, pos=pos, close_side=close_side:
                c.create_order(symbol, "market", close_side, pos["qty"],
                               params={"positionIdx": pidx, "reduceOnly": True}))
            summary["done"] += 1
            _write_status(uid, state="active", last_error=None,
                          last_action=f"CLOSE {symbol}")
            print(f"[copy] {name}: CLOSED {symbol}")
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            _mark_order_error(uid, name, key, exc, f"CLOSE {symbol}")
    _announce("平倉", symbol, why, summary)
    return summary


def mirror_set_stop(symbol: str, sl_price: float) -> dict:
    """Move each follower's stop (e.g. break-even jump). Best-effort; a missing
    position is not an error."""
    summary = {"live": live(), "done": 0, "failed": 0, "total": 0}
    if not summary["live"]:
        return summary
    for uid, name, key, secret, _margin in _each_follower():
        summary["total"] += 1
        try:
            c = _client(key, secret)
            fpos = _position(c, symbol)
            if not fpos:
                summary["done"] += 1
                continue
            market_id = c.market(symbol)["id"]
            setter = getattr(c, "private_post_v5_position_trading_stop", None) or \
                getattr(c, "privatePostV5PositionTradingStop", None)
            if setter:
                bybit_mode.send_with_mode(
                    bybit_mode.scope_key(uid, symbol), fpos["side"],
                    lambda pidx, c=c, setter=setter, market_id=market_id:
                    setter({"category": "linear", "symbol": market_id,
                            "positionIdx": pidx, "tpslMode": "Full",
                            "stopLoss": c.price_to_precision(symbol, sl_price)}))
            summary["done"] += 1
            _write_status(uid, state="active", last_error=None,
                          last_action=f"STOP {symbol} → {sl_price:.6g}")
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            _mark_order_error(uid, name, key, exc, f"STOP {symbol}")
    return summary


# ── helpers ─────────────────────────────────────────────────────────────────
def _position(c, symbol: str):
    """The follower's open position on `symbol`, or None."""
    for p in c.fetch_positions([symbol]):
        try:
            qty = abs(float(p.get("contracts") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0:
            return {"side": "long" if p.get("side") == "long" else "short",
                    "qty": qty}
    return None


def _mark_order_error(uid, name, api_key, exc, action) -> None:
    msg = str(exc)
    if "position idx" in msg.lower():
        msg += "（持倉模式不符，已自動改用另一種模式重試仍失敗）"
    if isinstance(exc, ccxt.AuthenticationError):
        _drop_client(api_key)                 # key changed/revoked — stop reusing
        msg = "金鑰失效或被撤銷，請重新提交"
    _write_status(uid, state="error", last_error=msg[:200], last_action=action)
    print(f"[copy] {name}: order error ({action}): {msg[:200]}")


def _announce(what: str, symbol: str, extra: str, summary: dict) -> None:
    """One low-noise Telegram line per mirror action, only when there are
    followers and something to report. Failures always surface."""
    if not summary["total"]:
        return
    try:
        import telegram_utils
        sym = symbol.split("/")[0]
        tag = "" if summary["live"] else "（模擬）"
        line = (f"🪞 跟單{what}{tag} · {sym} — 成功 {summary['done']}/"
                f"{summary['total']}")
        if summary["failed"]:
            line += f" · ⚠️ {summary['failed']} 失敗（見 /health 或後台）"
        # channel="trades": this mirrors S3's live trades. "strategy3" was never
        # a registered channel, so _route() fell through to the alerts bot —
        # a destination nobody had chosen on purpose.
        telegram_utils.send_message(line, channel="trades", force=bool(summary["failed"]))
    except Exception as exc:  # noqa: BLE001 — never let a TG hiccup break trading
        print(f"[copy] announce failed: {exc}")
