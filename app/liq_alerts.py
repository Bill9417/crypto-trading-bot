"""
💥 Liquidation-cascade alerts — BTC/ETH stop-run detection into the Telegram
"liq" topic.

The thesis this serves (the user's): price gets pushed into the zones where
crowded stops/liquidations sit, clears them, and often reverses. There is no
free source for WHERE the clusters sit (Coinglass-style heatmaps are paid), so
this module does the honest half: it watches liquidations as they PRINT —
liquidations.py's Binance+Bybit+OKX WebSocket buffer — and alerts the moment a
burst of longs or shorts gets force-closed. A one-sided burst IS the stop-run
footprint, in real time.

Alert rule (per symbol, checked every scanner sweep):
  rolling LIQ_ALERT_WINDOW_SEC (10 min) liquidation total ≥ its USD threshold
    → 💥 alert: side cleared, total/count/largest, price context, 24h tally.
  Dominant side ≥70% is called 多單被掃 / 空單被掃, otherwise 雙向絞殺.
  Cooldown LIQ_ALERT_COOLDOWN_SEC (30 min) per symbol; inside the cooldown
  only a ≥3× bigger burst re-alerts (cascade escalation, not spam).

Thresholds are env-tunable and deliberately conservative defaults — Binance's
stream is throttled (1 event/sym/sec) so real bursts print LOW, and these
totals still cross only on genuine cascades:
  LIQ_ALERT_BTC_USD=2000000   LIQ_ALERT_ETH_USD=1500000

/liq additionally shows the MOST RECENT liquidation prints (side, size and the
exact PRICE they executed at — Binance fills only: Bybit/OKX report bankruptcy
prices, which sit beyond the real tape, so they carry no px) and a 🧲
liquidation MAP — an estimate of where
the un-swept liquidation/stop clusters sit. The map is honest modelling, not
data: real cluster maps (Coinglass heatmaps) are paid, so we approximate the
standard way — every 1h candle of the last 7 days is treated as positions
opened at its close (weighted by traded volume), projected to the liquidation
prices of the common leverage tiers (10/25/50/100x → ±10/4/2/1%), and levels
that price has ALREADY swept since are removed. What remains are the magnets
above and below the current price.

Alerts are INFORMATION, not entry signals — nothing here is backtested edge.
The collector buffer starts empty on every restart (collecting_since says how
much history is behind the numbers). /liq in the group returns the summary.
"""
import os
import time

import requests

import liquidations

SYMBOLS = (
    ("BTC", "BTC/USDT", float(os.getenv("LIQ_ALERT_BTC_USD", "2000000"))),
    ("ETH", "ETH/USDT", float(os.getenv("LIQ_ALERT_ETH_USD", "1500000"))),
)
WINDOW_SEC = int(os.getenv("LIQ_ALERT_WINDOW_SEC", "600"))
COOLDOWN_SEC = int(os.getenv("LIQ_ALERT_COOLDOWN_SEC", "1800"))
ESCALATE_MULT = 3.0     # inside the cooldown, re-alert only on a 3× bigger burst
DOMINANCE = 0.70        # one side ≥70% → that side "was cleared"

# Assumed leverage mix for the 🧲 map (no public data on the true mix — this
# is the modelling assumption, stated in the message footer).
TIERS = ((10, 0.30), (25, 0.30), (50, 0.25), (100, 0.15))
MAP_BUCKET_PCT = 0.0025          # cluster bucket width = 0.25% of price
MAP_TOP_N = 3

_last_alert: dict = {}  # base -> {"ts": epoch, "usd": alerted total}


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def window_stats(events: list, base: str, window_sec: int, now_ms: int = None) -> dict:
    """One symbol's liquidation totals over the trailing window."""
    now_ms = now_ms or int(time.time() * 1000)
    cutoff = now_ms - window_sec * 1000
    evs = [e for e in events if e["sym"] == base and e["ts"] >= cutoff]
    long_usd = sum(e["usd"] for e in evs if e["side"] == "long")
    short_usd = sum(e["usd"] for e in evs if e["side"] == "short")
    largest = max(evs, key=lambda e: e["usd"], default=None)
    pxs = [e["px"] for e in evs if e.get("px")]
    return {"long_usd": long_usd, "short_usd": short_usd,
            "total_usd": long_usd + short_usd, "n": len(evs), "largest": largest,
            "px_min": min(pxs) if pxs else None,
            "px_max": max(pxs) if pxs else None}


def classify_side(stats: dict) -> str:
    """'long' / 'short' (that side was ≥70% of the burst) or 'mixed'."""
    total = stats["total_usd"]
    if total <= 0:
        return "mixed"
    if stats["long_usd"] / total >= DOMINANCE:
        return "long"
    if stats["short_usd"] / total >= DOMINANCE:
        return "short"
    return "mixed"


def should_alert(stats: dict, threshold: float, last: dict, now: float,
                 cooldown_sec: int = COOLDOWN_SEC) -> bool:
    if stats["total_usd"] < threshold:
        return False
    if last and now - last["ts"] < cooldown_sec:
        return stats["total_usd"] >= ESCALATE_MULT * last["usd"]
    return True


def _usd(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def _fpx(v: float) -> str:
    return f"{v:,.0f}" if v >= 100 else f"{v:,.2f}"


def _ago(ts_ms: int, now_ms: int) -> str:
    mins = max(0, (now_ms - ts_ms) // 60_000)
    return f"{mins}分前" if mins < 120 else f"{mins // 60}小時前"


def recent_liqs(events: list, base: str, n: int = 3, min_usd: float = 10_000,
                now_ms: int = None) -> list:
    """Newest→oldest formatted lines for the last liquidation prints with a
    price attached. 🔻 = a LONG died (price pressed down), 🔺 = a SHORT died."""
    now_ms = now_ms or int(time.time() * 1000)
    evs = [e for e in events
           if e["sym"] == base and e.get("px") and e["usd"] >= min_usd]
    evs.sort(key=lambda e: e["ts"], reverse=True)
    return [f"{'🔻多' if e['side'] == 'long' else '🔺空'} {_usd(e['usd'])} "
            f"@ {_fpx(e['px'])} · {e['ex']} · {_ago(e['ts'], now_ms)}"
            for e in evs[:n]]


# ── 🧲 liquidation map (estimate) ────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = "1h", limit: int = 168) -> list:
    """Binance USD-M public klines → [(ts, o, h, l, c, quote_vol), ...]."""
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": symbol, "interval": interval,
                             "limit": limit}, timeout=15)
    r.raise_for_status()
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
             float(k[7])) for k in r.json()]


def liq_map(klines: list, tiers=TIERS, bucket_pct: float = MAP_BUCKET_PCT) -> dict:
    """Estimated un-swept liquidation clusters from recent candles.

    Each candle's close = an entry anchor weighted by its quote volume; each
    leverage tier projects the liq price (long: entry×(1−1/L), short mirror).
    A level that price has ALREADY traded through since the anchor (the stops
    there are gone) is dropped. Returns the surviving clusters bucketed to
    bucket_pct of the current price, strongest first."""
    if len(klines) < 2:
        return {"price": klines[-1][4] if klines else 0.0, "long": [], "short": []}
    current = klines[-1][4]
    n = len(klines)
    # suffix extremes AFTER each index (what price has done since the anchor)
    min_low_after = [0.0] * n
    max_high_after = [0.0] * n
    lo, hi = float("inf"), 0.0
    for i in range(n - 1, -1, -1):
        min_low_after[i], max_high_after[i] = lo, hi
        lo, hi = min(lo, klines[i][3]), max(hi, klines[i][2])

    step = current * bucket_pct
    longs: dict = {}
    shorts: dict = {}
    for i, (ts, o, h, l, c, qv) in enumerate(klines):
        for lev, w in tiers:
            lp = c * (1 - 1.0 / lev)
            if lp < current and min_low_after[i] > lp:
                key = round(lp / step)
                longs[key] = longs.get(key, 0.0) + qv * w
            sp = c * (1 + 1.0 / lev)
            if sp > current and max_high_after[i] < sp:
                key = round(sp / step)
                shorts[key] = shorts.get(key, 0.0) + qv * w

    def _top(buckets):
        """Merge runs of adjacent buckets into one cluster (a broad magnet
        shouldn't show as three neighbouring rows), keep the strongest."""
        if not buckets:
            return []
        keys = sorted(buckets)
        groups, run = [], [keys[0]]
        for k in keys[1:]:
            if k - run[-1] <= 1:
                run.append(k)
            else:
                groups.append(run)
                run = [k]
        groups.append(run)
        levels = []
        for ks in groups:
            s = sum(buckets[k] for k in ks)
            px = sum(k * step * buckets[k] for k in ks) / s   # weighted centre
            levels.append((px, s))
        levels.sort(key=lambda x: x[1], reverse=True)
        return levels[:MAP_TOP_N]

    return {"price": current, "long": _top(longs), "short": _top(shorts)}


def fmt_map(m: dict, base: str) -> str:
    """🧲 text rendering — magnets above/below, bars scaled to the strongest."""
    if not (m.get("long") or m.get("short")):
        return f"🧲 {base} 清算地圖: 近期區間內無明顯殘留群集"
    peak = max((s for _p, s in (m["long"] + m["short"])), default=1.0)
    cur = m["price"]

    def _rows(levels, sign):
        rows = []
        for px, s in levels:
            bar = "▰" * max(1, round(5 * s / peak))
            rows.append(f"   {_fpx(px)} ({(px / cur - 1) * 100:+.1f}%) {bar}")
        return rows

    lines = [f"🧲 {base} 清算地圖 (估算) · 現價 {_fpx(cur)}"]
    if m["short"]:
        lines.append(" 上方磁鐵 (空單清算區):")
        lines += _rows(sorted(m["short"]), "+")           # ladder: closest first
    if m["long"]:
        lines.append(" 下方磁鐵 (多單清算區):")
        lines += _rows(sorted(m["long"], reverse=True), "-")
    return "\n".join(lines)


def build_alert(base: str, stats: dict, side: str, day: dict,
                ticker: dict = None, window_sec: int = WINDOW_SEC,
                liqmap: dict = None) -> str:
    mins = window_sec // 60
    head = {"long": f"💥 {base} 多單被掃 ↓",
            "short": f"💥 {base} 空單被掃 ↑",
            "mixed": f"💥 {base} 雙向絞殺"}[side]
    total, n = stats["total_usd"], stats["n"]
    pct_long = 100 * stats["long_usd"] / total if total else 0
    lines = [head,
             f"{mins}分鐘內清算 {_usd(total)} · {n} 筆 "
             f"(多 {pct_long:.0f}% / 空 {100 - pct_long:.0f}%)"]
    if stats.get("px_min") and stats.get("px_max"):
        lines.append(f"清算價格帶 {_fpx(stats['px_min'])} – {_fpx(stats['px_max'])}")
    lg = stats.get("largest")
    if lg:
        at = f" @ {_fpx(lg['px'])}" if lg.get("px") else ""
        lines.append(f"最大單筆 {_usd(lg['usd'])}{at} · {lg['ex']}")
    if ticker:
        px = ticker.get("last")
        pct = ticker.get("percentage")
        hi, lo = ticker.get("high"), ticker.get("low")
        bits = []
        if px:
            bits.append(f"價格 {px:,.0f}" if px >= 100 else f"價格 {px:,.2f}")
        if pct is not None:
            bits.append(f"24h {pct:+.1f}%")
        if hi and lo:
            bits.append(f"區間 {lo:,.0f}–{hi:,.0f}")
        if bits:
            lines.append(" · ".join(bits))
    if side == "long":
        lines.append("→ 下方停損已被清空 — 若價格快速收回，注意反轉")
    elif side == "short":
        lines.append("→ 上方停損已被清空 — 若價格快速回落，注意反轉")
    nxt = _next_magnet(liqmap, side)
    if nxt:
        px, dist = nxt
        lines.append(f"🧲 下一個磁鐵 ~{_fpx(px)} ({dist:+.1f}%)")
    lines += ["",
              f"24h 累計: 多單 {_usd(day['long_usd'])} / 空單 {_usd(day['short_usd'])}",
              "(資訊性警報，非進場訊號)"]
    return "\n".join(lines)


def _next_magnet(liqmap: dict, side: str):
    """After a sweep, the closest surviving cluster in the sweep's direction:
    longs cleared → next long-liq level below; shorts → next level above."""
    if not liqmap:
        return None
    cur = liqmap.get("price") or 0
    if not cur:
        return None
    if side == "long" and liqmap.get("long"):
        px = max(p for p, _s in liqmap["long"])          # closest below
    elif side == "short" and liqmap.get("short"):
        px = min(p for p, _s in liqmap["short"])         # closest above
    else:
        return None
    return px, (px / cur - 1) * 100


def fmt_summary(snap: dict, btc_day: dict, eth_day: dict,
                recent: dict = None, maps: dict = None) -> str:
    """/liq — 24h tallies + most-recent prints (with prices) + 🧲 maps."""
    lines = ["💥 清算 24h\n"]
    for base, d in (("BTC", btc_day), ("ETH", eth_day)):
        lines.append(f"{base}: 多單 {_usd(d['long_usd'])} / 空單 {_usd(d['short_usd'])}"
                     f" · {d['n']} 筆")
    lines.append(f"\n全市場: {_usd(snap.get('total_usd') or 0)} "
                 f"(多 {_usd(snap.get('long_usd') or 0)} / "
                 f"空 {_usd(snap.get('short_usd') or 0)})")
    lg = snap.get("largest")
    if lg:
        lines.append(f"最大單筆: {lg['symbol']} {('多' if lg['side'] == 'long' else '空')}單 "
                     f"{_usd(lg['usd'])} @ {lg['exchange']}")

    for base in ("BTC", "ETH"):
        rows = (recent or {}).get(base)
        if rows:
            lines += ["", f"🕐 {base} 最近清算:"] + [f"  {r}" for r in rows]
    for base in ("BTC", "ETH"):
        m = (maps or {}).get(base)
        if m:
            lines += ["", fmt_map(m, base)]
    if maps:
        lines.append("(地圖=量能×假設槓桿10/25/50/100x推估、已排除掃過區 — 僅供參考)")

    since = snap.get("collecting_since")
    if since:
        hrs = (time.time() - since) / 3600
        lines.append(f"\n(清算流收集中 {hrs:.1f} 小時 — 重啟後從零開始，"
                     f"爆量門檻 BTC {_usd(SYMBOLS[0][2])} / ETH {_usd(SYMBOLS[1][2])} per 10min)")
    else:
        lines.append("\n(收集器未啟動)")
    return "\n".join(lines)


def build_report() -> str:
    """Everything /liq shows, assembled from the live buffer + fresh klines."""
    events = liquidations.events_copy()
    recent = {b: recent_liqs(events, b) for b in ("BTC", "ETH")}
    maps = {}
    for base, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        try:
            maps[base] = liq_map(fetch_klines(sym))
        except Exception:  # noqa: BLE001 — the map is optional decoration
            continue
    return fmt_summary(liquidations.snapshot(86400),
                       window_stats(events, "BTC", 86400),
                       window_stats(events, "ETH", 86400),
                       recent=recent, maps=maps)


# ── orchestration ────────────────────────────────────────────────────────────
def start() -> None:
    """Start the WebSocket collector in this process (idempotent)."""
    liquidations.start()


def tick(client) -> int:
    """Check both symbols once; returns how many alerts were sent."""
    events = liquidations.events_copy()
    if not events:
        return 0
    sent = 0
    now = time.time()
    for base, pair, threshold in SYMBOLS:
        stats = window_stats(events, base, WINDOW_SEC)
        if not should_alert(stats, threshold, _last_alert.get(base), now):
            continue
        side = classify_side(stats)
        day = window_stats(events, base, 86400)
        ticker = None
        try:
            ticker = client.fetch_ticker(pair)
        except Exception:  # noqa: BLE001 — price context is optional
            pass
        liqmap = None
        try:
            liqmap = liq_map(fetch_klines(pair.replace("/", "")))
        except Exception:  # noqa: BLE001 — the magnet line is optional
            pass
        msg = build_alert(base, stats, side, day, ticker, liqmap=liqmap)
        import telegram_utils
        if telegram_utils.send_message(msg, force=True, channel="liq"):
            sent += 1
            print(f"[liq] {base} cascade alert: {_usd(stats['total_usd'])} ({side})")
        _last_alert[base] = {"ts": now, "usd": stats["total_usd"]}
    return sent
