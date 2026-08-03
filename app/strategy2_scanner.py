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

import bybit_data
import tg_format
import config
import daily_report
import event_radar
import executor
import indicators
import price_alerts
import backup_state
import eth_mom
import liq_alerts
import morning_brief
import signal_outcomes
import tech_news
import tg_commands
import tw_intraday
import tw_stocks
import us_market
import watchdog
import whale_tracker
import strategy2_live as S2L
import strategy2_meter as S2
import telegram_utils
from market_data import SafeBinanceClient, RateLimitCooldownError, is_tradfi_market

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

# ── Pump Radar ────────────────────────────────────────────────────────────────
# Piggybacks on the sweep's already-fetched candles (zero extra API calls) to
# catch coins moving RIGHT NOW: 1h price change + last-hour volume vs its own
# 24h norm. Alert-grade movers (both thresholds crossed) send an immediate
# Telegram alert; the dashboard's 🚀 Pump Radar strip shows the top list.
MOVER_1H_PCT = float(os.getenv("MOVER_1H_PCT", "4"))            # |1h %| for alert grade
MOVER_VOL_MULT = float(os.getenv("MOVER_VOL_MULT", "3"))        # last-hour vol vs 24h norm
MOVER_ALERT_COOLDOWN_SEC = int(os.getenv("MOVER_ALERT_COOLDOWN_SEC", "7200"))
MOVER_KEEP = int(os.getenv("MOVER_KEEP", "12"))                 # rows on the dashboard
MOVER_STALE_SEC = 1800                                          # drop entries not re-seen

SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")

# Latest meter score per top symbol, refreshed in place during each sweep.
LATEST_SCORES: dict = {}
# Latest mover read per symbol, refreshed in place during each sweep.
LATEST_MOVERS: dict = {}


def _mover_metrics(ohlcv):
    """1h/24h change + last-hour volume vs its 24h average, on CLOSED 15m
    candles. Needs ~25h of history; returns None when there isn't enough."""
    o = ohlcv[:-1] if ohlcv else []                 # drop the forming candle
    if len(o) < 101:
        return None
    closes = [float(c[4]) for c in o]
    vols = [float(c[5]) for c in o]
    if not closes[-5] or not closes[-97]:
        return None
    vol_1h = sum(vols[-4:])
    prior = vols[-100:-4]                           # the 24h before this hour
    avg_1h = sum(prior) / len(prior) * 4
    return {
        "chg_1h": (closes[-1] / closes[-5] - 1) * 100,
        "chg_24h": (closes[-1] / closes[-97] - 1) * 100,
        "vol_mult": (vol_1h / avg_1h) if avg_1h > 0 else 0.0,
        "price": closes[-1],
    }


def _top_movers() -> list:
    """Freshest reads, biggest 1h move first, trimmed for the dashboard."""
    cutoff = time.time() - MOVER_STALE_SEC
    rows = [m for m in LATEST_MOVERS.values() if m["ts"] >= cutoff and abs(m["chg_1h"]) >= 1.0]
    rows.sort(key=lambda m: -abs(m["chg_1h"]))
    return rows[:MOVER_KEEP]


def _tv_url(symbol: str) -> str:
    base = symbol.split("/")[0].split(":")[0]
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}{config.QUOTE_ASSET}.P"


# ── BTC regime (once per sweep) ──────────────────────────────────────────────
# Mirrors bot.check_btc_regime: 1h EMA50, price above a RISING ema = bull,
# below a FALLING ema = bear, else neutral. Measured on 120 real fired
# signals + a 60d replay: trading WITH this regime was the single biggest
# win-rate lever, so it gates the ⭐ premium tier below.
def _btc_regime(client) -> str:
    import pandas as pd
    try:
        need = config.BTC_REGIME_EMA + config.BTC_REGIME_SLOPE_LOOKBACK + 5
        rows = client.call("fetch_ohlcv", config.BTC_REGIME_SYMBOL,
                           config.BTC_REGIME_TIMEFRAME, None, need)
        closes = [float(r[4]) for r in rows or []]
        if len(closes) < config.BTC_REGIME_EMA + config.BTC_REGIME_SLOPE_LOOKBACK:
            return "neutral"
        ema = pd.Series(closes).ewm(span=config.BTC_REGIME_EMA, adjust=False).mean()
        ema_now = ema.iloc[-1]
        ema_prev = ema.iloc[-1 - config.BTC_REGIME_SLOPE_LOOKBACK]
        if closes[-1] > ema_now and ema_now > ema_prev:
            return "bull"
        if closes[-1] < ema_now and ema_now < ema_prev:
            return "bear"
    except Exception as exc:  # noqa: BLE001 — regime is a filter, not a dependency
        print(f"[strategy2] BTC regime check failed: {exc}")
    return "neutral"


def _conviction(direction: str, score) -> int:
    """Score distance from neutral: long 85 ⇒ 85, short 15 ⇒ 85. Robust to a
    literal 0 score (a maximally-bearish short is conviction 100, not 0) —
    `score or 100` would mis-handle it if the trendline factor were ever
    un-pinned and pushed a score to exactly 0."""
    s = 0 if score is None else int(score)
    return s if direction == "long" else 100 - s


def classify_signal(direction: str, score, adx, regime: str) -> dict:
    """Tiering for one fired signal — pure, unit-testable.
    aligned = trades WITH the BTC regime; premium = the measured best gate
    (conviction + alignment + a real trend per ADX)."""
    aligned = (direction == "long" and regime == "bull") or \
              (direction == "short" and regime == "bear")
    against = (direction == "long" and regime == "bear") or \
              (direction == "short" and regime == "bull")
    conv = _conviction(direction, score)
    premium = (conv >= config.STRATEGY2_PREMIUM_MIN_SCORE
               and (aligned or not config.STRATEGY2_PREMIUM_REQUIRE_ALIGNED)
               and (adx or 0) >= config.STRATEGY2_PREMIUM_MIN_ADX)
    return {"aligned": aligned, "against": against, "conviction": conv,
            "premium": bool(premium)}


def digest_worthy(sig: dict) -> bool:
    """Does this signal clear the topic's digest bar? (Everything still
    shows on the /strategy2 page — this only limits Telegram noise.)"""
    if sig.get("against") and config.STRATEGY2_DIGEST_SKIP_COUNTER_BTC:
        return False
    return _conviction(sig.get("direction"), sig.get("score")) >= \
        config.STRATEGY2_DIGEST_MIN_CONV


def universe(client, tickers: dict = None) -> list:
    """All active USDT-margined perps, most-liquid first (so hot coins scan first).
    TradFi stock perps are dropped (EXCLUDE_TRADFI_PERPS) — the account can't
    trade them, so a signal there is a guaranteed -4411 at entry."""
    markets = client.call("load_markets")
    syms = [s for s, m in markets.items()
            if m.get("swap") and m.get("quote") == config.QUOTE_ASSET and m.get("active", True)
            and s.endswith(":" + config.QUOTE_ASSET)
            and not (config.EXCLUDE_TRADFI_PERPS and is_tradfi_market(m))]
    try:
        if tickers is None:
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
        # 🚀 Pump Radar rows for the dashboard strip.
        "movers": _top_movers(),
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


def premium_alert_text(sig: dict) -> str:
    """The ⭐ premium alert in the shared house style (tg_format): headline ·
    方向 · 標的 · 週期 / 信心·BTC·ADX / monospace 進場停損目標 block / Bybit link /
    免責. Only signals that passed the measured premium gate reach this."""
    direction = sig.get("direction")
    base = sig["base"]
    is_long = direction == "long"
    adx = sig.get("adx")
    head = f"⭐ 精選訊號 · {tg_format.dir_zh(direction)} {base} · {TIMEFRAME}"
    meta = [f"信心 {sig.get('score')}",
            "BTC同向" if sig.get("aligned") else "BTC盤整"]
    if adx:
        meta.append(f"ADX {adx:.0f}")
    lines = [head, " · ".join(meta)]
    plan = tg_format.mono_plan(sig.get("entry"), sig.get("sl"),
                               sig.get("tp1"), sig.get("tp2"), is_long=is_long)
    if plan:
        lines += [tg_format.DIV, plan, tg_format.DIV]
    lines.append(tg_format.bybit_line(base, sig.get("price")))
    lines.append("⚠️ 非投資建議 · 到目標先平半、停損移進場")
    return "\n".join(x for x in lines if x)


DIGEST_MAX_ROWS = int(os.getenv("STRATEGY2_DIGEST_MAX_ROWS", "12"))   # per side


def _digest_text(sigs: list) -> str:
    """ONE clean Strategy-2 digest, 中文為主 — grouped by direction, highest
    conviction first, capped at DIGEST_MAX_ROWS per side (a raw 80-signal
    chop-day list once blew Telegram's 4096-char limit and was lost).
    Row price = live BYBIT price when the coin trades there (one cached bulk
    ticker call), otherwise the Binance signal price."""
    longs = sorted([s for s in sigs if s["direction"] == "long"], key=lambda s: -s["score"])
    shorts = sorted([s for s in sigs if s["direction"] == "short"], key=lambda s: s["score"])
    lines = [f"📊 策略2 訊號榜 · {TIMEFRAME}",
             f"— 最近 {DIGEST_SEC // 60} 分鐘 · {len(sigs)} 個新訊號 —"]

    def _price_part(s):
        px = bybit_data.last_price(s["base"])
        return f"Bybit {_fmt_price(px)}" if px is not None else _fmt_price(s.get("price"))

    def _side(title, rows):
        if not rows:
            return
        lines.append(f"\n{title}")
        cells = [("", "幣種", "信心", "現價", "停損", "目標")]
        for s in rows[:DIGEST_MAX_ROWS]:
            has_plan = s.get("sl") and s.get("tp2")
            cells.append(("⭐" if s.get("premium") else "•", s["base"],
                          s["score"], _price_part(s),
                          _fmt_price(s["sl"]) if has_plan else "",
                          _fmt_price(s["tp2"]) if has_plan else ""))
        lines.append(tg_format.pre_table(cells, align="llrrrr"))
        if len(rows) > DIGEST_MAX_ROWS:
            lines.append(f"…還有 {len(rows) - DIGEST_MAX_ROWS} 個 (完整清單見網頁)")

    _side("🟢 做多", longs)
    _side("🔴 做空", shorts)
    lines.append("\n⭐ = 精選 (信心+BTC同向+趨勢確認) · 僅供參考，非投資建議")
    return "\n".join(lines)


def _send_digest(sigs: list) -> str:
    """Send the digest on the DIGEST_SEC cadence (default every 30 min) instead
    of a message per signal. Only signals clearing the digest bar (conviction
    ≥ STRATEGY2_DIGEST_MIN_CONV, not counter-BTC) reach Telegram — the raw
    70/30 firehose measured ~49% TP1-first / 65% eventual-SL, and sending
    coin-flips is what made the topic's win rate feel awful. Everything still
    shows on the /strategy2 page.

    Returns 'sent' | 'suppressed' | 'failed'. Three outcomes, not two: this
    used to return a bare False both when Telegram rejected the message AND
    when every signal was deliberately held back, so the caller logged a quiet
    market as "digest of 19 signal(s) FAILED". All 14 FAILED lines in
    strategy2.log were that — zero were real send errors — which is exactly the
    kind of false alarm that trains you to ignore the log.
    """
    rows = [s for s in sigs if digest_worthy(s)]
    if not rows:
        if sigs:
            print(f"[strategy2] digest: all {len(sigs)} signal(s) below the "
                  f"topic bar (conv<{config.STRATEGY2_DIGEST_MIN_CONV} or "
                  f"counter-BTC) — nothing sent")
        return "suppressed"
    try:
        ok = telegram_utils.send_message(_digest_text(rows),
                                         parse_mode="HTML", channel="signals")
        return "sent" if ok else "failed"
    except Exception as exc:  # noqa: BLE001 — a failed alert must never kill the loop
        print(f"[strategy2] digest send failed: {telegram_utils.redact(exc)}")
        return "failed"


# ── candle cache ─────────────────────────────────────────────────────────────
# Closed 15m candles only change once per 15-min bucket, but the sweep runs
# every ~5 min — so 2 of every 3 sweeps used to re-download identical data
# for ~530 symbols (measured 172s/sweep, all of it Binance rate-limit
# spacing). Now a symbol is re-fetched only when a NEW bucket has started;
# in between, the cached rows are reused with just the forming candle
# refreshed from the bulk ticker the sweep already fetches for volume
# sorting. Signal semantics are unchanged (closed bars identical, live
# price fresher than the old snapshot). Kill switch: STRATEGY2_OHLCV_CACHE.
OHLCV_CACHE_ON = os.getenv("STRATEGY2_OHLCV_CACHE", "true").strip().lower() \
    in ("1", "true", "yes", "on")
_TF_SEC = 900                       # matches TIMEFRAME (15m)
_ohlcv_cache: dict = {}             # sym -> {"bucket": int, "rows": list}


def _bucket(ts: float) -> int:
    return int(ts // _TF_SEC)


def _patch_forming(rows: list, px, now: float) -> list:
    """Refresh cached rows' forming candle with the live ticker price: update
    close, stretch high/low. Appends a synthetic forming candle when the
    cache snapshot ended exactly on a bucket boundary. Mutates in place so
    the intra-bucket high/low keeps accumulating across sweeps."""
    if not px:
        return rows
    cur_open_ms = _bucket(now) * _TF_SEC * 1000
    if rows and int(rows[-1][0]) >= cur_open_ms:
        last = rows[-1]
        last[2] = max(last[2], px)
        last[3] = min(last[3], px)
        last[4] = px
    else:
        rows.append([cur_open_ms, px, px, px, px, 0.0])
    return rows


def _get_ohlcv(client, sym: str, tickers: dict, now: float = None):
    """Cache-aware candle fetch (see the cache note above)."""
    now = now or time.time()
    if OHLCV_CACHE_ON:
        slot = _ohlcv_cache.get(sym)
        if slot and slot["bucket"] == _bucket(now):
            px = (tickers.get(sym) or {}).get("last")
            return _patch_forming(slot["rows"], px, now), True
    rows = client.call("fetch_ohlcv", sym, TIMEFRAME, None, CANDLES)
    if OHLCV_CACHE_ON and rows:
        _ohlcv_cache[sym] = {"bucket": _bucket(now), "rows": rows}
    return rows, False


def scan_once(client, recent: list, last_alert: dict, pending: list) -> list:
    btc_regime = _btc_regime(client)              # once per sweep, for the ⭐ gate
    premium_alerts_sent = 0                        # burst cap (per-sweep)
    try:
        tickers = client.call("fetch_tickers") or {}
    except Exception:  # noqa: BLE001 — tickers are reused for sort + cache
        tickers = {}
    syms = universe(client, tickers)
    total = len(syms)
    hits = 0
    for stale in [s for s in _ohlcv_cache if s not in set(syms)]:
        del _ohlcv_cache[stale]                 # delisted symbols leave the cache
    print(f"[strategy2] scanning {total} {TIMEFRAME} perps…")
    for i, sym in enumerate(syms):
        try:
            ohlcv, cached = _get_ohlcv(client, sym, tickers)
            hits += cached
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

        # 🚀 Pump Radar — reuses this symbol's candles, no extra API call.
        mv = _mover_metrics(ohlcv)
        if mv:
            hot = abs(mv["chg_1h"]) >= MOVER_1H_PCT and mv["vol_mult"] >= MOVER_VOL_MULT
            LATEST_MOVERS[sym] = {
                "symbol": sym, "base": sym.split("/")[0], "hot": hot,
                "ts": time.time(), "tv_url": _tv_url(sym), **mv,
            }
            key = ("mover", sym)
            now_ts = time.time()
            if hot and now_ts - last_alert.get(key, 0) >= MOVER_ALERT_COOLDOWN_SEC:
                last_alert[key] = now_ts
                arrow = "🚀" if mv["chg_1h"] > 0 else "📉"
                zh = "急拉" if mv["chg_1h"] > 0 else "急殺"
                base_ = sym.split("/")[0]
                print(f"[strategy2] MOVER {base_} {mv['chg_1h']:+.1f}% 1h "
                      f"vol {mv['vol_mult']:.1f}x")
                try:
                    by_line = bybit_data.price_line(base_, fallback_price=mv["price"])
                    telegram_utils.send_message(
                        f"{arrow} {zh} MOVER · {base_} 一小時 {mv['chg_1h']:+.1f}%\n"
                        f"成交量 {mv['vol_mult']:.1f}× 平常 · 24h {mv['chg_24h']:+.1f}%"
                        + (f"\n{by_line}" if by_line else "")
                        + f"\n{bybit_data.trade_url(base_) or _tv_url(sym)}",
                        force=True)
                except Exception as exc:  # noqa: BLE001 — alert must never kill the sweep
                    print(f"[strategy2] mover alert failed {sym}: {exc}")

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
                # ⭐ premium tiering — the measured win-rate gate (see config):
                # conviction + BTC-regime alignment + ADX trend confirmation.
                try:
                    adx = indicators.calculate_adx(ohlcv)
                except Exception:  # noqa: BLE001
                    adx = None
                tier = classify_signal(direction, sig["score"], adx, btc_regime)
                sig.update({"adx": round(adx, 1) if adx is not None else None,
                            "btc_regime": btc_regime, "aligned": tier["aligned"],
                            "against": tier["against"], "premium": tier["premium"]})
                # Trade plan attached to every fired signal, so the Telegram
                # alert/digest and the /strategy2 page show a complete
                # Entry/SL/TP plan, not just a naked price. ⭐ signals publish
                # the measured premium geometry; the rest keep the classic
                # 1.5×ATR / 1R / 2R plan (which live execution also uses).
                try:
                    levels_fn = (S2L.premium_trade_levels if sig["premium"]
                                 else S2L.trade_levels)
                    entry, sl, tp1, tp2 = levels_fn(
                        res["price"], direction == "long", S2L._atr(ohlcv))
                    sig.update({"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2})
                except Exception as exc:  # noqa: BLE001 — plan is a bonus, never blocks
                    print(f"[strategy2] plan calc failed {sym}: {exc}")
                # Fee-burn guard: a stop closer than STRATEGY2_MIN_STOP_PCT of
                # price is untradeable noise (USDC once fired 82 signals in 60d
                # with a 0.002% stop — fees alone ≈ 55R). Drop it entirely.
                if sig.get("sl") and sig.get("entry"):
                    stop_pct = abs(sig["entry"] - sig["sl"]) / sig["entry"]
                    if stop_pct < config.STRATEGY2_MIN_STOP_PCT:
                        print(f"[strategy2] drop {sig['base']} {direction}: stop "
                              f"{stop_pct * 100:.3f}% < "
                              f"{config.STRATEGY2_MIN_STOP_PCT * 100:.1f}% floor "
                              f"(fee-burn)")
                        continue
                recent.insert(0, sig)
                pending.append(sig)                  # buffered → next 30-min digest
                print(f"[strategy2] SIGNAL {direction.upper()} {sig['base']} "
                      f"score {sig['score']}"
                      f"{' ⭐PREMIUM' if sig['premium'] else ''}")
                # ⭐ premium signals alert IMMEDIATELY (bilingual, Bybit data)
                # and punch through quiet mode. Deduped by the 4h cooldown
                # above. This replaces the old score-only HC alert — measured
                # on real fires, score alone did NOT raise the win rate. Burst-
                # capped per sweep (STRATEGY2_PREMIUM_ALERTS_PER_SWEEP): extras
                # still ride the 30-min digest + the page, just no instant push.
                cap = config.STRATEGY2_PREMIUM_ALERTS_PER_SWEEP
                if HC_ALERT and sig["premium"] and (cap <= 0 or premium_alerts_sent < cap):
                    premium_alerts_sent += 1
                    try:
                        telegram_utils.send_message(
                            premium_alert_text(sig), force=True, channel="signals")
                    except Exception as exc:  # noqa: BLE001 — alert must never kill the sweep
                        print(f"[strategy2] premium alert failed {sym}: {exc}")
                elif HC_ALERT and sig["premium"]:
                    print(f"[strategy2] ⭐ {sig['base']} over per-sweep alert cap "
                          f"({cap}) — digest only")
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
    if OHLCV_CACHE_ON:
        print(f"[strategy2] candle cache: {hits} reused / {total - hits} fetched")
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
    # 📱 LINE 開機通知 + webhook 自動註冊(quick-tunnel 網址每次重啟都會換,
    # 由程式打 LINE API 重新指向,不必手動改 console)。
    try:
        import line_push
        if line_push.enabled():
            # sync_webhook() ALWAYS runs — it re-points LINE at the current
            # quick-tunnel URL, which changes on every restart. Only the
            # 已啟動 notice is optional (LINE_LIFECYCLE_NOTICE, default off).
            line_push.sync_webhook()
            line_push.send_lifecycle(line_push.START_MSG)
    except Exception as exc:  # noqa: BLE001 — LINE must never block startup
        print(f"[strategy2] line start notice failed: {exc}")
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

    # 🤖 Telegram command bot — /winrate /positions /signals /alerts /report,
    # answered from a daemon thread (long-polls getUpdates; read-only).
    try:
        tg_commands.start()
    except Exception as exc:  # noqa: BLE001 — the command bot is optional
        print(f"[strategy2] tg command bot failed to start: {exc}")

    # 💥 Liquidation collector — Binance+Bybit+OKX WebSockets feeding the
    # BTC/ETH cascade alerts (this process keeps its own rolling buffer).
    try:
        liq_alerts.start()
    except Exception as exc:  # noqa: BLE001 — the collector is optional
        print(f"[strategy2] liq collector failed to start: {exc}")

    _guard()                                   # protect immediately on startup
    while True:
        start = time.time()
        _guard()                               # …and at the top of every sweep
        try:
            recent = scan_once(client, recent, last_alert, pending)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            print(f"[strategy2] sweep error: {exc}")
        # 🌍 Event Radar — big-event news / macro-calendar / BTC-ETH shock
        # alerts into the group's Events topic, once per sweep.
        try:
            event_radar.tick(client)
        except Exception as exc:  # noqa: BLE001 — radar must never kill the loop
            print(f"[strategy2] event radar error: {exc}")
        # 📱 LINE webhook keep-alive — re-registers the endpoint if the
        # cloudflare quick-tunnel URL rotated mid-run (no-op otherwise) —
        # plus a daily push-quota check (200/mo runs out silently).
        try:
            import line_push
            line_push.sync_webhook()
            line_push.quota_tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] line webhook sync error: {exc}")
        # 🔗 Site link — when the tunnel URL rotates (reboot), push the fresh
        # dashboard link to the Telegram group + LINE group automatically.
        try:
            import site_link
            site_link.tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] site link error: {exc}")
        # 💻 Tech digest — 6-hourly tech/AI headlines into the Tech topic
        # (self-paced: tick() is a no-op until the cadence is due).
        try:
            tech_news.tick()
        except Exception as exc:  # noqa: BLE001 — digest must never kill the loop
            print(f"[strategy2] tech news error: {exc}")
        # 📈 Daily report — one account+market summary per local day into the
        # Report topic (self-paced: no-op until DAILY_REPORT_HOUR has passed).
        try:
            daily_report.tick()
        except Exception as exc:  # noqa: BLE001 — report must never kill the loop
            print(f"[strategy2] daily report error: {exc}")
        # ☀️ Public morning brief — market-only snapshot into the group's
        # Report topic (the private report above goes to the owner's DM).
        try:
            morning_brief.tick(client)
        except Exception as exc:  # noqa: BLE001 — brief must never kill the loop
            print(f"[strategy2] morning brief error: {exc}")
        # 🧵 Meta Threads — the same public snapshot as a daily post. Two API
        # calls split across two sweeps so Meta's ~30s container delay costs
        # this loop nothing. No-op unless THREADS_ENABLED and connected.
        try:
            import threads_post
            threads_post.tick(client)
        except Exception as exc:  # noqa: BLE001 — marketing never kills the loop
            print(f"[strategy2] threads error: {exc}")
        # 🔔 Price alerts — user-set levels from the dashboard, checked against
        # live tickers each sweep (one bulk fetch_tickers call).
        try:
            price_alerts.tick(client)
        except Exception as exc:  # noqa: BLE001 — alerts must never kill the loop
            print(f"[strategy2] price alerts error: {exc}")
        # 🇹🇼 台股 scan — TW50 pullback setups into the twstocks topic, once
        # per TWSE trading day after the 13:30 close (self-paced no-op else).
        try:
            tw_stocks.tick()
        except Exception as exc:  # noqa: BLE001 — scan must never kill the loop
            print(f"[strategy2] tw stocks error: {exc}")
        # 🇹🇼 台股盤中 — live session updates (opening bell, movers, TAIEX
        # shock, setup SL/TP touches); no-op outside 09:00–13:35 台北.
        try:
            tw_intraday.tick()
        except Exception as exc:  # noqa: BLE001 — intraday must never kill the loop
            print(f"[strategy2] tw intraday error: {exc}")
        # 🇺🇸 美股收盤 — last night's US session in Chinese at 08:00 台北, one
        # hour before the TWSE open (no-op every other sweep and at weekends).
        try:
            us_market.tick()
        except Exception as exc:  # noqa: BLE001 — digest must never kill the loop
            print(f"[strategy2] us market error: {exc}")
        # 🇺🇸 美股進場掃描 — oversold-in-uptrend setups on US100, 09:00 台北,
        # just after the close digest. Watch-only; measured edge +1.34%/trade
        # over a random-day baseline (see the module docstring).
        try:
            import us_stocks
            us_stocks.tick()
        except Exception as exc:  # noqa: BLE001 — a watch-only scan never kills the loop
            print(f"[strategy2] us stocks error: {exc}")
        # 📋 台股週結 — honest Sunday-morning TP/SL scorecard to LINE
        # (self-paced no-op except Sunday mornings).
        try:
            tw_stocks.scorecard_tick()
        except Exception as exc:  # noqa: BLE001 — scorecard must never kill the loop
            print(f"[strategy2] tw scorecard error: {exc}")
        # 💥 Liquidation cascades — BTC/ETH stop-run bursts into the liq topic
        # (thresholded + cooled down; a quiet market sends nothing).
        try:
            liq_alerts.tick(client)
        except Exception as exc:  # noqa: BLE001 — alerts must never kill the loop
            print(f"[strategy2] liq alerts error: {exc}")
        # 🐳 Whale tracker — curated Hyperliquid addresses open/close/flip into
        # the same liq topic (self-paced to WHALE_POLL_SEC).
        try:
            whale_tracker.tick(client)
        except Exception as exc:  # noqa: BLE001 — must never kill the loop
            print(f"[strategy2] whale tracker error: {exc}")
        # 🚨 Watchdog — bark on Telegram if a stack process died (S3 watches us).
        try:
            watchdog.tick("strategy2_scanner.py")
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] watchdog error: {exc}")
        # 📋 Signal outcomes — replay 48h-old signals against real candles;
        # Sunday scorecard closes the honesty loop.
        try:
            signal_outcomes.tick(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] outcomes error: {exc}")
        # 📐 ETH 14d momentum — paper forward-test of the honest backtest winner.
        try:
            eth_mom.tick(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] eth-mom error: {exc}")
        # 💾 Daily state backup + 🧹 scheduled message clean (both self-paced).
        try:
            backup_state.tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] backup error: {exc}")
        try:
            telegram_utils.auto_clean_tick()
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy2] auto-clean error: {exc}")
        # Flush a single grouped Telegram digest on a clean DIGEST_SEC cadence
        # (default every 30 min). Silent when nothing new fired in the window.
        if time.time() - last_digest >= DIGEST_SEC:
            if pending:
                outcome = _send_digest(pending)
                if outcome != "suppressed":       # suppression already logged
                    print(f"[strategy2] digest of {len(pending)} signal(s) "
                          f"{'sent' if outcome == 'sent' else 'FAILED'}")
                pending.clear()
            last_digest = time.time()
        elapsed = time.time() - start
        sleep_for = max(15, INTERVAL_SEC - elapsed)
        print(f"[strategy2] sweep done in {elapsed:.0f}s; sleeping {sleep_for:.0f}s")
        time.sleep(sleep_for)


if __name__ == "__main__":
    import signal as _signal

    def _term(signum, frame):        # ./run_all.sh stop pkills with SIGTERM —
        raise SystemExit(0)          # turn it into an exit that runs `finally`

    _signal.signal(_signal.SIGTERM, _term)
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        # 📱 LINE 關機通知 — covers stop, Ctrl+C and crashes alike.
        # Off by default (LINE_LIFECYCLE_NOTICE); every restart during testing
        # sent one of these, which is noise in 爸爸's group.
        try:
            import line_push
            if line_push.enabled():
                line_push.send_lifecycle(line_push.STOP_MSG)
        except Exception:  # noqa: BLE001 — dying anyway
            pass
