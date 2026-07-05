"""
Live futures-liquidation tracker — Binance + Bybit + OKX public WebSockets.

There is NO free REST endpoint for historical liquidations (Binance removed
theirs in 2021; Coinglass-style aggregators are paid), so this module does the
only honest thing possible for free: it LISTENS to each exchange's public
liquidation stream and keeps a rolling in-memory window. Numbers therefore
start at zero on every web restart and grow as events arrive — the API reports
`collecting_since` so the UI can say how much history is actually behind them.

Streams:
  • Binance USD-M  wss://fstream.binance.com/ws/!forceOrder@arr
      ALL symbols in one stream. NOTE Binance throttles it to at most one
      order per symbol per second — bursts are under-counted, which is fine
      for a dashboard read (the bias is the same for longs and shorts).
      A SELL force-order means a LONG position was liquidated.
  • Bybit linear   wss://stream.bybit.com/v5/public/linear
      allLiquidation.{symbol}, per-symbol subscription → we cover the majors
      (BYBIT_SYMBOLS). Side "Sell" = long liquidated (order side is the
      closing side, same convention as Binance).
  • OKX            wss://ws.okx.com:8443/ws/v5/public
      liquidation-orders channel, instType=SWAP → ALL swaps in one stream.
      Rows carry posSide directly. Needs an app-level "ping" every <30s.

Every event is normalised to:
    {ts_ms, exchange, symbol (base), side ('long'/'short' = the side that got
     liquidated), value_usdt}
and pushed into one deque trimmed to WINDOW_SEC. aggregate() slices it into
the long/short totals, largest single event, per-exchange and per-symbol
breakdowns and an hourly heat series the /api/liquidations route serves.

start() is idempotent and spawns daemon threads — call it lazily from the
route so importing this module (e.g. in tests) opens no sockets.
"""
import json
import threading
import time
from collections import deque

import requests
import websocket

WINDOW_SEC = 24 * 3600
# Bybit needs per-symbol subs — cover the liquid majors (incl. our S3 coins).
BYBIT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
                 "1000PEPEUSDT", "HYPEUSDT", "XAUTUSDT", "BNBUSDT", "ADAUSDT")

_events: deque = deque()
_lock = threading.Lock()
_started = False
_started_at: float = 0.0
_status = {"binance": "idle", "bybit": "idle", "okx": "idle"}


def _push(ts_ms: int, exchange: str, symbol: str, side: str, value_usdt: float) -> None:
    if not symbol or side not in ("long", "short") or value_usdt <= 0:
        return
    base = symbol.replace("USDT", "").replace("-SWAP", "").replace("-", "")
    with _lock:
        _events.append({"ts": ts_ms, "ex": exchange, "sym": base,
                        "side": side, "usd": value_usdt})
        cutoff = (time.time() - WINDOW_SEC) * 1000
        while _events and _events[0]["ts"] < cutoff:
            _events.popleft()


# ── per-exchange message parsers ─────────────────────────────────────────────

def _on_binance(msg: str) -> None:
    d = json.loads(msg) or {}
    if "data" in d:                               # combined-stream wrapper
        d = d["data"] or {}
    o = d.get("o") or {}
    if not o:
        return
    qty = float(o.get("q") or 0)
    px = float(o.get("ap") or o.get("p") or 0)
    # SELL force order closes a LONG position.
    side = "long" if o.get("S") == "SELL" else "short"
    _push(int(o.get("T") or 0), "Binance", o.get("s") or "", side, qty * px)


def _on_bybit(msg: str) -> None:
    d = json.loads(msg) or {}
    if "allLiquidation" not in str(d.get("topic", "")):
        return
    for r in d.get("data") or []:
        qty = float(r.get("v") or 0)
        px = float(r.get("p") or 0)
        side = "long" if r.get("S") == "Sell" else "short"
        _push(int(r.get("T") or 0), "Bybit", r.get("s") or "", side, qty * px)


# OKX reports liquidation size in CONTRACTS, and the multiplier (ctVal, base
# units per contract — 0.01 for BTC-USDT-SWAP) is NOT in the stream message.
# Without it BTC notionals would be off by 100×, so specs are fetched once
# from the public instruments endpoint and cached for the process lifetime.
_okx_ctval: dict = {}


def _load_okx_ctval() -> None:
    try:
        r = requests.get("https://www.okx.com/api/v5/public/instruments",
                         params={"instType": "SWAP"}, timeout=15)
        for row in (r.json() or {}).get("data") or []:
            try:
                _okx_ctval[row["instId"]] = float(row.get("ctVal") or 1)
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001 — retried on next reconnect
        pass


def _on_okx(msg: str) -> None:
    d = json.loads(msg) or {}
    if (d.get("arg") or {}).get("channel") != "liquidation-orders":
        return
    for row in d.get("data") or []:
        inst = row.get("instId") or ""            # e.g. BTC-USDT-SWAP
        if "-USDT-" not in inst:
            continue                              # skip coin-margined contracts
        base = inst.split("-")[0]
        ct_val = _okx_ctval.get(inst)
        if ct_val is None:
            continue                              # specs not loaded yet — skip, don't guess
        for det in row.get("details") or []:
            try:
                sz = float(det.get("sz") or 0)
                px = float(det.get("bkPx") or 0)
            except (TypeError, ValueError):
                continue
            side = det.get("posSide")
            if side not in ("long", "short"):
                side = "long" if det.get("side") == "sell" else "short"
            _push(int(det.get("ts") or 0), "OKX", base, side, sz * ct_val * px)


# ── connection management ────────────────────────────────────────────────────

def _run_ws(name: str, url: str, on_message, subscribe: dict = None,
            text_ping: str = None) -> None:
    """One exchange's reconnect-forever loop (daemon thread)."""
    while True:
        try:
            if name == "okx" and not _okx_ctval:
                _load_okx_ctval()

            def on_open(ws):
                _status[name] = "connected"
                if subscribe:
                    ws.send(json.dumps(subscribe))
                if text_ping:
                    def pinger():
                        while ws.keep_running:
                            time.sleep(20)
                            try:
                                ws.send(text_ping)
                            except Exception:  # noqa: BLE001
                                return
                    threading.Thread(target=pinger, daemon=True).start()

            def handle(ws, msg):
                try:
                    on_message(msg)
                except Exception:  # noqa: BLE001 — one bad frame must not kill the socket
                    pass

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=handle)
            ws.run_forever(ping_interval=15, ping_timeout=10)
        except Exception:  # noqa: BLE001
            pass
        _status[name] = "reconnecting"
        time.sleep(10)


def start() -> None:
    """Start the collector threads (idempotent, lazy — call from the route)."""
    global _started, _started_at
    with _lock:
        if _started:
            return
        _started = True
        _started_at = time.time()
    threads = [
        ("binance", "wss://fstream.binance.com/ws/!forceOrder@arr", _on_binance, None, None),
        ("bybit", "wss://stream.bybit.com/v5/public/linear", _on_bybit,
         {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in BYBIT_SYMBOLS]}, None),
        ("okx", "wss://ws.okx.com:8443/ws/v5/public", _on_okx,
         {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]},
         "ping"),
    ]
    for name, url, cb, sub, ping in threads:
        threading.Thread(target=_run_ws, args=(name, url, cb, sub, ping),
                         daemon=True, name=f"liq-{name}").start()


# ── aggregation (pure — unit-tested) ─────────────────────────────────────────

def aggregate(events, window_sec: int, now_ms: int = None) -> dict:
    """Slice a list of normalised events into the dashboard read."""
    now_ms = now_ms or int(time.time() * 1000)
    cutoff = now_ms - window_sec * 1000
    evs = [e for e in events if e["ts"] >= cutoff]

    long_usd = sum(e["usd"] for e in evs if e["side"] == "long")
    short_usd = sum(e["usd"] for e in evs if e["side"] == "short")
    largest = max(evs, key=lambda e: e["usd"], default=None)

    by_ex: dict = {}
    for e in evs:
        b = by_ex.setdefault(e["ex"], {"long": 0.0, "short": 0.0, "n": 0})
        b[e["side"]] += e["usd"]
        b["n"] += 1
    exchanges = [{"exchange": k, "long_usd": round(v["long"], 2),
                  "short_usd": round(v["short"], 2), "n": v["n"],
                  "total_usd": round(v["long"] + v["short"], 2)}
                 for k, v in by_ex.items()]
    exchanges.sort(key=lambda x: x["total_usd"], reverse=True)

    by_sym: dict = {}
    for e in evs:
        s = by_sym.setdefault(e["sym"], {"long": 0.0, "short": 0.0, "n": 0})
        s[e["side"]] += e["usd"]
        s["n"] += 1
    symbols = [{"symbol": k, "long_usd": round(v["long"], 2),
                "short_usd": round(v["short"], 2), "n": v["n"],
                "total_usd": round(v["long"] + v["short"], 2)}
               for k, v in by_sym.items()]
    symbols.sort(key=lambda x: x["total_usd"], reverse=True)

    # Hour-bucketed heat series (oldest → newest), long vs short per bucket.
    n_buckets = max(1, window_sec // 3600)
    bucket_ms = window_sec * 1000 // n_buckets
    series = [{"long_usd": 0.0, "short_usd": 0.0} for _ in range(n_buckets)]
    for e in evs:
        idx = min(n_buckets - 1, int((e["ts"] - cutoff) // bucket_ms))
        series[idx][f"{e['side']}_usd"] += e["usd"]
    for b in series:
        b["long_usd"] = round(b["long_usd"], 2)
        b["short_usd"] = round(b["short_usd"], 2)

    return {
        "window_sec": window_sec,
        "n_events": len(evs),
        "long_usd": round(long_usd, 2),
        "short_usd": round(short_usd, 2),
        "total_usd": round(long_usd + short_usd, 2),
        "largest": ({"symbol": largest["sym"], "side": largest["side"],
                     "usd": round(largest["usd"], 2), "exchange": largest["ex"],
                     "ts": largest["ts"]} if largest else None),
        "exchanges": exchanges,
        "symbols": symbols[:10],
        "series": series,
    }


def snapshot(window_sec: int = WINDOW_SEC) -> dict:
    """Thread-safe aggregate of the live buffer + collector status."""
    with _lock:
        evs = list(_events)
    out = aggregate(evs, min(window_sec, WINDOW_SEC))
    out["collecting_since"] = _started_at or None
    out["status"] = dict(_status)
    return out
