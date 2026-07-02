"""
Strategy 2 — 15m signal scanner (ALERT + PAGE only, never places orders).

Stand-alone process: it has its OWN Binance REST client so it never competes with
the live S1 bot's rate budget. Every cycle it sweeps all USDT perps on the 15m
timeframe, runs strategy2_meter.compute_signal (the green/red-triangle arm-and-fire
that mirrors TV.pine), and on a NEW signal it:

  • appends it to strategy2_signals.json  → the /strategy2 page "Live 15m Signals"
  • sends a Telegram alert (deduped per symbol+direction with a cooldown)

By DEFAULT it is alert-only — it never touches the executor or the live account,
you read the signal and decide. If you opt in with STRATEGY2_LIVE=true it ALSO
hands each new high-conviction signal to strategy2_live.maybe_trade, which places
a real bracketed order (see that module for the full safety model). With the
default config STRATEGY2_LIVE is False, so nothing is ever ordered.

Run detached (like the bot/web):
    nohup ./run_strategy2.sh >/dev/null 2>&1 & disown
"""
import json
import os
import time

import config
import executor
import strategy2_live as S2L
import strategy2_meter as S2
import telegram_utils
from market_data import SafeBinanceClient, RateLimitCooldownError

# ── tunables (env-overridable, sensible defaults) ────────────────────────────
TIMEFRAME = os.getenv("STRATEGY2_TIMEFRAME", "15m")
INTERVAL_SEC = int(os.getenv("STRATEGY2_INTERVAL_SEC", "300"))      # gap between full sweeps
ALERT_COOLDOWN_SEC = int(os.getenv("STRATEGY2_ALERT_COOLDOWN_SEC", "14400"))  # 4h per symbol+dir
CANDLES = int(os.getenv("STRATEGY2_CANDLES", "400"))               # ≥ SIGNAL_MIN_CANDLES (347)
RETAIN_HOURS = float(os.getenv("STRATEGY2_RETAIN_HOURS", "24"))    # how long signals stay on the page
MAX_KEEP = int(os.getenv("STRATEGY2_MAX_KEEP", "60"))
DIGEST_SEC = int(os.getenv("STRATEGY2_DIGEST_SEC", "1800"))        # Telegram: one grouped digest every 30 min
# Meter scores for the N most-liquid symbols are persisted every sweep → the
# /strategy2 heatmap. Universe is volume-sorted, so these finish early in a sweep.
HEATMAP_TOP = int(os.getenv("STRATEGY2_HEATMAP_TOP", "40"))
# High-conviction fired signals (the same score bar live execution uses) send an
# IMMEDIATE Telegram alert that bypasses quiet mode — these are the "I would take
# this trade myself" moments. The 30-min digest still covers everything else.
HC_ALERT = os.getenv("STRATEGY2_HC_ALERT", "true").strip().lower() in ("1", "true", "yes", "on")

SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")

# Latest meter score per top symbol, refreshed in place during each sweep.
LATEST_SCORES: dict = {}


def _tv_url(symbol: str) -> str:
    base = symbol.split("/")[0].split(":")[0]
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}{config.QUOTE_ASSET}.P"


def universe(client) -> list:
    """All active USDT-margined perps, most-liquid first (so hot coins scan first)."""
    markets = client.call("load_markets")
    syms = [s for s, m in markets.items()
            if m.get("swap") and m.get("quote") == config.QUOTE_ASSET and m.get("active", True)
            and s.endswith(":" + config.QUOTE_ASSET)]
    try:
        tickers = client.call("fetch_tickers")
        syms.sort(key=lambda s: (tickers.get(s, {}) or {}).get("quoteVolume") or 0, reverse=True)
    except Exception as exc:  # noqa: BLE001 — sorting is a nicety, not required
        print(f"[strategy2] ticker sort skipped: {exc}")
    return syms


def _load_recent() -> list:
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("signals", [])
    except Exception:  # noqa: BLE001
        return []


def _write(recent: list, scanning: int, done: int) -> None:
    cutoff = time.time() - RETAIN_HOURS * 3600
    recent = [s for s in recent if s.get("ts", 0) >= cutoff][:MAX_KEEP]
    payload = {
        "generated_at": time.time(),
        "last_scan_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timeframe": TIMEFRAME,
        # Whether THIS scanner is the live engine — lets the dashboard tell an
        # alert-only scanner (running alongside S1) from one armed to trade.
        "live": bool(config.STRATEGY2_LIVE),
        "scanning": scanning,
        "done": done,
        "signals": recent,
        # Volume-ranked meter scores for the heatmap (rank preserved via list order).
        "scores": list(LATEST_SCORES.values()),
    }
    tmp = SIGNALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, SIGNALS_FILE)


def _fmt_price(p) -> str:
    try:
        return f"{float(p):.6g}"
    except (TypeError, ValueError):
        return str(p)


def _send_digest(sigs: list) -> None:
    """Send ONE clean Strategy-2 Telegram digest for the signals collected since
    the last one — grouped by direction, highest conviction first. Called on the
    DIGEST_SEC cadence (default every 30 min) instead of a message per signal, so
    the channel gets a single tidy summary rather than a stream of alerts."""
    if not sigs:
        return
    longs = sorted([s for s in sigs if s["direction"] == "long"], key=lambda s: -s["score"])
    shorts = sorted([s for s in sigs if s["direction"] == "short"], key=lambda s: s["score"])
    lines = [f"📊 STRATEGY 2 · {TIMEFRAME} signals",
             f"— last {DIGEST_SEC // 60} min · {len(sigs)} new —"]
    if longs:
        lines.append("\n🟢 LONG")
        lines += [f"  • {s['base']}  ·  {s['score']}/100  ·  {_fmt_price(s['price'])}" for s in longs]
    if shorts:
        lines.append("\n🔴 SHORT")
        lines += [f"  • {s['base']}  ·  {s['score']}/100  ·  {_fmt_price(s['price'])}" for s in shorts]
    try:
        telegram_utils.send_message("\n".join(lines))
    except Exception as exc:  # noqa: BLE001 — a failed alert must never kill the loop
        print(f"[strategy2] digest send failed: {exc}")


def scan_once(client, recent: list, last_alert: dict, pending: list) -> list:
    syms = universe(client)
    total = len(syms)
    print(f"[strategy2] scanning {total} {TIMEFRAME} perps…")
    for i, sym in enumerate(syms):
        try:
            ohlcv = client.call("fetch_ohlcv", sym, TIMEFRAME, None, CANDLES)
        except RateLimitCooldownError as exc:
            print(f"[strategy2] cooldown: {exc}; pausing 30s")
            time.sleep(30)
            continue
        except Exception:  # noqa: BLE001 — a bad/new symbol must not kill the sweep
            continue

        try:
            res = S2.compute_signal(ohlcv)
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] compute error {sym}: {exc}")
            continue

        if i < HEATMAP_TOP:
            LATEST_SCORES[sym] = {
                "symbol": sym, "base": sym.split("/")[0],
                "score": res.get("score"), "price": res.get("price"),
                "ts": time.time(), "tv_url": _tv_url(sym),
            }

        if res.get("signal"):
            direction = res["signal"]
            key = (sym, direction)
            now = time.time()
            if now - last_alert.get(key, 0) >= ALERT_COOLDOWN_SEC:
                last_alert[key] = now
                sig = {
                    "symbol": sym,
                    "base": sym.split("/")[0],
                    "direction": direction,
                    "score": res["score"],
                    "price": res["price"],
                    "ts": now,
                    "tv_url": _tv_url(sym),
                }
                recent.insert(0, sig)
                pending.append(sig)                  # buffered → next 30-min digest
                print(f"[strategy2] SIGNAL {direction.upper()} {sig['base']} score {sig['score']}")
                # High-conviction (live-entry-grade) signals alert IMMEDIATELY and
                # punch through quiet mode. Deduped by the same 4h cooldown above.
                hc = (direction == "long" and sig["score"] >= config.STRATEGY2_LIVE_MIN_SCORE) or \
                     (direction == "short" and sig["score"] <= 100 - config.STRATEGY2_LIVE_MIN_SCORE)
                if HC_ALERT and hc:
                    try:
                        telegram_utils.send_message(
                            f"🎯 S2 HIGH CONVICTION · {direction.upper()} {sig['base']}\n"
                            f"score {sig['score']}/100 @ {_fmt_price(sig['price'])} ({TIMEFRAME})\n"
                            f"{sig['tv_url']}",
                            force=True)
                    except Exception as exc:  # noqa: BLE001 — alert must never kill the sweep
                        print(f"[strategy2] HC alert failed {sym}: {exc}")
                _write(recent, total, i + 1)        # surface on the page immediately
                # Opt-in LIVE execution — a no-op unless STRATEGY2_LIVE is on. The
                # best-plan filter + all safety gates live inside maybe_trade; `i`
                # is the volume rank (universe is sorted most-liquid first).
                try:
                    S2L.maybe_trade(sig, ohlcv, rank=i)
                except Exception as exc:  # noqa: BLE001 — live exec must never kill the sweep
                    print(f"[strategy2] live exec error {sym}: {exc}")

        if (i + 1) % 25 == 0:
            _write(recent, total, i + 1)            # progress for the page

    _write(recent, total, total)
    return recent


def main() -> None:
    if config.STRATEGY2_LIVE:
        net = "DRY-RUN" if not config.LIVE_TRADING else (
            "TESTNET" if config.USE_TESTNET else "LIVE MAINNET")
        mode = (f"LIVE EXECUTION ON ({net}) — high-conviction signals "
                f"(long ≥{config.STRATEGY2_LIVE_MIN_SCORE} / short ≤"
                f"{100 - config.STRATEGY2_LIVE_MIN_SCORE}) will place bracketed orders. "
                f"STOP THE S1 BOT FIRST — one engine at a time.")
    else:
        mode = "ALERT-ONLY: no orders are ever placed (set STRATEGY2_LIVE=true to trade)."
    print(f"[strategy2] 15m signal scanner starting — interval {INTERVAL_SEC}s, "
          f"cooldown {ALERT_COOLDOWN_SEC}s. {mode}")
    client = SafeBinanceClient(
        min_rest_interval=float(os.getenv("STRATEGY2_REST_INTERVAL", "0.25")),
        max_retries=3,
    )
    recent = _load_recent()
    last_alert = {}
    pending = []                    # new signals awaiting the next digest
    last_digest = time.time()       # cadence anchor for the 30-min digest

    def _guard():
        """Keep the live account consistent every cycle. When S2 is the live
        engine it manages the account, so each sweep it (1) reconciles closed
        positions — freeing concurrency-cap slots and cancelling stale SL/TP so
        the engine never wedges shut or fires an old stop at a new position — and
        (2) auto-sets a stop on any naked position (orphaned/manual entries, or a
        bracket that failed to place). No-op in dry-run / alert-only."""
        if not config.STRATEGY2_LIVE:
            return
        try:
            executor.reconcile_open_positions()    # release cap slots + cancel stale orders
        except Exception as exc:  # noqa: BLE001 — reconcile must never kill the loop
            print(f"[strategy2] reconcile error: {exc}")
        try:
            executor.ensure_stop_losses()
        except Exception as exc:  # noqa: BLE001 — protection must never kill the loop
            print(f"[strategy2] guardian error: {exc}")

    _guard()                                   # protect immediately on startup
    while True:
        start = time.time()
        _guard()                               # …and at the top of every sweep
        try:
            recent = scan_once(client, recent, last_alert, pending)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            print(f"[strategy2] sweep error: {exc}")
        # Flush a single grouped Telegram digest on a clean DIGEST_SEC cadence
        # (default every 30 min). Silent when nothing new fired in the window.
        if time.time() - last_digest >= DIGEST_SEC:
            if pending:
                _send_digest(pending)
                print(f"[strategy2] sent digest of {len(pending)} signal(s)")
                pending.clear()
            last_digest = time.time()
        elapsed = time.time() - start
        sleep_for = max(15, INTERVAL_SEC - elapsed)
        print(f"[strategy2] sweep done in {elapsed:.0f}s; sleeping {sleep_for:.0f}s")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
