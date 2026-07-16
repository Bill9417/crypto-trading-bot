"""
Price Alerts — user-set price levels, watched by the Strategy-2 scanner.

The dashboard's 🔔 Price Alerts card writes levels here (via /api/price_alerts
in app.py); the scanner calls tick(client) once per sweep, checks every active
level against ONE bulk fetch_tickers call, and on a cross sends a Telegram
alert (force=True — a level you set by hand always punches through quiet mode)
and marks the alert triggered. Triggered alerts stay visible on the card for a
day, then purge automatically.

Direction is fixed at creation time from the live price ("above" if the target
is over the market, else "below"), so a level only fires on the cross the user
meant — not instantly on creation, and not again on every wobble around it.

Shared file: price_alerts.json (web writes, scanner reads+marks). Writes are
atomic (tmp + os.replace), the same pattern every other cross-process JSON in
this repo uses.
"""
import json
import os
import time
import uuid

import telegram_utils

FILE = os.path.join(os.path.dirname(__file__), "price_alerts.json")

TRIGGERED_RETAIN_SEC = 24 * 3600      # keep fired alerts on the card for a day
MAX_ALERTS = 40                       # sanity cap on active levels


def load_alerts() -> list:
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("alerts", [])
    except Exception:  # noqa: BLE001 — missing/corrupt file = no alerts
        return []


def save_alerts(alerts: list) -> None:
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"alerts": alerts}, f)
    os.replace(tmp, FILE)


def _purge(alerts: list, now: float) -> list:
    """Drop triggered alerts older than the retention window."""
    return [a for a in alerts
            if not a.get("triggered") or now - a["triggered"] <= TRIGGERED_RETAIN_SEC]


def add_alert(symbol: str, price: float, ref_price: float) -> dict:
    """Create + persist one alert. Direction comes from where the target sits
    relative to the live price at creation. Raises ValueError on bad input."""
    price = float(price)
    ref_price = float(ref_price)
    if price <= 0 or ref_price <= 0:
        raise ValueError("price must be positive")
    if price == ref_price:
        raise ValueError("target equals the current price")
    now = time.time()
    alerts = _purge(load_alerts(), now)
    if sum(1 for a in alerts if not a.get("triggered")) >= MAX_ALERTS:
        raise ValueError(f"too many active alerts (max {MAX_ALERTS})")
    alert = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol,
        "base": symbol.split("/")[0],
        "price": price,
        "direction": "above" if price > ref_price else "below",
        "ref_price": ref_price,
        "created": now,
        "triggered": None,
        "triggered_price": None,
    }
    alerts.append(alert)
    save_alerts(alerts)
    return alert


def remove_alert(alert_id: str) -> bool:
    alerts = load_alerts()
    kept = [a for a in alerts if a.get("id") != alert_id]
    if len(kept) == len(alerts):
        return False
    save_alerts(kept)
    return True


def check_alerts(alerts: list, prices: dict, now: float) -> list:
    """Pure: mark crossed alerts triggered (mutates the list items), return the
    Telegram messages to send. `prices` maps symbol → last price."""
    msgs = []
    for a in alerts:
        if a.get("triggered"):
            continue
        last = prices.get(a.get("symbol"))
        if last is None:
            continue
        crossed = (last >= a["price"]) if a["direction"] == "above" else (last <= a["price"])
        if not crossed:
            continue
        a["triggered"] = now
        a["triggered_price"] = last
        arrow = "📈" if a["direction"] == "above" else "📉"
        dir_zh = "向上突破" if a["direction"] == "above" else "向下跌破"
        msgs.append(f"🔔 到價提醒 PRICE ALERT · {arrow} {a['base']} {dir_zh} "
                    f"{a['price']:,.6g}\n現價 {last:,.6g} · 設定於 "
                    f"{time.strftime('%m-%d %H:%M', time.localtime(a['created']))} "
                    f"@ {a['ref_price']:,.6g}")
    return msgs


def tick(client) -> int:
    """Scanner hook: check active levels against live tickers; returns the
    number of alerts fired. Skips the ticker fetch when nothing is active."""
    now = time.time()
    raw = load_alerts()
    alerts = _purge(raw, now)
    active = [a for a in alerts if not a.get("triggered")]
    if not active:
        if len(alerts) != len(raw):
            save_alerts(alerts)               # persist the purge
        return 0
    try:
        tickers = client.call("fetch_tickers", [a["symbol"] for a in active])
    except Exception as exc:  # noqa: BLE001 — a ticker blip just waits a sweep
        print(f"[alerts] ticker fetch failed: {exc}")
        return 0
    prices = {}
    for sym, t in (tickers or {}).items():
        try:
            last = float(t.get("last") or t.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if last > 0:
            prices[sym] = last
    msgs = check_alerts(alerts, prices, now)
    fired = 0
    for msg in msgs:
        try:
            if telegram_utils.send_message(msg, force=True, channel="alerts"):
                fired += 1
            print(f"[alerts] {msg.splitlines()[0]}")
        except Exception as exc:  # noqa: BLE001 — one send must not kill the tick
            print(f"[alerts] send failed: {exc}")
    save_alerts(alerts)
    return fired
