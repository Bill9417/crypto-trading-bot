"""
Strategy 3 — the Bybit auto-trader (config.STRATEGY3_SYMBOLS). Each symbol
trades its own timeframe, position size, emergency stop, engine AND candle
feed (config.strategy3_params). Signals from Binance charts by default
(per-symbol 'feed' can switch to Bybit) · orders on BYBIT.

TWO ENGINES share this scanner, its executor, guardian and Telegram plumbing:

  • "flagflip" (default) — the Vegas Flag Flip below, the Python twin of
    pine/TV_strategy_XAUT_30min.pine. XAUT (gold, 30m) runs this.
  • "occ" (config.STRATEGY3_OCC_SYMBOLS) — JustUncleL's "Open Close
    Cross": SMMA8 of the open vs close series on 90m buckets resampled from
    30m candles; close-MA crossing over the open-MA flips long, under flips
    short. Stop-and-reverse, one entry per cross; signals from
    strategy3_occ.py, the honest non-repainting port (the TV original
    repaints with default settings — its pine was deleted 2026-07-09; this
    engine stays for reference and no symbol routes to it now).

The flag-flip rules, exactly as on the chart:

  1. FLAG — the TV.pine confluence signal fires (MSB structure flip arms it;
     it fires on a CLOSED 15m bar when price is on the right side of EMA200,
     ADX confirms a trend, and the confidence score reaches the threshold).
  2. VEGAS — the Vegas line (SMA5 of EMA200, the green/red line in the tunnel)
     must MATCH the flag: green → long, red → short. If it disagrees at flag
     time the entry WAITS until Vegas turns (unless a newer opposite flag
     replaces the direction first).

  The position is held until the OPPOSITE flag appears → close immediately,
  then enter the new direction once Vegas agrees. There is no take-profit; a
  wide EMERGENCY stop (config.STRATEGY3_EMERGENCY_SL_PCT) rests on Bybit
  purely as crash protection.

  V2 ANTI-CHOP (config.STRATEGY3_BE_SYMBOLS — HYPE by default; its pine test
  script was retired 2026-07-09): once the position is BE_TRIGGER_PCT in profit, the
  stop jumps to entry ± BE_OFFSET_PCT (≈ fees), so sideways chop that pokes
  into profit and reverses scratches at ~0 instead of −1.5%. Winners are NOT
  capped — the exit is still the opposite flag. SOL/XAUT keep the plain rules.

Split-exchange design (user's request): candles are fetched from BINANCE by
default (identical to the TradingView chart driving the strategy), execution
happens on BYBIT via strategy3_exec. Per-symbol override: config
strategy3_params 'feed' can point a symbol's SIGNAL candles at Bybit instead
(STRATEGY3_XAUT_FEED=bybit) — XAUT is thin enough that the two venues print
different candles, and 2026-07-10 the same 30m bar read ADX 19.7 on Binance
(gate blocked) vs 22.5 on Bybit (short fired). Chart the feed you trade.
MANUAL intervention is expected and respected —
close a position by hand on Bybit and the scanner stands down for that symbol
until the NEXT flag; a position it did not open is never touched.

By DEFAULT alert-only. Arming real orders needs ALL of: STRATEGY3_LIVE=true,
BYBIT_API_KEY/BYBIT_API_SECRET in app/.env, and LIVE_TRADING=true (the master
gate — while false, orders are logged as dry-runs).

Flag-flip signals come from strategy3_signal.py — an EXACT bar-by-bar port of
pine/TV_strategy_XAUT_30min.pine (zigzag MSB arm, LuxAlgo internal bias, TV
score weights, per-bar ADX gate, arm-and-fire flags with alternation), so the
TradingView backtest and this bot fire the same flags on the same candles. The
only deliberate divergence, in both: the trendline factor (weight 5) is neutral.

State (last signal per symbol, consumed marker, our open direction) persists in
strategy3_state.json so a restart never re-enters or loses track of a flip.
"""
import json
import os
import time

import config
import strategy3_exec as X
import strategy3_occ as OCC
import strategy3_signal as SIG
import telegram_utils
from market_data import SafeBinanceClient, RateLimitCooldownError, timeframe_to_seconds

TIMEFRAME = config.STRATEGY3_TIMEFRAME                      # shared default; XAUT overrides to 30m
TF_SEC = timeframe_to_seconds(TIMEFRAME)
POLL_SEC = int(os.getenv("STRATEGY3_POLL_SEC", "45"))       # candle-close watcher
# Deep history on purpose: the arm/alternation state replays from the start of
# the series, so more bars ⇒ the last-bar flag matches TradingView better.
CANDLES = int(os.getenv("STRATEGY3_CANDLES", "1000"))

STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy3_state.json")


def symbols() -> list:
    return [f"{b}/{config.QUOTE_ASSET}:{config.QUOTE_ASSET}" for b in config.STRATEGY3_SYMBOLS]


# ── signal computation (pure, on CLOSED candles) ─────────────────────────────

def closed_candles(ohlcv, tf_sec=None, now=None) -> list:
    """Drop the still-forming candle so every read is bar-close confirmed,
    like the indicator's 'Confirm Signals on Bar Close'. tf_sec defaults to
    the shared TIMEFRAME's length; callers with a per-symbol override (XAUT's
    30m) must pass their own tf_sec."""
    if not ohlcv:
        return []
    tf_sec = tf_sec or TF_SEC
    now = now or time.time()
    last_open_ts = float(ohlcv[-1][0]) / 1000.0
    return ohlcv[:-1] if now < last_open_ts + tf_sec else ohlcv


def flag_on_last_bar(ohlcv) -> tuple:
    """(flag, snapshot) for the last CLOSED bar, straight from the exact
    TV_strategy_XAUT_30min.pine port — flag is 'long'/'short'/None."""
    snap = SIG.last_bar(ohlcv, config.STRATEGY3_SCORE_TH, config.STRATEGY3_ADX_TH)
    return snap["flag"], snap


def fetch_candles(client, sym: str, timeframe: str, feed: str) -> list:
    """Candles for the SIGNAL computation, from this symbol's configured feed
    (config.strategy3_params 'feed'). "binance" (default) uses the shared
    SafeBinanceClient; "bybit" reads the same market from the venue the orders
    actually fill on. Why it matters: XAUT is thin — 2026-07-10 the two venues
    printed the same 30m bar with ADX 19.7 (Binance, gate blocked) vs 22.5
    (Bybit, short fired). On thin symbols, chart the feed you trade."""
    if feed == "bybit":
        return X.client().fetch_ohlcv(sym, timeframe, None, CANDLES)
    return client.call("fetch_ohlcv", sym, timeframe, None, CANDLES)


# ── flip decision (pure — unit-tested) ───────────────────────────────────────

def decide(state: dict, flag, vegas: int, holding) -> tuple:
    """One symbol's flip decision for a closed bar.

    state   {'last_flag': 'long'/'short'/None, 'consumed': bool} — mutated
    flag    'long'/'short'/None — flag fired on THIS bar
    vegas   +1 green / -1 red / 0 flat
    holding 'long'/'short'/None — OUR open position direction

    Returns (close: bool, open_dir: 'long'/'short'/None). A new flag replaces
    the pending direction; an opposite flag closes at once; entry requires
    Vegas agreement and each flag opens at most one position (consumed)."""
    if flag and flag != state.get("last_flag"):
        state["last_flag"] = flag
        state["consumed"] = False

    want = state.get("last_flag")
    close = bool(holding and want and holding != want)
    effective = None if holding and not close else holding

    open_dir = None
    if want and not state.get("consumed") and effective != want:
        if (want == "long" and vegas > 0) or (want == "short" and vegas < 0):
            open_dir = want
    return close, open_dir


# ── execution (Bybit via strategy3_exec) ─────────────────────────────────────

def _tg(msg: str) -> None:
    """S3 is the live engine — its flag/trade/error alerts always punch through
    quiet mode (force=True), like the naked-position safety alarm."""
    try:
        telegram_utils.send_message(msg, force=True)
    except Exception as exc:  # noqa: BLE001 — alerts must never kill the loop
        print(f"[strategy3] telegram failed: {exc}")


def _mirror(action: str, *args) -> None:
    """Fan a live S3 action out to copy-trading followers, FULLY fail-soft —
    the master's own trade has already happened, so nothing here (a bad key, a
    down exchange, an import error) may ever raise into the S3 loop."""
    try:
        import copy_engine
        getattr(copy_engine, action)(*args)
    except Exception as exc:  # noqa: BLE001 — followers must never affect the master
        print(f"[strategy3] copy-mirror {action} failed: {exc}")


# Errors that deserve a retry on the next closed candle instead of burning the
# flag: network blips, exchange hiccups and rate limits. Anything else (below
# min size, insufficient balance, bad params) is treated as final for the flag.
_TRANSIENT_ERR = ("timeout", "timed out", "connection", "network", "temporar",
                  "unavailable", "busy", "502", "503", "504", "10006",
                  "rate limit", "too many")


def _is_transient(err) -> bool:
    e = str(err or "").lower()
    return any(t in e for t in _TRANSIENT_ERR)


def live_blocked() -> str:
    """'' when execution may proceed (incl. dry-run), else why it is alert-only."""
    if not config.STRATEGY3_LIVE:
        return "STRATEGY3_LIVE=false (alert-only)"
    return ""


def open_flip(symbol: str, direction: str, price: float, score, margin: float,
              leverage: int, sl_pct: float = None, why: str = None) -> str:
    """Enter a flip position on Bybit: market + emergency SL, no TP (the exit
    is the opposite signal). margin/leverage/sl_pct come from
    config.strategy3_params for this symbol — sizes, stops and even the engine
    differ per symbol. `why` is the engine-specific Telegram explanation (the
    flag-flip default mentions score/Vegas, the OCC engine passes its own).
    Returns an outcome for the signal bookkeeping:
      'opened' — in a position now (real fill or dry-run)
      'retry'  — transient failure; try again on the next closed candle
      'skip'   — final for this signal (alert-only mode, manual position, sizing)"""
    blocked = live_blocked()
    if blocked:
        print(f"[strategy3] would OPEN {direction.upper()} {symbol} — {blocked}")
        return "skip"
    # 🛑 Daily circuit breaker — refuses NEW entries after a bad day (real
    # Bybit closed-P&L, 24h window). Exits are never blocked. /resume clears.
    try:
        import strategy3_risk
        halt = strategy3_risk.entry_blocked()
    except Exception as exc:  # noqa: BLE001 — the breaker itself fails open
        print(f"[strategy3] risk gate error ({exc}) — allowing entry")
        halt = ""
    if halt:
        print(f"[strategy3] entry blocked by circuit breaker: {halt}")
        return "skip"
    if X.is_live():
        try:
            if X.get_position(symbol):
                print(f"[strategy3] skip {symbol}: a Bybit position already exists "
                      f"(manual?) — not touching it")
                _tg(f"⚠️ S3 skipped {direction.upper()} {symbol.split('/')[0]} — a position "
                    f"already exists on Bybit (manual?). Close it or let me manage it.")
                return "skip"
        except Exception as exc:  # noqa: BLE001 — fail closed on an unreadable account
            print(f"[strategy3] cannot read Bybit positions ({exc}) — will retry")
            return "retry"

    is_long = direction == "long"
    slp = sl_pct if sl_pct is not None else config.STRATEGY3_EMERGENCY_SL_PCT
    sl = price * (1 - slp) if is_long else price * (1 + slp)

    res = X.open_flip(symbol, direction, price, sl, margin, leverage)
    if not res.get("ok"):
        err = res.get("error")
        print(f"[strategy3] order error {symbol}: {err}")
        if _is_transient(err):
            return "retry"
        _tg(f"⚠️ S3 order error {symbol.split('/')[0]} {direction.upper()}: {err}")
        return "skip"
    tag = "DRY-RUN " if res.get("dry") else ""
    print(f"[strategy3] {tag}OPENED {direction.upper()} {symbol} @ {price:.6g} "
          f"(score {score}, emergency SL {sl:.6g}, qty {res.get('qty')})")
    why = why or f"score {score}/100 · Vegas agrees · exit = opposite flag"
    _tg(f"🔀 S3 {tag}FLIP · {direction.upper()} {symbol.split('/')[0]} @ {price:.6g} (Bybit)\n"
        f"{why} (emergency SL {slp:.0%})")
    if res.get("leverage_warning"):
        _tg(f"⚠️ S3 · {symbol.split('/')[0]} leverage may not be {leverage}x "
            f"— Bybit said: {res['leverage_warning']}\nSame {price * res.get('qty', 0):.0f} USDT "
            f"notional, but MORE margin may be locked than expected — check free balance "
            f"before the next flip.")
    if not res.get("dry"):                       # only a REAL fill mirrors to followers
        _mirror("mirror_open", symbol, direction, price, sl, leverage)
    return "opened"


def close_flip(symbol: str, why: str) -> bool:
    blocked = live_blocked()
    if blocked:
        print(f"[strategy3] would CLOSE {symbol} ({why}) — {blocked}")
        return False
    res = X.close_flip(symbol)
    if not res.get("ok"):
        print(f"[strategy3] close error {symbol}: {res.get('error')}")
        _tg(f"⚠️ S3 close FAILED {symbol.split('/')[0]}: {res.get('error')} — check Bybit!")
        return False
    tag = "DRY-RUN " if res.get("dry") else ""
    print(f"[strategy3] {tag}CLOSED {symbol} — {why}")
    _tg(f"🔀 S3 {tag}EXIT · {symbol.split('/')[0]} (Bybit) — {why}")
    if not res.get("dry"):                       # mirror the exit to followers
        _mirror("mirror_close", symbol, why)
    return True


# ── state persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    payload = {**state, "updated_at": time.time(),
               "live": bool(config.STRATEGY3_LIVE and X.is_live())}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ── main loop ────────────────────────────────────────────────────────────────

# Guardian-alert cooldown: a naked position gets re-tried and re-alerted every
# poll (~45s) until fixed, which would spam Telegram during a real outage.
# This is the single most dangerous failure mode in the system (unbounded
# downside on a 50x position with no floor), so it still alerts periodically
# rather than going silent after the first message.
_GUARDIAN_ALERT_COOLDOWN_SEC = 600
_last_guardian_alert: dict = {}


def _guardian_alert(base: str, detail: str) -> None:
    now = time.time()
    if now - _last_guardian_alert.get(base, 0) >= _GUARDIAN_ALERT_COOLDOWN_SEC:
        _last_guardian_alert[base] = now
        leverage = config.strategy3_params(base)["leverage"]
        _tg(f"🚨 S3 GUARDIAN FAILED · {base} has NO stop-loss on Bybit and the "
            f"auto-repair failed: {detail}\nThis position is UNPROTECTED at "
            f"{leverage}x — check Bybit now and set a stop by hand if this repeats.")


def breakeven_level(base: str, pos: dict):
    """Where the stop should sit for `pos` once break-even is armed, or None
    when this symbol doesn't use the V2 break-even rule (config)."""
    if base.upper() not in config.STRATEGY3_BE_SYMBOLS or not pos.get("entry"):
        return None
    off = config.STRATEGY3_BE_OFFSET_PCT
    return pos["entry"] * (1 + off) if pos["side"] == "long" else pos["entry"] * (1 - off)


def manage_breakeven(sym: str, st: dict, pos: dict) -> None:
    """V2 anti-chop (HYPE by default): once the position is BE_TRIGGER_PCT in
    profit (mark vs entry), jump the resting Bybit stop from the wide emergency
    level to entry ± BE_OFFSET_PCT (≈ fees). Fires ONCE per position
    (st['be_armed']); a failed move retries on the next ~45s poll. Chop that
    pokes into profit and reverses now scratches at ~0 instead of −1.5%;
    winners still run to the opposite flag."""
    base = sym.split("/")[0]
    be_lvl = breakeven_level(base, pos)
    if be_lvl is None or st.get("be_armed"):
        return
    entry, mark = pos.get("entry"), pos.get("mark")
    if not (entry and mark):
        return
    trig = config.STRATEGY3_BE_TRIGGER_PCT
    in_profit = mark >= entry * (1 + trig) if pos["side"] == "long" else mark <= entry * (1 - trig)
    if not in_profit:
        return
    try:
        X.set_stop(sym, be_lvl)
    except Exception as exc:  # noqa: BLE001 — retry next poll, stop stays at emergency level
        print(f"[strategy3] break-even move failed {sym}: {exc}")
        return
    st["be_armed"] = True
    _mirror("mirror_set_stop", sym, be_lvl)      # move followers' stops too
    print(f"[strategy3] {base}: break-even armed — stop moved to {be_lvl:.6g} "
          f"(entry {entry:.6g}, mark {mark:.6g})")
    _tg(f"🛡️ S3 · {base} is +{trig:.2%} — stop moved to break-even "
        f"({be_lvl:.6g}). Worst case is now ~0 instead of "
        f"−{config.STRATEGY3_EMERGENCY_SL_PCT:.1%}.")


def reconcile_position(sym: str, st: dict) -> None:
    """EVERY poll (not just on candle closes): notice an emergency-SL hit or a
    MANUAL close within ~45s and stand down until the next flag, keep the
    emergency stop armed on a position we hold, and run the break-even jump
    for the symbols that use it. No-op in dry-run.

    A failure to (re-)arm the stop is the single most dangerous failure mode
    here — a naked position at 50x has no floor — so it gets a LOUD, repeating
    Telegram alert, not just a log line nobody is watching in real time."""
    if not (st.get("pos_dir") and X.is_live()):
        return
    base = sym.split("/")[0]
    try:
        pos = X.get_position(sym)
    except Exception as exc:  # noqa: BLE001 — keep last known state on API blips
        print(f"[strategy3] reconcile error {sym}: {exc}")
        return

    if not pos:
        print(f"[strategy3] {base}: position gone on Bybit (SL or manual "
              f"close) — standing down until the next flag")
        _tg(f"ℹ️ S3 · {base} position closed on Bybit (stop or manual) — "
            f"waiting for the next flag")
        st["pos_dir"] = None
        st["consumed"] = True
        st["be_armed"] = False
        return

    manage_breakeven(sym, st, pos)

    if pos.get("sl"):
        return                                          # protected — nothing to do

    # A stop still missing next cycle (~45s later) re-enters this same path and
    # retries — no separate immediate re-check here, which would risk a false
    # alarm from Bybit's own propagation delay right after a successful set.
    # If break-even already armed, re-arm at the BE level; otherwise at THIS
    # symbol's own emergency distance (the OCC pair runs a wider stop at lower
    # leverage than the flag-flip symbols — the global default would be wrong).
    if st.get("be_armed"):
        sl_price = breakeven_level(base, pos)
    else:
        slp = config.strategy3_params(base)["sl_pct"]
        sl_price = (pos["entry"] * (1 - slp) if pos["side"] == "long"
                    else pos["entry"] * (1 + slp))
    try:
        X.ensure_stop(sym, sl_price=sl_price, pos=pos)
        print(f"[strategy3] guardian armed the stop on {sym}")
    except Exception as exc:  # noqa: BLE001
        print(f"[strategy3] GUARDIAN FAILED to arm stop on {sym}: {exc}")
        _guardian_alert(base, str(exc)[:200])


def ensure_engine(st: dict, engine: str) -> None:
    """Stamp the symbol's engine into its state; on a REAL engine change (e.g.
    SOL: flag-flip → OCC) wipe the old signal bookkeeping so a stale flag from
    the previous engine can never trigger an entry under the new rules. The
    open-position marker survives — an engine change must not orphan a
    position the bot is holding. Symbols with no engine stamp yet (state
    written before engines existed) are stamped without a wipe unless they are
    switching onto the OCC engine, whose fields mean different things."""
    prev = st.get("engine")
    if prev == engine:
        return
    if prev is not None or engine == "occ":
        st["last_flag"] = None
        st["consumed"] = False
        st["last_candle"] = 0
        st["last_bucket"] = 0
        st["open_attempts"] = 0
    st["engine"] = engine


def step(client, state: dict) -> None:
    """One pass over the symbols; trades only on a NEW closed candle FOR THAT
    SYMBOL'S OWN TIMEFRAME (config.strategy3_params — XAUT is 30m, the rest are
    15m by default; the OCC engine additionally waits for a NEW closed 90m
    bucket), but reconciles positions on every pass regardless. State is saved
    after each symbol so a mid-pass abort can never lose an opened position."""
    for sym in symbols():
        base = sym.split("/")[0]
        params = config.strategy3_params(base)
        tf_sec = timeframe_to_seconds(params["timeframe"])
        st = state.setdefault(sym, {"last_flag": None, "consumed": False,
                                    "pos_dir": None, "last_candle": 0,
                                    "be_armed": False})
        ensure_engine(st, params.get("engine", "flagflip"))
        reconcile_position(sym, st)

        feed = params.get("feed", "binance")
        try:
            raw = fetch_candles(client, sym, params["timeframe"], feed)
        except RateLimitCooldownError as exc:
            print(f"[strategy3] cooldown: {exc}; pausing 30s")
            save_state(state)
            time.sleep(30)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy3] {feed} fetch error {sym}: {exc}")
            continue

        ohlcv = closed_candles(raw, tf_sec)
        if not ohlcv:
            continue
        last_ts = float(ohlcv[-1][0])
        if last_ts <= st.get("last_candle", 0):
            continue                                    # no new closed bar yet
        st["last_candle"] = last_ts

        if params.get("engine") == "occ":
            # ── OCC engine: SMMA8 open/close cross on resampled 90m buckets ──
            try:
                snap = OCC.snapshot(ohlcv, tf_sec, params["res_mult"], params["ma_len"])
            except Exception as exc:  # noqa: BLE001
                print(f"[strategy3] OCC compute error {sym}: {exc}")
                continue
            if snap["insufficient"]:
                continue
            if snap["bucket_ts"] <= st.get("last_bucket", 0):
                continue                                # no new closed bucket yet
            st["last_bucket"] = snap["bucket_ts"]
            sig = snap["signal"]
            alt_min = int(tf_sec * params["res_mult"] // 60)
            print(f"[strategy3] {base} {alt_min}m OCC close · trend {snap['trend']} · "
                  f"cross {sig or '—'} · pos {st.get('pos_dir') or 'flat'}"
                  + (f" · feed {feed}" if feed != "binance" else ""))
            # flag-flip display fields don't exist on this engine — clear them
            # so the web pages describe the OCC state instead of a stale score
            st["last_score"] = None
            st["last_vegas"] = None
            st["last_msb"] = None
            st["last_trend"] = snap["trend"]
            st["last_price"] = snap["price"]
            st["last_seen"] = time.time()

            if sig and sig != st.get("last_flag"):
                st["open_attempts"] = 0                 # fresh cross → fresh retries
                _tg(f"🚩 S3 CROSS · {sig.upper()} {base} ({alt_min}m open/close cross, "
                    f"{feed.capitalize()} chart)\nstop-and-reverse — flipping the position now")
            # the cross IS the whole signal — no Vegas gate on this engine, so
            # feed decide() an always-agreeing value for the pending direction
            # to reuse the flip/consumed/manual-close state machine unchanged
            want = sig or st.get("last_flag")
            gate = 1 if want == "long" else -1 if want == "short" else 0
            close, open_dir = decide(st, sig, gate, st.get("pos_dir"))
            why = (f"{params['ma_len']}-SMMA close crossed "
                   f"{'over' if open_dir == 'long' else 'under'} open on {alt_min}m · "
                   f"exit = opposite cross")
            score = None
        else:
            # ── flag-flip engine (the original Vegas Flag Flip rules) ──
            try:
                flag, snap = flag_on_last_bar(ohlcv)
            except Exception as exc:  # noqa: BLE001
                print(f"[strategy3] compute error {sym}: {exc}")
                continue
            if snap["insufficient"]:
                continue

            vegas_txt = "green" if snap["vegas"] > 0 else "red" if snap["vegas"] < 0 else "flat"
            # heartbeat — one line per closed candle so the log (and /health) show life
            print(f"[strategy3] {base} {params['timeframe']} close · score {snap['score']:.1f} · "
                  f"vegas {vegas_txt} · msb {snap['msb']} · flag {flag or '—'} · "
                  f"pos {st.get('pos_dir') or 'flat'}"
                  + (f" · feed {feed}" if feed != "binance" else ""))
            # Persisted so the /bybit web page can show "why" without recomputing
            # the signal itself — one line per closed candle, not per poll.
            st["last_score"] = snap["score"]
            st["last_vegas"] = snap["vegas"]
            st["last_msb"] = snap["msb"]
            st["last_price"] = snap["price"]
            st["last_seen"] = time.time()

            if flag and flag != st.get("last_flag"):
                st["open_attempts"] = 0                 # fresh flag → fresh retries
                _tg(f"🚩 S3 FLAG · {flag.upper()} {base} ({params['timeframe']}, "
                    f"{feed.capitalize()} chart)\n"
                    f"score {snap['score']:.0f}/100 · Vegas "
                    f"{'agrees → entering' if (snap['vegas'] > 0) == (flag == 'long') and snap['vegas'] != 0 else 'disagrees → waiting'}")

            close, open_dir = decide(st, flag, snap["vegas"], st.get("pos_dir"))
            why = None                                  # open_flip's score/Vegas default
            score = snap["score"]

        if close:
            what = "cross" if params.get("engine") == "occ" else "flag"
            if close_flip(sym, f"opposite {what} ({st['last_flag']})"):
                st["pos_dir"] = None
        if open_dir and not st.get("pos_dir"):
            outcome = open_flip(sym, open_dir, snap["price"], score,
                                 params["margin"], params["leverage"],
                                 sl_pct=params["sl_pct"], why=why)
            if outcome == "opened":
                st["pos_dir"] = open_dir
                st["consumed"] = True
                st["be_armed"] = False              # fresh position → fresh break-even
            elif outcome == "retry":
                st["open_attempts"] = st.get("open_attempts", 0) + 1
                if st["open_attempts"] >= 3:            # give up after 3 candles
                    st["consumed"] = True
                    _tg(f"⚠️ S3 gave up opening {open_dir.upper()} {base} after "
                        f"{st['open_attempts']} attempts — waiting for the next flag")
                # else: flag stays live → retried on the next closed candle
            else:                                       # 'skip' — final for this flag
                st["consumed"] = True

        save_state(state)                               # persist after each symbol

    save_state(state)


def mode_string() -> str:
    """Short phrase for whether orders actually reach Bybit right now — shared
    by the startup banner and the /bybit web page so they can never drift."""
    blocked = live_blocked()
    if blocked:
        return f"ALERT-ONLY ({blocked})"
    if not X.keys_present():
        return "ARMED but no BYBIT_API_KEY/BYBIT_API_SECRET in app/.env → dry-run"
    if not config.LIVE_TRADING:
        return "ARMED but LIVE_TRADING=false → dry-run"
    return "LIVE on BYBIT"


def _symbol_breakdown() -> str:
    """'XAUT 30m 3000 USDT×50x · ETH 30m 500 USDT×10x OCC · ...' — one clause
    per symbol, since timeframe/size/engine/rules can differ (XAUT's flag-flip,
    the OCC pair's open/close cross, HYPE's break-even)."""
    parts = []
    for base in config.STRATEGY3_SYMBOLS:
        p = config.strategy3_params(base)
        be = "+BE" if base.upper() in config.STRATEGY3_BE_SYMBOLS else ""
        occ = " OCC" if p.get("engine") == "occ" else ""
        feed = f" [{p['feed']} feed]" if p.get("feed", "binance") != "binance" else ""
        parts.append(f"{base} {p['timeframe']} {p['margin'] * p['leverage']:.0f} "
                     f"USDT×{p['leverage']}x{be}{occ} SL {p['sl_pct']:.1%}{feed}")
    return " · ".join(parts)


def status_line() -> str:
    return (f"Execution: {mode_string()} | {_symbol_breakdown()} | "
            f"flag-flip: score ≥{config.STRATEGY3_SCORE_TH} · ADX ≥{config.STRATEGY3_ADX_TH} | "
            f"OCC: SMMA{config.STRATEGY3_OCC_MA_LEN} ×{config.STRATEGY3_OCC_RES_MULT} res cross")


def main() -> None:
    mode = mode_string()
    print(f"[strategy3] starting — {_symbol_breakdown()} "
          f"(per-symbol chart feed → Bybit orders) · flag-flip score ≥{config.STRATEGY3_SCORE_TH} "
          f"· ADX ≥{config.STRATEGY3_ADX_TH} · mode: {mode}")
    client = SafeBinanceClient(
        min_rest_interval=float(os.getenv("STRATEGY3_REST_INTERVAL", "0.35")),
        max_retries=3,
    )
    state = load_state()
    state.pop("updated_at", None)
    state.pop("live", None)

    while True:
        start = time.time()
        try:
            step(client, state)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            print(f"[strategy3] step error: {exc}")
        # 🚨 Watchdog — S3 watches the rest of the stack (S2 watches us back).
        try:
            import watchdog
            watchdog.tick("strategy3_scanner.py")
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy3] watchdog error: {exc}")
        time.sleep(max(10, POLL_SEC - (time.time() - start)))


if __name__ == "__main__":
    main()
