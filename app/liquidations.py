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
      (BYBIT_SYMBOLS). ⚠ OPPOSITE convention from Binance: "S" is the
      POSITION side (docs: a "Buy" update means a LONG was liquidated).
      "p" is the BANKRUPTCY price, not the traded price.
  • OKX            wss://ws.okx.com:8443/ws/v5/public
      liquidation-orders channel, instType=SWAP → ALL swaps in one stream.
      Rows carry posSide directly. "bkPx" is the bankruptcy price too.
      Needs an app-level "ping" every <30s.

Every event is normalised to:
    {ts_ms, exchange, symbol (base), side ('long'/'short' = the side that got
     liquidated), value_usdt, px (the price the liquidation printed at)}
px is only attached when it is a REAL traded price — Binance's `ap` (average
fill). Bybit/OKX report bankruptcy prices, which sit beyond where the tape
actually printed (visibly so at low leverage), so those events carry px=0 and
the price displays (recent prints, price band) show Binance fills only. The
bankruptcy price is still used for the USD value estimate — at the high
leverage typical of liquidations it is within a couple percent of the fill.
and pushed into one deque trimmed to WINDOW_SEC. aggregate() slices it into
the long/short totals, largest single event, per-exchange and per-symbol
breakdowns and an hourly heat series the /api/liquidations route serves.

start() is idempotent and spawns daemon threads — call it lazily from the
route so importing this module (e.g. in tests) opens no sockets.
"""
import json
import os
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

# ── persistence ──────────────────────────────────────────────────────────────
# The buffer used to live only in memory, so every web restart threw away the
# whole 24h window — and the web process restarts often. That is why the
# liquidation views kept rendering empty: not "the market was quiet", but
# "the collector was seconds old again". There is no free historical source
# to backfill from, so what is collected has to be kept.
#
# Two processes can run a collector (the web app and the S2 scanner). Both
# write atomically and MERGE on load, deduped, so the loser of a race loses
# at most its most recent slice rather than corrupting the file.
BUFFER_FILE = os.path.join(os.path.dirname(__file__), "liquidations_buffer.json")
SAVE_EVERY_SEC = 60
_last_save: float = 0.0


def _key(e: dict) -> tuple:
    return (e.get("ts"), e.get("ex"), e.get("sym"), e.get("side"), e.get("usd"))


def save_buffer() -> bool:
    """Atomically persist the in-window events. Best-effort by design — a
    failure here must never disturb collection."""
    global _last_save
    try:
        with _lock:
            evs = list(_events)
        cutoff = (time.time() - WINDOW_SEC) * 1000
        evs = [e for e in evs if e.get("ts", 0) >= cutoff]
        tmp = BUFFER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "events": evs}, f)
        os.replace(tmp, BUFFER_FILE)
        _last_save = time.time()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[liq] buffer save failed: {exc}")
        return False


def load_buffer() -> int:
    """Merge the persisted window back in, deduped and re-trimmed. Returns how
    many events were adopted."""
    try:
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            saved = (json.load(f) or {}).get("events") or []
    except Exception:  # noqa: BLE001 — absent/corrupt = start empty
        return 0
    cutoff = (time.time() - WINDOW_SEC) * 1000
    with _lock:
        seen = {_key(e) for e in _events}
        adopted = [e for e in saved
                   if e.get("ts", 0) >= cutoff and _key(e) not in seen]
        merged = sorted(list(_events) + adopted, key=lambda e: e.get("ts", 0))
        _events.clear()
        _events.extend(merged)
    return len(adopted)


def _maybe_save() -> None:
    if time.time() - _last_save >= SAVE_EVERY_SEC:
        save_buffer()


def _push(ts_ms: int, exchange: str, symbol: str, side: str, value_usdt: float,
          px: float = 0.0) -> None:
    if not symbol or side not in ("long", "short") or value_usdt <= 0:
        return
    base = symbol.replace("USDT", "").replace("-SWAP", "").replace("-", "")
    with _lock:
        _events.append({"ts": ts_ms, "ex": exchange, "sym": base,
                        "side": side, "usd": value_usdt, "px": px})
        cutoff = (time.time() - WINDOW_SEC) * 1000
        while _events and _events[0]["ts"] < cutoff:
            _events.popleft()
    _maybe_save()


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
    _push(int(o.get("T") or 0), "Binance", o.get("s") or "", side, qty * px, px)


def _on_bybit(msg: str) -> None:
    d = json.loads(msg) or {}
    if "allLiquidation" not in str(d.get("topic", "")):
        return
    for r in d.get("data") or []:
        qty = float(r.get("v") or 0)
        px = float(r.get("p") or 0)          # bankruptcy price — value only, never displayed
        # Bybit's S is the POSITION side ("Buy" = a long was liquidated) —
        # the opposite convention from Binance's order side.
        side = "long" if r.get("S") == "Buy" else "short"
        _push(int(r.get("T") or 0), "Bybit", r.get("s") or "", side, qty * px, 0.0)


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
                px = float(det.get("bkPx") or 0)   # bankruptcy price — value only, never displayed
            except (TypeError, ValueError):
                continue
            side = det.get("posSide")
            if side not in ("long", "short"):
                side = "long" if det.get("side") == "sell" else "short"
            _push(int(det.get("ts") or 0), "OKX", base, side, sz * ct_val * px, 0.0)


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
    adopted = load_buffer()
    if adopted:
        # started_at must reflect the OLDEST data we hold, or the UI claims a
        # 24h window is "3 seconds old" and looks broken
        with _lock:
            if _events:
                _started_at = min(_started_at, _events[0]["ts"] / 1000.0)
        print(f"[liq] restored {adopted} events from the saved buffer")
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


def events_copy() -> list:
    """Thread-safe copy of the raw event buffer (for liq_alerts)."""
    with _lock:
        return list(_events)
