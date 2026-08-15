"""
Research: what actually raises the S2 signal win rate? (2026-07-16)

The Signals topic's win rate FELT low but was never measured (the outcome
tracker's 48h evaluation window never saw a signal because the signals file
purges at 24h). This script measures it, twice:

  baseline  — the REAL fired signals recovered from the daily state backups
              (backups/state-*.zip → strategy2_signals.json), evaluated
              against the real candles that followed. This is the actual
              track record of what the topic sent.
  replay    — an honest 60-day replay of strategy2_meter.compute_signal over
              the top-N liquid USDT perps (exact production code on trailing
              400-candle windows, closed candles only, 4h alert cooldown
              applied), with per-fire features (score, BTC-regime alignment,
              ADX, msb age, volume gate) so filter combinations can be
              compared on a real sample.

Outcomes use signal_outcomes.evaluate — the same pessimistic 48h replay the
weekly scorecard uses (SL checked before TP inside a candle).

Fidelity caveats (stated, not hidden): production fires mid-candle off the
forming bar (entry = live price at the edge), the replay fires at bar close
(entry = close). The population differs slightly at the margin; the LIFT of
a filter (with vs without) is what this measures.

Usage (from app/):
    python research_s2_winrate.py fetch     # download candles → research_cache/
    python research_s2_winrate.py replay    # replay + save fires
    python research_s2_winrate.py analyze   # gate table from saved fires
    python research_s2_winrate.py baseline  # evaluate the REAL fired signals

NEVER touches Telegram, the executor, or any live state. Read-only research.
"""
import glob
import json
import os
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from multiprocessing import Pool
from zoneinfo import ZoneInfo

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

CACHE_DIR = os.path.join(APP_DIR, "research_cache")
FIRES_FILE = os.path.join(CACHE_DIR, "replay_fires.json")
BASELINE_FILE = os.path.join(CACHE_DIR, "baseline_results.json")

DAYS = int(os.getenv("RS2_DAYS", "60"))
TOP_N = int(os.getenv("RS2_TOP_N", "60"))
# DERIVED, not a literal. This said 400 "candles the meter needs" and was wrong
# from 2026-08-11, when the outer tunnel moved to EMA676 and the meter began
# needing 688 — the same drift that silently disarmed the live scanner for
# three days. A replay window below the requirement makes compute_signal take
# its insufficient-history early return on every bar, so the harness reports
# ZERO fires and reads as "this filter finds nothing" rather than "this tool is
# broken". A research tool that fails that way is worse than no tool.
import strategy2_meter as _M
WARMUP = max(400, _M.SIGNAL_MIN_CANDLES + 20)
TF_SEC = 900                      # 15m
EVAL_BARS = 48 * 4                # 48h of 15m bars
COOLDOWN_SEC = 14400              # scanner's 4h per symbol+direction
TZ = ZoneInfo("Asia/Taipei")


# ── data fetching ────────────────────────────────────────────────────────────
def _client():
    from market_data import SafeBinanceClient
    return SafeBinanceClient(min_rest_interval=0.15, max_retries=4)


def _fetch_paginated(client, symbol, timeframe, since_ms, until_ms):
    """All candles in [since_ms, until_ms], oldest→newest, deduped."""
    out, cursor = [], since_ms
    while cursor < until_ms:
        rows = client.call("fetch_ohlcv", symbol, timeframe, cursor, 1500)
        if not rows:
            break
        rows = [r for r in rows if r[0] >= cursor]
        if not rows:
            break
        out.extend(rows)
        nxt = rows[-1][0] + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(rows) < 2:
            break
    seen, dedup = set(), []
    for r in out:
        if r[0] not in seen and r[0] <= until_ms:
            seen.add(r[0])
            dedup.append(r)
    return dedup


def cmd_fetch():
    import config
    from market_data import is_tradfi_market
    os.makedirs(CACHE_DIR, exist_ok=True)
    client = _client()
    markets = client.call("load_markets")
    tickers = client.call("fetch_tickers")
    syms = [s for s, m in markets.items()
            if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)
            and s.endswith(":USDT")
            and not (config.EXCLUDE_TRADFI_PERPS and is_tradfi_market(m))]
    syms.sort(key=lambda s: (tickers.get(s, {}) or {}).get("quoteVolume") or 0,
              reverse=True)
    syms = syms[:TOP_N]

    now_ms = int(time.time() // TF_SEC * TF_SEC) * 1000
    since_ms = now_ms - (DAYS * 96 + WARMUP) * TF_SEC * 1000
    meta = {"fetched_at": time.time(), "days": DAYS, "symbols": []}
    for i, sym in enumerate(syms):
        base = sym.split("/")[0]
        path = os.path.join(CACHE_DIR, f"{base}_15m.json")
        if os.path.exists(path):
            meta["symbols"].append({"symbol": sym, "base": base, "rank": i})
            continue
        try:
            rows = _fetch_paginated(client, sym, "15m", since_ms, now_ms)
        except Exception as exc:  # noqa: BLE001 — skip a bad symbol
            print(f"  skip {base}: {exc}")
            continue
        if len(rows) < WARMUP + EVAL_BARS + 100:
            print(f"  skip {base}: only {len(rows)} candles (too young)")
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        meta["symbols"].append({"symbol": sym, "base": base, "rank": i})
        print(f"[{i + 1}/{len(syms)}] {base}: {len(rows)} candles")

    btc = _fetch_paginated(client, "BTC/USDT:USDT", "1h",
                           since_ms - 100 * 3600 * 1000, now_ms)
    with open(os.path.join(CACHE_DIR, "BTC_1h.json"), "w", encoding="utf-8") as f:
        json.dump(btc, f)
    with open(os.path.join(CACHE_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    print(f"fetched {len(meta['symbols'])} symbols + BTC 1h ({len(btc)} bars)")


# ── BTC regime (mirrors bot.check_btc_regime: 1h EMA50, rising/falling) ─────
def btc_regime_series(btc_1h):
    """ts_ms → regime, computed on a trailing 60-bar window per closed bar
    (same window length production feeds check_btc_regime)."""
    import pandas as pd
    out = {}
    closes = [float(c[4]) for c in btc_1h]
    for i in range(55, len(btc_1h)):
        win = closes[max(0, i - 59):i + 1]
        ema = pd.Series(win).ewm(span=50, adjust=False).mean()
        price, ema_now, ema_prev = win[-1], ema.iloc[-1], ema.iloc[-6]
        if price > ema_now and ema_now > ema_prev:
            out[btc_1h[i][0]] = "bull"
        elif price < ema_now and ema_now < ema_prev:
            out[btc_1h[i][0]] = "bear"
        else:
            out[btc_1h[i][0]] = "neutral"
    return out


def regime_at(regimes: dict, ts_ms: int) -> str:
    """Regime of the last CLOSED 1h bar at time ts."""
    slot = (ts_ms // 3600000 - 1) * 3600000   # previous closed hour bar's open
    return regimes.get(slot, "neutral")


# ── replay worker ────────────────────────────────────────────────────────────
def replay_symbol(args):
    """Exact production signal replay for one symbol. Returns fires with
    features + outcomes."""
    base, rank, regimes = args
    import indicators
    import signal_outcomes
    import strategy2_live as S2L
    import strategy2_meter as S2

    path = os.path.join(CACHE_DIR, f"{base}_15m.json")
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)

    fires = []
    last_fire = {}
    n = len(rows)
    for i in range(WARMUP, n - 1):
        window = rows[i - WARMUP + 1:i + 1]
        try:
            res = S2.compute_signal(window)
        except Exception:  # noqa: BLE001 — bad data bar, skip
            continue
        direction = res.get("signal")
        if not direction:
            continue
        ts = rows[i][0] / 1000 + TF_SEC          # fire at bar close
        if ts - last_fire.get(direction, 0) < COOLDOWN_SEC:
            continue
        last_fire[direction] = ts
        price = float(rows[i][4])
        try:
            entry, sl, tp1, tp2 = S2L.trade_levels(
                price, direction == "long", S2L._atr(window))
        except Exception:  # noqa: BLE001
            continue
        if i + EVAL_BARS >= n:                   # 48h window would be censored
            continue
        sig = {"direction": direction, "ts": ts,
               "sl": sl, "tp1": tp1, "tp2": tp2}
        out = signal_outcomes.evaluate(sig, rows[i + 1:i + 1 + EVAL_BARS + 4])
        if out is None:
            continue
        adx = indicators.calculate_adx(window)
        vols = [float(c[5]) for c in window]
        vol_gate = (sum(vols[-21:-1]) / 20) > 0 and \
            vols[-1] >= 1.3 * (sum(vols[-21:-1]) / 20)
        regime = regime_at(regimes, rows[i][0])
        aligned = (direction == "long" and regime == "bull") or \
                  (direction == "short" and regime == "bear")
        against = (direction == "long" and regime == "bear") or \
                  (direction == "short" and regime == "bull")
        fires.append({
            "base": base, "rank": rank, "direction": direction,
            "score": res.get("score"), "ts": ts,
            "msb_age": res.get("msb_age"),
            "adx": round(adx, 1) if adx is not None else None,
            "vol_gate": bool(vol_gate),
            "btc_regime": regime, "aligned": aligned, "against": against,
            "hour": datetime.fromtimestamp(ts, TZ).hour,
            "outcome": out["outcome"], "hours": out["hours"],
        })
    return fires


def cmd_replay():
    with open(os.path.join(CACHE_DIR, "meta.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(CACHE_DIR, "BTC_1h.json"), "r", encoding="utf-8") as f:
        btc = json.load(f)
    regimes = btc_regime_series(btc)
    jobs = [(s["base"], s["rank"], regimes) for s in meta["symbols"]
            if os.path.exists(os.path.join(CACHE_DIR, f"{s['base']}_15m.json"))]
    t0 = time.time()
    all_fires = []
    with Pool(min(8, os.cpu_count() or 4)) as pool:
        for k, fires in enumerate(pool.imap_unordered(replay_symbol, jobs)):
            all_fires.extend(fires)
            print(f"[{k + 1}/{len(jobs)}] +{len(fires)} fires "
                  f"(total {len(all_fires)}, {time.time() - t0:.0f}s)")
    with open(FIRES_FILE, "w", encoding="utf-8") as f:
        json.dump(all_fires, f)
    print(f"replay done: {len(all_fires)} fires in {time.time() - t0:.0f}s")


# ── analysis ─────────────────────────────────────────────────────────────────
def _stats(fires):
    """Win rates + expectancy models for one bucket."""
    n = len(fires)
    if not n:
        return None
    c = defaultdict(int)
    for x in fires:
        c[x["outcome"]] += 1
    tp1_first = c["tp2"] + c["tp1"] + c["tp1→sl"]     # TP1 hit before SL
    resolved = n - c["none"]
    # managed: 50% off at TP1 + stop→BE, rest to TP2
    managed = (c["tp2"] * 1.5 + c["tp1→sl"] * 0.5 + c["tp1"] * 0.5
               + c["sl"] * -1.0) / n
    # full hold to TP2 (no partial)
    hold = (c["tp2"] * 2.0 + (c["sl"] + c["tp1→sl"]) * -1.0) / n
    return {
        "n": n,
        "wr_tp1": 100 * tp1_first / resolved if resolved else 0,
        "wr_tp2": 100 * c["tp2"] / resolved if resolved else 0,
        "sl_pct": 100 * (c["sl"] + c["tp1→sl"]) / resolved if resolved else 0,
        "none": c["none"],
        "exp_managed": managed, "exp_hold": hold,
    }


def _row(label, fires):
    s = _stats(fires)
    if not s:
        return f"{label:<42} —"
    return (f"{label:<42} n={s['n']:<5} TP1先到 {s['wr_tp1']:5.1f}%  "
            f"TP2 {s['wr_tp2']:5.1f}%  SL {s['sl_pct']:5.1f}%  "
            f"none {s['none']:<4} 期望值 managed {s['exp_managed']:+.3f}R "
            f"hold {s['exp_hold']:+.3f}R")


def _conviction(f):
    """Distance from neutral: long score 85 ⇒ 85; short score 15 ⇒ 85."""
    return f["score"] if f["direction"] == "long" else 100 - f["score"]


def cmd_analyze():
    with open(FIRES_FILE, "r", encoding="utf-8") as f:
        fires = json.load(f)
    print(f"\n══ S2 replay gate analysis — {DAYS}d, top {TOP_N} perps, "
          f"{len(fires)} fires ══\n")
    print(_row("ALL fires (today's digest bar)", fires))
    for d in ("long", "short"):
        print(_row(f"  direction={d}", [f for f in fires if f["direction"] == d]))
    print()
    for lo in (70, 75, 80, 85, 90):
        print(_row(f"conviction ≥{lo}", [f for f in fires if _conviction(f) >= lo]))
    print()
    print(_row("BTC-aligned", [f for f in fires if f["aligned"]]))
    print(_row("BTC-neutral", [f for f in fires if not f["aligned"] and not f["against"]]))
    print(_row("BTC-against", [f for f in fires if f["against"]]))
    print()
    for th in (20, 25, 30):
        print(_row(f"ADX ≥{th}", [f for f in fires if (f['adx'] or 0) >= th]))
        print(_row(f"ADX <{th}", [f for f in fires if (f['adx'] or 99) < th]))
    print()
    print(_row("volume ≥1.3×20bar", [f for f in fires if f["vol_gate"]]))
    print(_row("msb_age ≤ 12 bars", [f for f in fires if (f["msb_age"] or 99) <= 12]))
    print(_row("rank < 40 (most liquid)", [f for f in fires if f["rank"] < 40]))
    print()
    print("── combined gates ──")
    combos = [
        ("conv≥80 + aligned", lambda f: _conviction(f) >= 80 and f["aligned"]),
        ("conv≥85 + aligned", lambda f: _conviction(f) >= 85 and f["aligned"]),
        ("conv≥80 + not-against", lambda f: _conviction(f) >= 80 and not f["against"]),
        ("conv≥85 + not-against", lambda f: _conviction(f) >= 85 and not f["against"]),
        ("conv≥80 + aligned + ADX≥20",
         lambda f: _conviction(f) >= 80 and f["aligned"] and (f["adx"] or 0) >= 20),
        ("conv≥85 + aligned + ADX≥20",
         lambda f: _conviction(f) >= 85 and f["aligned"] and (f["adx"] or 0) >= 20),
        ("conv≥80 + aligned + ADX≥25",
         lambda f: _conviction(f) >= 80 and f["aligned"] and (f["adx"] or 0) >= 25),
        ("conv≥80 + aligned + vol",
         lambda f: _conviction(f) >= 80 and f["aligned"] and f["vol_gate"]),
        ("conv≥80 + aligned + ADX≥20 + vol",
         lambda f: _conviction(f) >= 80 and f["aligned"] and (f["adx"] or 0) >= 20
         and f["vol_gate"]),
    ]
    for label, fn in combos:
        print(_row(label, [f for f in fires if fn(f)]))
    print()


# ── baseline: the REAL fired signals from the state backups ─────────────────
def _real_signals():
    """Union of every strategy2_signals.json in backups + the live file,
    deduped by symbol+ts."""
    sigs = {}
    for z in sorted(glob.glob(os.path.join(APP_DIR, "backups", "state-*.zip"))):
        try:
            with zipfile.ZipFile(z) as zf:
                payload = json.loads(zf.read("strategy2_signals.json"))
        except Exception:  # noqa: BLE001
            continue
        for s in payload.get("signals") or []:
            if s.get("sl"):
                sigs[f"{s.get('symbol')}:{int(s.get('ts') or 0)}"] = s
    try:
        with open(os.path.join(APP_DIR, "strategy2_signals.json"), encoding="utf-8") as f:
            for s in (json.load(f).get("signals") or []):
                if s.get("sl"):
                    sigs[f"{s.get('symbol')}:{int(s.get('ts') or 0)}"] = s
    except Exception:  # noqa: BLE001
        pass
    return list(sigs.values())


def cmd_baseline():
    import signal_outcomes
    os.makedirs(CACHE_DIR, exist_ok=True)
    sigs = _real_signals()
    now = time.time()
    ready = [s for s in sigs if now - s["ts"] >= 48 * 3600]
    print(f"real signals recovered: {len(sigs)} (evaluable ≥48h old: {len(ready)})")
    client = _client()

    btc = _fetch_paginated(client, "BTC/USDT:USDT", "1h",
                           int((min(s['ts'] for s in ready) - 80 * 3600) * 1000),
                           int(now * 1000)) if ready else []
    regimes = btc_regime_series(btc)

    by_sym = defaultdict(list)
    for s in ready:
        by_sym[s["symbol"]].append(s)
    results = []
    for k, (sym, group) in enumerate(sorted(by_sym.items())):
        since = int((min(s["ts"] for s in group)) * 1000) - WARMUP * TF_SEC * 1000
        try:
            rows = _fetch_paginated(client, sym, "15m", since,
                                    int(now * 1000))
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {sym}: {exc}")
            continue
        for s in group:
            out = signal_outcomes.evaluate(s, rows)
            if out is None:
                continue
            regime = regime_at(regimes, int(s["ts"] * 1000))
            aligned = (s["direction"] == "long" and regime == "bull") or \
                      (s["direction"] == "short" and regime == "bear")
            against = (s["direction"] == "long" and regime == "bear") or \
                      (s["direction"] == "short" and regime == "bull")
            results.append({
                "base": s.get("base"), "direction": s["direction"],
                "score": s.get("score"), "ts": s["ts"], "rank": 0,
                "adx": None, "vol_gate": None, "msb_age": None,
                "btc_regime": regime, "aligned": aligned, "against": against,
                "hour": datetime.fromtimestamp(s["ts"], TZ).hour,
                "outcome": out["outcome"], "hours": out["hours"],
            })
        if (k + 1) % 20 == 0:
            print(f"  …{k + 1}/{len(by_sym)} symbols")
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f)
    print(f"\n══ REAL fired signals (from backups) — {len(results)} evaluated ══\n")
    print(_row("ALL real fires (what the topic sent)", results))
    for d in ("long", "short"):
        print(_row(f"  direction={d}", [f for f in results if f["direction"] == d]))
    for lo in (70, 80, 85):
        print(_row(f"conviction ≥{lo}", [f for f in results if _conviction(f) >= lo]))
    print(_row("BTC-aligned", [f for f in results if f["aligned"]]))
    print(_row("BTC-against", [f for f in results if f["against"]]))
    print(_row("conv≥80 + aligned",
               [f for f in results if _conviction(f) >= 80 and f["aligned"]]))


# ── geometry grid: the SL/TP structure is the win-rate lever ────────────────
# The filter analysis showed ~50% TP1-first no matter the gate — the plan
# geometry (stop at 1R, target at 1R/2R) caps the win rate structurally. This
# pass re-prices every replay fire under a grid of SL widths × TP distances,
# with taker fees both ways and HONEST timeout exits (mark-to-market at the
# window end, not "0"). Win rate AND expectancy per cell — pick with eyes open.
FEE_RT = 0.0011                    # Bybit taker 0.055% × 2 (in + out)
SL_MULTS = (1.0, 1.5, 2.0, 3.0)    # × ATR, capped at MAX_SL_PCT like production
TP_RS = (0.5, 0.75, 1.0, 1.5, 2.0)  # targets in R (R = actual stop distance)
# Micro-ATR symbols (USDC: 82 fires with a 0.002%-of-price stop!) make the
# fee term explode and poison every mean. A stop tighter than this % of
# price is a fee-burn, not a trade — excluded here AND now in production.
MIN_DIST_PCT = 0.003


def _geometry_eval(rows, i, is_long, sl_mult, tp_r, atr, window_bars):
    import config
    entry = float(rows[i][4])
    dist = min(atr * sl_mult, entry * config.MAX_SL_PCT) if atr else \
        entry * config.MAX_SL_PCT
    if dist <= entry * MIN_DIST_PCT:
        return None
    sl = entry - dist if is_long else entry + dist
    tp = entry + dist * tp_r if is_long else entry - dist * tp_r
    fee_r = FEE_RT * entry / dist
    end = min(i + 1 + window_bars, len(rows))
    for j in range(i + 1, end):
        h, l = float(rows[j][2]), float(rows[j][3])
        if (l <= sl if is_long else h >= sl):        # pessimistic: SL first
            return {"res": "sl", "r": -1.0 - fee_r, "r0": -1.0}
        if (h >= tp if is_long else l <= tp):
            return {"res": "tp", "r": tp_r - fee_r, "r0": tp_r}
    last = float(rows[end - 1][4])
    mark = (last - entry) / dist if is_long else (entry - last) / dist
    return {"res": "timeout", "r": mark - fee_r, "r0": mark}


def cmd_geometry():
    import strategy2_live as S2L
    with open(FIRES_FILE, "r", encoding="utf-8") as f:
        fires = json.load(f)
    candles, atrs = {}, {}
    for f_ in fires:
        base = f_["base"]
        if base not in candles:
            with open(os.path.join(CACHE_DIR, f"{base}_15m.json"), encoding="utf-8") as fh:
                rows = json.load(fh)
            candles[base] = (rows, {r[0]: k for k, r in enumerate(rows)})

    gates = [
        ("ALL fires", lambda f: True),
        ("aligned", lambda f: f["aligned"]),
        ("conv≥85 + aligned", lambda f: _conviction(f) >= 85 and f["aligned"]),
        ("conv≥80 + aligned + vol", lambda f: _conviction(f) >= 80
         and f["aligned"] and f["vol_gate"]),
    ]
    for wh in (24, 48):
        window_bars = wh * 4
        print(f"\n══ geometry grid · {wh}h window · fees {FEE_RT * 100:.2f}% rt ══")
        for gate_label, gate in gates:
            sub = [f_ for f_ in fires if gate(f_)]
            print(f"\n─ {gate_label} (n={len(sub)}) ─")
            print(f"{'SL×ATR':>7} {'TP(R)':>6} {'WR%':>6} {'SL%':>6} "
                  f"{'t/o%':>6} {'expR':>7} {'preFee':>7}")
            for sm in SL_MULTS:
                for tr in TP_RS:
                    res = []
                    for f_ in sub:
                        rows, idx = candles[f_["base"]]
                        i = idx.get(int((f_["ts"] - TF_SEC) * 1000))
                        if i is None or i + window_bars + 1 >= len(rows):
                            continue
                        key = (f_["base"], i)
                        if key not in atrs:
                            atrs[key] = S2L._atr(rows[i - WARMUP + 1:i + 1])
                        out = _geometry_eval(rows, i, f_["direction"] == "long",
                                             sm, tr, atrs[key], window_bars)
                        if out:
                            res.append(out)
                    if not res:
                        continue
                    n = len(res)
                    wr = 100 * sum(1 for x in res if x["res"] == "tp") / n
                    slp = 100 * sum(1 for x in res if x["res"] == "sl") / n
                    to = 100 * sum(1 for x in res if x["res"] == "timeout") / n
                    exp = sum(x["r"] for x in res) / n
                    exp0 = sum(x["r0"] for x in res) / n
                    print(f"{sm:>7} {tr:>6} {wr:>6.1f} {slp:>6.1f} "
                          f"{to:>6.1f} {exp:>+7.3f} {exp0:>+7.3f} (n={n})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    {"fetch": cmd_fetch, "replay": cmd_replay, "analyze": cmd_analyze,
     "baseline": cmd_baseline, "geometry": cmd_geometry}[cmd]()
