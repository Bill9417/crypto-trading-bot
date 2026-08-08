"""Strategy 3 (Vegas Flag Flip) — decision-logic tests.

decide() is the pure heart of the flip engine: flag sets the direction, Vegas
gates the entry, opposite flag closes, one entry per flag. These tests pin the
exact behaviour discussed with the operator (2026-07-04)."""
import time

import strategy3_scanner as S3


def fresh():
    return {"last_flag": None, "consumed": False}


def test_flag_plus_vegas_opens():
    st = fresh()
    close, open_dir = S3.decide(st, "long", +1, None)
    assert (close, open_dir) == (False, "long")


def test_flag_without_vegas_waits_then_enters():
    st = fresh()
    close, open_dir = S3.decide(st, "long", -1, None)   # vegas red → wait
    assert (close, open_dir) == (False, None)
    # …later bar: no new flag, vegas turns green → enter now
    close, open_dir = S3.decide(st, None, +1, None)
    assert (close, open_dir) == (False, "long")


def test_opposite_flag_closes_and_flips_when_vegas_agrees():
    st = {"last_flag": "long", "consumed": True}
    close, open_dir = S3.decide(st, "short", -1, "long")
    assert close is True
    assert open_dir == "short"


def test_opposite_flag_closes_immediately_even_if_vegas_disagrees():
    st = {"last_flag": "long", "consumed": True}
    close, open_dir = S3.decide(st, "short", +1, "long")   # vegas still green
    assert close is True          # rule: opposite flag exits at once
    assert open_dir is None       # new entry waits for vegas
    # next bar vegas turns red → enter the short
    close, open_dir = S3.decide(st, None, -1, None)
    assert (close, open_dir) == (False, "short")


def test_one_entry_per_flag_no_reentry_after_stop():
    st = fresh()
    _, open_dir = S3.decide(st, "long", +1, None)
    assert open_dir == "long"
    st["consumed"] = True                       # scanner marks it on entry
    # position later disappears (emergency SL) → flat, same flag → NO re-entry
    close, open_dir = S3.decide(st, None, +1, None)
    assert (close, open_dir) == (False, None)
    # …but a fresh flag re-arms it
    close, open_dir = S3.decide(st, "long", +1, None)
    assert (close, open_dir) == (False, None)   # same-direction flag ≠ new flag
    close, open_dir = S3.decide(st, "short", -1, None)
    assert (close, open_dir) == (False, "short")


def test_same_direction_flag_while_holding_is_noop():
    st = {"last_flag": "long", "consumed": True}
    close, open_dir = S3.decide(st, "long", +1, "long")
    assert (close, open_dir) == (False, None)


def test_no_flag_no_action():
    st = fresh()
    assert S3.decide(st, None, +1, None) == (False, None)
    assert S3.decide(st, None, -1, None) == (False, None)


def test_closed_candles_drops_forming_bar():
    now = time.time()
    tf = 15 * 60
    # last candle opened 5 min ago → still forming → dropped
    ohlcv = [[(now - 3 * tf) * 1000, 1, 2, 0.5, 1.5, 10],
             [(now - 2 * tf) * 1000, 1.5, 2, 1, 1.8, 10],
             [(now - 300) * 1000, 1.8, 2, 1.5, 1.9, 10]]
    assert len(S3.closed_candles(ohlcv, tf, now)) == 2
    # last candle opened a full TF (+1s margin) ago → closed → kept
    ohlcv[-1][0] = (now - tf - 1) * 1000
    assert len(S3.closed_candles(ohlcv, tf, now)) == 3


def test_closed_candles_uses_the_tf_sec_passed_in_not_the_module_default():
    """Regression: XAUT's 30m bars must use ITS OWN tf_sec, not the shared
    15m default — otherwise a still-forming 30m XAUT candle (open <30 min ago
    but >15 min ago) would be wrongly treated as closed."""
    now = time.time()
    tf30 = 30 * 60
    ohlcv = [[(now - 2 * tf30) * 1000, 1, 2, 0.5, 1.5, 10],
             [(now - 20 * 60) * 1000, 1.5, 2, 1, 1.8, 10]]   # opened 20 min ago
    assert len(S3.closed_candles(ohlcv, tf30, now)) == 1     # still forming on a 30m tf
    assert len(S3.closed_candles(ohlcv, 15 * 60, now)) == 2  # would be "closed" on a 15m tf


# NOTE: these three tests must never assert a specific coin list — the user
# turns symbols on/off in .env (BTC was switched off 2026-07-05) and preflight
# runs against the real .env, so hardcoding "BTC" here aborted a live launch.
# Pin fixture values with monkeypatch to test the MECHANICS instead.

def test_symbols_from_config(monkeypatch):
    monkeypatch.setattr(S3.config, "STRATEGY3_SYMBOLS", ["BTC", "SOL", "HYPE", "XAUT"])
    assert S3.symbols() == ["BTC/USDT:USDT", "SOL/USDT:USDT",
                            "HYPE/USDT:USDT", "XAUT/USDT:USDT"]
    # and whatever is really configured right now must map to the same format
    monkeypatch.undo()
    syms = S3.symbols()
    assert syms and all(s.endswith("/USDT:USDT") for s in syms)
    assert [s.split("/")[0] for s in syms] == S3.config.STRATEGY3_SYMBOLS


def test_strategy3_params_per_symbol_override(monkeypatch):
    """Per-symbol overrides win per key; everything else falls back to the
    shared defaults — this is the config the whole per-symbol sizing/timeframe
    refactor depends on. Values are pinned here, NOT read from .env, so the
    user can freely enable/disable symbols without breaking preflight."""
    monkeypatch.setattr(S3.config, "STRATEGY3_TIMEFRAME", "15m")
    monkeypatch.setattr(S3.config, "STRATEGY3_MARGIN_USDT", 30.0)
    monkeypatch.setattr(S3.config, "STRATEGY3_LEVERAGE", 50)
    monkeypatch.setattr(S3.config, "STRATEGY3_EMERGENCY_SL_PCT", 0.015)
    monkeypatch.setattr(S3.config, "STRATEGY3_FEED", "binance")
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SYMBOLS", [])
    monkeypatch.setattr(S3.config, "STRATEGY3_OVERRIDES",
                        {"XAUT": {"timeframe": "30m", "margin": 60.0}})
    xaut = S3.config.strategy3_params("XAUT")
    sol = S3.config.strategy3_params("SOL")
    assert xaut == {"engine": "flagflip", "timeframe": "30m", "margin": 60.0,
                    "leverage": 50, "sl_pct": 0.015, "feed": "binance"}
    assert sol == {"engine": "flagflip", "timeframe": "15m", "margin": 30.0,
                   "leverage": 50, "sl_pct": 0.015, "feed": "binance"}
    # worst-case total margin lock for the full 4-coin list (a regression a
    # careless merge bug in strategy3_params could easily break silently)
    total = sum(S3.config.strategy3_params(b)["margin"]
                for b in ["BTC", "SOL", "HYPE", "XAUT"])
    assert total == 150.0


def test_strategy3_params_occ_engine(monkeypatch):
    """Symbols in STRATEGY3_OCC_SYMBOLS get the whole OCC block (engine,
    timeframe, sizing, their OWN wider stop) regardless of the flag-flip
    overrides; other symbols keep the flag-flip params untouched."""
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SYMBOLS", ["ETH", "SOL"])
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_TIMEFRAME", "30m")
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_RES_MULT", 3)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_MA_LEN", 8)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_MARGIN_USDT", 50.0)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_LEVERAGE", 10)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SL_PCT", 0.04)
    monkeypatch.setattr(S3.config, "STRATEGY3_EMERGENCY_SL_PCT", 0.015)
    monkeypatch.setattr(S3.config, "STRATEGY3_FEED", "binance")
    eth = S3.config.strategy3_params("ETH")
    assert eth == {"engine": "occ", "timeframe": "30m", "margin": 50.0,
                   "leverage": 10, "sl_pct": 0.04, "res_mult": 3, "ma_len": 8,
                   "feed": "binance"}
    assert eth["margin"] * eth["leverage"] == 500.0     # the user's order value
    assert S3.config.strategy3_params("XAUT")["engine"] == "flagflip"
    assert S3.config.strategy3_params("XAUT")["sl_pct"] == 0.015


def test_strategy3_params_feed_override(monkeypatch):
    """Per-symbol candle feed: XAUT reads Bybit candles (the venue the orders
    fill on — added 2026-07-10 after the ADX gate split across venues on a
    thin market); everyone else stays on the Binance default. An UNSET env
    override is stored as None and must still fall back to the shared feed."""
    monkeypatch.setattr(S3.config, "STRATEGY3_FEED", "binance")
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SYMBOLS", [])
    monkeypatch.setattr(S3.config, "STRATEGY3_OVERRIDES",
                        {"XAUT": {"timeframe": "30m", "feed": "bybit"},
                         "SOL": {"feed": None}})
    assert S3.config.strategy3_params("XAUT")["feed"] == "bybit"
    assert S3.config.strategy3_params("SOL")["feed"] == "binance"   # None → default
    assert S3.config.strategy3_params("HYPE")["feed"] == "binance"  # no override


def test_fetch_candles_routes_by_feed(monkeypatch):
    """'binance' goes through the shared SafeBinanceClient wrapper; 'bybit'
    hits strategy3_exec.client() (the execution venue) — and never vice versa."""
    calls = []

    class _Binance:
        def call(self, method, *a):
            calls.append(("binance", method, a))
            return [[1, 1, 1, 1, 1, 1]]

    class _Bybit:
        def fetch_ohlcv(self, *a):
            calls.append(("bybit", "fetch_ohlcv", a))
            return [[2, 2, 2, 2, 2, 2]]

    monkeypatch.setattr(S3.X, "client", lambda: _Bybit())
    assert S3.fetch_candles(_Binance(), "XAUT/USDT:USDT", "30m", "bybit")[0][0] == 2
    assert S3.fetch_candles(_Binance(), "SOL/USDT:USDT", "15m", "binance")[0][0] == 1
    assert [c[0] for c in calls] == ["bybit", "binance"]
    # both fetch the full replay depth (arm/alternation needs deep history)
    assert calls[0][2] == ("XAUT/USDT:USDT", "30m", None, S3.CANDLES)
    assert calls[1][2] == ("SOL/USDT:USDT", "15m", None, S3.CANDLES)


def test_status_line_and_banner_mention_every_symbol(monkeypatch):
    """Regression: the per-symbol breakdown used by both the startup banner
    and the /bybit web page must actually list every configured symbol with
    its own timeframe — a single shared TIMEFRAME string silently dropped
    XAUT's 30m override in an earlier draft of this refactor."""
    monkeypatch.setattr(S3.config, "STRATEGY3_SYMBOLS", ["BTC", "SOL", "HYPE", "XAUT"])
    monkeypatch.setattr(S3.config, "STRATEGY3_TIMEFRAME", "15m")
    monkeypatch.setattr(S3.config, "STRATEGY3_MARGIN_USDT", 30.0)
    monkeypatch.setattr(S3.config, "STRATEGY3_LEVERAGE", 50)
    monkeypatch.setattr(S3.config, "STRATEGY3_OVERRIDES",
                        {"XAUT": {"timeframe": "30m", "margin": 60.0}})
    line = S3.status_line()
    assert "BTC 15m 1500 USDT×50x" in line
    assert "XAUT 30m 3000 USDT×50x" in line
    # and with the real live config: every enabled symbol must appear
    monkeypatch.undo()
    line = S3.status_line()
    for base in S3.config.STRATEGY3_SYMBOLS:
        assert base in line


def _synth_ohlcv(n=900, seed=7):
    """Deterministic random-walk OHLCV, oldest→newest."""
    import numpy as np
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    spread = np.abs(rng.normal(0, 0.004, n)) * close
    high = close + spread
    low = close - spread
    vol = rng.uniform(50, 500, n)
    return [[i * 900_000, float(close[i]), float(high[i]), float(low[i]),
             float(close[i]), float(vol[i])] for i in range(n)]


def test_signal_port_invariants():
    """The exact TV_strategy.pine port must respect the script's own rules:
    flags alternate (lastSig), and every flag bar satisfies the fire
    conditions (score threshold + the right side of EMA200)."""
    import numpy as np
    import pandas as pd
    import strategy3_signal as SIG

    ohlcv = _synth_ohlcv()
    res = SIG.compute(ohlcv, score_th=70, adx_th=20)
    flags, scores = res["flags"], res["scores"]

    fired = [int(f) for f in flags if f != 0]
    for a, b in zip(fired, fired[1:], strict=False):
        assert a != b, "same-direction flags must never repeat (lastSig rule)"

    closes = pd.Series([float(c[4]) for c in ohlcv])
    e200 = closes.ewm(span=200, adjust=False).mean().values
    for i, f in enumerate(flags):
        if f == 1:
            assert scores[i] >= 70 and closes[i] > e200[i]
        elif f == -1:
            assert scores[i] <= 30 and closes[i] < e200[i]

    assert not res["insufficient"]
    assert 0 <= res["score"] <= 100
    assert res["vegas"] in (-1, 0, 1)
    assert res["msb"] in ("bull", "bear")
    assert np.all((scores >= 0) & (scores <= 100))


def test_signal_port_trend_factors():
    """On a clean uptrend the port must read bullish everywhere."""
    import strategy3_signal as SIG
    n = 700
    ohlcv = [[i * 900_000, 100 + i, 100.6 + i, 99.6 + i, 100 + i, 100.0]
             for i in range(n)]
    res = SIG.compute(ohlcv)
    assert res["vegas"] == 1            # Vegas line rising → green
    assert res["msb"] == "bull"
    assert res["score"] >= 70


def test_adx_series_bounds():
    import pandas as pd
    import strategy3_signal as SIG
    ohlcv = _synth_ohlcv(400, seed=3)
    h = pd.Series([c[2] for c in ohlcv])
    l = pd.Series([c[3] for c in ohlcv])
    c = pd.Series([c[4] for c in ohlcv])
    adx = SIG.adx_series(h, l, c)
    tail = adx.iloc[50:]
    assert ((tail >= 0) & (tail <= 100)).all()


def test_bybit_qty_sizing():
    from strategy3_exec import qty_for
    # SOL-like: step 0.1, min 0.1, min notional 5 — 3 USDT × 4x @ 82 → 0.1 SOL
    qty, err = qty_for(82.0, 3.0, 4, 0.1, 0.1, 5.0)
    assert err == ""
    assert qty == 0.1
    # too small a margin → clear guidance instead of a silent zero-qty order
    qty, err = qty_for(82.0, 1.5, 4, 0.1, 0.1, 5.0)
    assert qty == 0.0
    assert "STRATEGY3_MARGIN_USDT" in err
    # HYPE-like: step/min 0.01 — plenty of room at small size
    qty, err = qty_for(40.0, 3.0, 4, 0.01, 0.01, 5.0)
    assert err == ""
    assert abs(qty - 0.3) < 1e-9


def test_bybit_qty_sizing_no_float_underflow():
    """Regression: plain `int(raw/step)` truncates a whole step too low when
    float division lands a hair BELOW an exact multiple (0.3/0.1 ==
    2.9999999999999996 in IEEE754) — a silent ~33% under-size in this exact
    shape. margin=0.03, lev=1, price=1 -> raw=0.03/step is contrived to hit
    that boundary; the real-world case is any margin*lev/price landing near
    an exact step multiple, which happens routinely as price moves."""
    from strategy3_exec import qty_for
    # raw = 3.0 * 1 / 10 = 0.3 exactly in decimal, step 0.1 -> 3 whole steps
    qty, err = qty_for(10.0, 3.0, 1, 0.1, 0.01, 0.0)
    assert err == ""
    assert abs(qty - 0.3) < 1e-9          # NOT 0.2 (the pre-fix truncated result)


def test_bybit_open_flip_dry_run_uses_passed_margin_leverage(monkeypatch):
    """Regression: open_flip's margin/leverage must come from its arguments
    (config.strategy3_params per symbol), not the global
    STRATEGY3_MARGIN_USDT/LEVERAGE — otherwise XAUT's bigger size would
    silently fall back to BTC/SOL/HYPE's smaller one."""
    import strategy3_exec as X
    monkeypatch.setattr(X, "is_live", lambda: False)
    monkeypatch.setattr(X, "keys_present", lambda: False)
    res = X.open_flip("XAUT/USDT:USDT", "long", 4000.0, 3900.0, 60.0, 50)
    assert res["ok"] is True
    assert res["dry"] is True
    assert abs(res["qty"] - (60.0 * 50 / 4000.0)) < 1e-9


def test_strategy3_open_flip_outcomes(monkeypatch):
    """Regression: open_flip must distinguish a transient failure (retry, flag
    stays live) from a final one (skip, flag consumed) — previously EVERY
    outcome set consumed=True unconditionally, silently dropping trades that
    failed only due to a network blip."""
    import strategy3_scanner as S3

    monkeypatch.setattr(S3.config, "STRATEGY3_LIVE", True)
    monkeypatch.setattr(S3.X, "is_live", lambda: False)   # skip the Bybit position check
    monkeypatch.setattr(S3, "_tg", lambda m: None)
    # never let the circuit breaker query the real account from a test — on
    # 2026-07-14 this exact test halted the LIVE engine mid-run
    import strategy3_risk
    monkeypatch.setattr(strategy3_risk, "entry_blocked", lambda: "")

    monkeypatch.setattr(S3.X, "open_flip", lambda *a, **k: {
        "ok": False, "error": "Read timed out", "dry": False})
    assert S3.open_flip("SOL/USDT:USDT", "long", 82.0, 98, 30.0, 50) == "retry"

    monkeypatch.setattr(S3.X, "open_flip", lambda *a, **k: {
        "ok": False, "error": "insufficient balance", "dry": False})
    assert S3.open_flip("SOL/USDT:USDT", "long", 82.0, 98, 30.0, 50) == "skip"

    monkeypatch.setattr(S3.X, "open_flip", lambda *a, **k: {
        "ok": True, "qty": 12.1, "dry": True})
    assert S3.open_flip("SOL/USDT:USDT", "long", 82.0, 98, 30.0, 50) == "opened"


def test_bybit_closed_pnl_summary(monkeypatch):
    """Regression: the /performance Bybit panel depends on this shape matching
    executor.realized_pnl_summary() closely enough to share frontend code —
    win_rate/profit_factor/streak/daily/by_symbol must all be present, and
    Bybit's already-net closedPnl must NOT be double-counted as commission."""
    import strategy3_exec as X

    class FakeEx:
        def private_get_v5_position_closed_pnl(self, params):
            if params.get("cursor"):
                return {"result": {"list": [], "nextPageCursor": ""}}
            # One SOL SHORT scaled out in TWO partial closes (same entry, both
            # positive) → must count as ONE win, not two; then a separate losing
            # SOL position at a different entry, and one HYPE win.
            return {"result": {"list": [
                {"symbol": "SOLUSDT", "avgEntryPrice": "80.39", "closedPnl": "3.0", "updatedTime": "1700000000000"},
                {"symbol": "SOLUSDT", "avgEntryPrice": "80.39", "closedPnl": "1.0", "updatedTime": "1700000600000"},
                {"symbol": "SOLUSDT", "avgEntryPrice": "81.06", "closedPnl": "-1.6", "updatedTime": "1700003600000"},
                {"symbol": "HYPEUSDT", "avgEntryPrice": "69.15", "closedPnl": "13.2", "updatedTime": "1700007200000"},
            ], "nextPageCursor": ""}}

    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "client", lambda: FakeEx())

    hist = X.closed_pnl_history()
    assert hist["ok"] is True
    assert len(hist["trades"]) == 4                                # raw ledger, per execution
    assert hist["trades"][0]["time"] >= hist["trades"][1]["time"]   # newest first

    summ = X.closed_pnl_summary()
    assert summ["ok"] is True
    # 4 raw rows but only 3 POSITIONS: the two same-entry SOL closes collapse
    assert summ["n_trades"] == 3
    assert summ["wins"] == 2 and summ["losses"] == 1
    assert abs(summ["net"] - 15.6) < 1e-9                          # 3.0+1.0-1.6+13.2
    # the collapsed winner nets +4.0 and records that it was 2 partial closes
    sol_win = next(t for t in summ["trades"]
                   if t["symbol"] == "SOLUSDT" and abs(t["pnl"] - 4.0) < 1e-9)
    assert sol_win["parts"] == 2
    assert summ["commission"] == 0.0 and summ["funding"] == 0.0   # not double-counted
    assert len(summ["daily"]) == 14
    assert {b["symbol"] for b in summ["by_symbol"]} == {"SOLUSDT", "HYPEUSDT"}


def test_bybit_closed_pnl_no_keys(monkeypatch):
    import strategy3_exec as X
    monkeypatch.setattr(X, "keys_present", lambda: False)
    assert X.closed_pnl_history() == {"ok": False, "error": "No Bybit API keys configured.", "trades": []}
    summ = X.closed_pnl_summary()
    assert summ["ok"] is False


def test_bybit_account_snapshot_no_keys(monkeypatch):
    import strategy3_exec as X
    monkeypatch.setattr(X, "keys_present", lambda: False)
    snap = X.account_snapshot()
    assert snap == {"ok": False, "error": "No Bybit API keys configured.",
                    "live": False, "balance": None, "positions": [],
                    # hand-opened positions the bot must never act on, but which
                    # the daily report has to show — see test_daily_report
                    "foreign": []}


def _reset_account_cache(monkeypatch):
    """account_snapshot() caches ~5s so N open dashboard tabs polling /api/bybit
    don't each hit Bybit — but that cache is module-level and outlives a single
    test, so every test exercising a fresh fetch must clear it first."""
    import strategy3_exec as X
    monkeypatch.setattr(X, "_account_cache", {"ts": 0.0, "data": None})


def test_bybit_account_snapshot_success(monkeypatch):
    """Regression for the /bybit web page: balance must parse Bybit's unified
    totalEquity/totalAvailableBalance/totalPerpUPL fields, and positions must
    be filtered to OUR symbols with a non-zero size only (a flat row or a
    manually-opened coin outside config.STRATEGY3_SYMBOLS must never appear)."""
    import strategy3_exec as X

    class FakeEx:
        def fetch_balance(self):
            return {"info": {"result": {"list": [{
                "totalEquity": "249.82", "totalWalletBalance": "249.82",
                "totalAvailableBalance": "200.0", "totalPerpUPL": "-1.5"}]}}}

        def fetch_positions(self, symbols, params=None):
            return [
                {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 3.0,
                 "notional": 246.0, "entryPrice": 82.0, "markPrice": 81.5,
                 "liquidationPrice": 60.0, "leverage": 50, "unrealizedPnl": -1.5,
                 "percentage": -0.6, "info": {"stopLoss": "79.0"}},
                {"symbol": "HYPE/USDT:USDT", "side": "long", "contracts": 0.0,
                 "notional": 0.0, "entryPrice": None, "markPrice": None,
                 "liquidationPrice": None, "leverage": 50, "unrealizedPnl": 0.0,
                 "percentage": 0.0, "info": {}},
                {"symbol": "DOGE/USDT:USDT", "side": "long", "contracts": 100.0,
                 "notional": 10.0, "entryPrice": 0.1, "markPrice": 0.1,
                 "liquidationPrice": 0.05, "leverage": 20, "unrealizedPnl": 0.0,
                 "percentage": 0.0, "info": {}},
            ]

    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "is_live", lambda: True)
    monkeypatch.setattr(X, "client", lambda: FakeEx())
    # pin the coin list — the user turns symbols on/off in .env, and this test's
    # fixtures assume SOL is "ours" (same preflight trap as the coin-list tests)
    monkeypatch.setattr(X.config, "STRATEGY3_SYMBOLS", ["SOL", "HYPE", "XAUT"])
    _reset_account_cache(monkeypatch)

    snap = X.account_snapshot()
    assert snap["ok"] is True
    assert snap["balance"] == {"equity": 249.82, "wallet": 249.82,
                                "available": 200.0, "unrealized_pnl": -1.5}
    assert len(snap["positions"]) == 1          # HYPE (flat) and DOGE (not ours) dropped
    p = snap["positions"][0]
    assert p["symbol"] == "SOL/USDT:USDT"
    assert p["sl"] == 79.0


def test_bybit_account_snapshot_error_is_not_fatal(monkeypatch):
    """Regression: an exchange/network blip must return ok=False with a
    message, never raise — this backs a live-money page that must not 500."""
    import strategy3_exec as X

    class BoomEx:
        def fetch_balance(self):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "is_live", lambda: True)
    monkeypatch.setattr(X, "client", lambda: BoomEx())
    _reset_account_cache(monkeypatch)

    snap = X.account_snapshot()
    assert snap["ok"] is False
    assert "connection reset" in snap["error"]
    assert snap["positions"] == []


def test_bybit_account_snapshot_is_cached_then_bypassed_with_force(monkeypatch):
    """THE FIX: uncached, this was called fresh on every /api/bybit hit — polled
    every 8-15s per open dashboard tab, on top of strategy3_scanner's own 45s
    loop and bot.py's guardian, each a separate process with its own Bybit
    client unaware of the others. That's what was tripping Bybit's 10006
    'Too many visits' — not any single loop being too fast on its own."""
    import strategy3_exec as X

    calls = {"n": 0}

    class CountingEx:
        def fetch_balance(self):
            calls["n"] += 1
            return {"info": {"result": {"list": [{
                "totalEquity": "100.0", "totalWalletBalance": "100.0",
                "totalAvailableBalance": "100.0", "totalPerpUPL": "0.0"}]}}}

        def fetch_positions(self, symbols, params=None):
            return []

    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "is_live", lambda: True)
    monkeypatch.setattr(X, "client", lambda: CountingEx())
    _reset_account_cache(monkeypatch)

    first = X.account_snapshot()
    second = X.account_snapshot()          # within the TTL — must reuse, not re-fetch
    assert calls["n"] == 1
    assert second == first

    fresh = X.account_snapshot(force=True)  # force= bypasses the cache
    assert calls["n"] == 2
    assert fresh == first


# ── V2 anti-chop break-even (HYPE by default) ────────────────────────────────
# manage_breakeven: once mark is BE_TRIGGER_PCT in profit, the stop jumps to
# entry ± BE_OFFSET_PCT — ONCE per position, only for STRATEGY3_BE_SYMBOLS.

def _be_setup(monkeypatch):
    import strategy3_scanner as S3
    monkeypatch.setattr(S3.config, "STRATEGY3_BE_SYMBOLS", ["HYPE"])
    monkeypatch.setattr(S3.config, "STRATEGY3_BE_TRIGGER_PCT", 0.0075)
    monkeypatch.setattr(S3.config, "STRATEGY3_BE_OFFSET_PCT", 0.0015)
    monkeypatch.setattr(S3, "_tg", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(S3.X, "set_stop", lambda sym, px: calls.append((sym, px)))
    return S3, calls


def test_breakeven_moves_stop_once_when_triggered(monkeypatch):
    S3, calls = _be_setup(monkeypatch)
    st = {"pos_dir": "long", "be_armed": False}
    pos = {"side": "long", "entry": 40.0, "mark": 40.0 * 1.0075, "sl": 39.4}
    S3.manage_breakeven("HYPE/USDT:USDT", st, pos)
    assert st["be_armed"] is True
    assert len(calls) == 1
    assert abs(calls[0][1] - 40.0 * 1.0015) < 1e-9
    # armed → never moved again, even deeper in profit
    pos["mark"] = 41.0
    S3.manage_breakeven("HYPE/USDT:USDT", st, pos)
    assert len(calls) == 1


def test_breakeven_not_triggered_below_threshold(monkeypatch):
    S3, calls = _be_setup(monkeypatch)
    st = {"pos_dir": "long", "be_armed": False}
    pos = {"side": "long", "entry": 40.0, "mark": 40.0 * 1.005, "sl": 39.4}
    S3.manage_breakeven("HYPE/USDT:USDT", st, pos)
    assert st.get("be_armed") is False
    assert calls == []


def test_breakeven_short_side(monkeypatch):
    S3, calls = _be_setup(monkeypatch)
    st = {"pos_dir": "short", "be_armed": False}
    pos = {"side": "short", "entry": 40.0, "mark": 40.0 * (1 - 0.0075), "sl": 40.6}
    S3.manage_breakeven("HYPE/USDT:USDT", st, pos)
    assert st["be_armed"] is True
    assert abs(calls[0][1] - 40.0 * (1 - 0.0015)) < 1e-9


def test_breakeven_only_for_configured_symbols(monkeypatch):
    """SOL/XAUT keep the plain rules — deep in profit must NOT move the stop."""
    S3, calls = _be_setup(monkeypatch)
    for sym in ("SOL/USDT:USDT", "XAUT/USDT:USDT"):
        st = {"pos_dir": "long", "be_armed": False}
        pos = {"side": "long", "entry": 100.0, "mark": 105.0, "sl": 98.5}
        S3.manage_breakeven(sym, st, pos)
        assert st.get("be_armed") is False
    assert calls == []


def test_breakeven_failed_move_retries_next_poll(monkeypatch):
    """A Bybit blip must leave be_armed False so the next ~45s poll retries —
    and the emergency stop stays resting in the meantime."""
    S3, _ = _be_setup(monkeypatch)

    def boom(sym, px):
        raise RuntimeError("bybit 502")
    monkeypatch.setattr(S3.X, "set_stop", boom)
    st = {"pos_dir": "long", "be_armed": False}
    pos = {"side": "long", "entry": 40.0, "mark": 41.0, "sl": 39.4}
    S3.manage_breakeven("HYPE/USDT:USDT", st, pos)   # must not raise
    assert st.get("be_armed") is False


def test_guardian_rearms_at_breakeven_level_once_armed(monkeypatch):
    """If the stop vanishes AFTER the break-even jump, the guardian must
    re-arm it at the BE level, not fall back to the wide emergency stop."""
    S3, _ = _be_setup(monkeypatch)
    monkeypatch.setattr(S3.X, "is_live", lambda: True)
    pos = {"side": "long", "entry": 40.0, "mark": 40.5, "sl": None}
    monkeypatch.setattr(S3.X, "get_position", lambda sym: dict(pos))
    ensured = []
    monkeypatch.setattr(S3.X, "ensure_stop",
                        lambda sym, sl_price=None, pos=None: ensured.append(sl_price))
    st = {"pos_dir": "long", "be_armed": True, "consumed": True}
    S3.reconcile_position("HYPE/USDT:USDT", st)
    assert len(ensured) == 1
    assert abs(ensured[0] - 40.0 * 1.0015) < 1e-9
    # …but before the BE jump, the guardian re-arms at THIS symbol's own
    # emergency distance (per-symbol sl_pct — the OCC pair's stop differs from
    # the flag-flip global, so None/"derive from the global" would be wrong)
    ensured.clear()
    st = {"pos_dir": "long", "be_armed": False, "consumed": True}
    monkeypatch.setattr(S3.config, "STRATEGY3_BE_TRIGGER_PCT", 9.9)  # keep BE untriggered
    monkeypatch.setattr(S3.config, "STRATEGY3_EMERGENCY_SL_PCT", 0.015)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SYMBOLS", [])
    S3.reconcile_position("HYPE/USDT:USDT", st)
    assert len(ensured) == 1
    assert abs(ensured[0] - 40.0 * 0.985) < 1e-9


def test_position_gone_resets_breakeven(monkeypatch):
    S3, _ = _be_setup(monkeypatch)
    monkeypatch.setattr(S3.X, "is_live", lambda: True)
    monkeypatch.setattr(S3.X, "get_position", lambda sym: None)
    st = {"pos_dir": "long", "be_armed": True, "consumed": False}
    S3.reconcile_position("HYPE/USDT:USDT", st)
    assert st["pos_dir"] is None
    assert st["consumed"] is True
    assert st["be_armed"] is False


def test_strategy3_retry_gives_up_after_3_attempts(monkeypatch):
    """Regression: a flag that keeps failing transiently must eventually be
    consumed (not retried forever), but must NOT be silently dropped on the
    first blip either.

    Uses deterministic, monotonically-advancing synthetic timestamps (never
    time.time()) so each of the 3 step() calls reliably looks like a new
    closed 15m candle regardless of how fast the test itself runs."""
    import strategy3_scanner as S3

    monkeypatch.setattr(S3, "open_flip", lambda *a, **k: "retry")
    monkeypatch.setattr(S3, "_tg", lambda *a, **k: None)
    monkeypatch.setattr(S3, "reconcile_position", lambda *a, **k: None)
    monkeypatch.setattr(S3, "save_state", lambda *a, **k: None)

    class FakeClient:
        """Each .call() returns a batch whose last candle is exactly one
        TF_SEC ahead of the previous batch — a deterministic 'new candle
        closed' on every invocation, with no dependence on wall-clock time."""
        def __init__(self):
            self.call_index = 0

        def call(self, method, sym, tf, since, limit):
            self.call_index += 1
            # Large base offset: the state's "no candle processed yet" sentinel
            # is 0, so a real timestamp must never collide with it (a naive
            # small offset landed exactly on 0 on the first call and silently
            # ate that iteration — this bit the original version of this test).
            last_open_sec = 10_000 * S3.TF_SEC + self.call_index * S3.TF_SEC
            return [[int((last_open_sec - (limit - 1 - i) * S3.TF_SEC) * 1000),
                     1, 1.1, 0.9, 1, 100] for i in range(limit)]

    monkeypatch.setattr(S3, "closed_candles", lambda raw, now=None: raw[:-1])
    monkeypatch.setattr(S3, "flag_on_last_bar", lambda ohlcv: (
        "long", {"score": 90, "vegas": 1, "msb": "bull", "price": 82.0, "insufficient": False}))
    monkeypatch.setattr(S3, "symbols", lambda: ["SOL/USDT:USDT"])
    # pin the ENGINE too: this tests the flag-flip retry path, and the real
    # .env may route SOL to the OCC engine (the usual preflight trap)
    monkeypatch.setattr(S3.config, "STRATEGY3_OCC_SYMBOLS", [])
    # …and the FEED: the real .env defaults S3 to Bybit candles (2026-07-11),
    # which would route the fetch past FakeClient to the real Bybit client
    monkeypatch.setattr(S3.config, "STRATEGY3_FEED", "binance")
    monkeypatch.setattr(S3.config, "STRATEGY3_OVERRIDES", {})

    state = {}
    client = FakeClient()
    for _ in range(3):
        S3.step(client, state)
    st = state["SOL/USDT:USDT"]
    assert st["open_attempts"] == 3
    assert st["consumed"] is True          # gave up — no infinite retry
    assert st["pos_dir"] is None           # and never fabricated a fake position
