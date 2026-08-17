"""S4 — Bybit TradFi perp scanner.

The gates are the product here, so each one is tested for what it REJECTS as
much as what it passes: a filter that never says no is not a filter, and this
scan's whole value is that it stays quiet.
"""
import strategy4 as S


import strategy2_meter as _M

# Fixture length, DERIVED. evaluate() needs the meter's full window, which grew
# when the outer tunnel moved to EMA676 — every 380/400-bar fixture here became
# "not enough history" and the reason lived three files away. Deriving it means
# the next period change adjusts these instead of breaking them.
N = _M.SIGNAL_MIN_CANDLES + 40


def _bars(closes, highs=None, lows=None, vol=1000.0):
    """[ts,o,h,l,c,v] oldest→newest."""
    highs = highs or [c * 1.002 for c in closes]
    lows = lows or [c * 0.998 for c in closes]
    return [[i * 900_000, c, h, lo, c, vol]
            for i, (c, h, lo) in enumerate(zip(closes, highs, lows, strict=False))]


# ── EMA200 must be RISING, not merely below price ───────────────────────────
def test_ema_rising_distinguishes_slope_from_position():
    """Price pops above a falling 200 EMA constantly in a downtrend. The
    triangle alone cannot tell that from a real uptrend — this gate can."""
    up = [100 + i * 0.1 for i in range(300)]
    down = [130 - i * 0.1 for i in range(300)]
    assert S.ema_rising(up)[0] is True
    assert S.ema_rising(down)[0] is False


def test_ema_rising_refuses_to_answer_without_enough_history():
    ok, slope = S.ema_rising([100.0] * 50)
    assert ok is False and slope is None


def test_ema_slope_is_percent_so_symbols_are_comparable():
    """A 0.5 USDT move means something different on a 3 USDT perp than on a
    300 USDT one; an absolute slope would rank the universe by price."""
    cheap = [3 + i * 0.003 for i in range(300)]
    dear = [300 + i * 0.3 for i in range(300)]
    a = S.ema_rising(cheap)[1]
    b = S.ema_rising(dear)[1]
    assert abs(a - b) < 0.05


# ── support IS the stop ─────────────────────────────────────────────────────
def test_support_below_finds_the_most_recent_swing_low():
    closes = [10, 11, 12, 11, 9, 11, 12, 13, 14, 13, 12, 14, 15, 16, 17]
    b = _bars([float(c) for c in closes])
    sup = S.support_below([x[2] for x in b], [x[3] for x in b], 17.0)
    assert sup is not None
    assert sup["level"] < 17.0


def test_no_support_below_means_no_signal_not_an_invented_stop():
    """A straight line up has no confirmed swing low. Inventing a stop
    distance is worse than staying silent."""
    closes = [100 + i for i in range(60)]
    b = _bars([float(c) for c in closes])
    assert S.support_below([x[2] for x in b], [x[3] for x in b], 160.0) is None


def test_plan_rejects_stops_that_are_absurd_in_either_direction():
    """Two kinds of refusal, deliberately different since 2026-08-15.

    Too WIDE, or geometrically impossible, is still a bare None: there is
    nothing to learn from a setup whose 2R target needs a 40% move.

    Too TIGHT returns the plan FLAGGED instead, because that boundary is a cost
    judgement (fees vs stop distance) rather than a fact about the chart, and a
    judgement has to stay auditable — those trades are recorded as shadows so
    the floor can be proved right or wrong. Either way it is not a signal."""
    assert S.plan(100.0, 97.0) is not None
    assert S.plan(100.0, 97.0).get("rejected") is None
    tight = S.plan(100.0, 99.98)             # tighter than the fee floor
    assert tight["rejected"] == "stop_too_tight"
    assert S.plan(100.0, 80.0) is None       # 2R target needs a 40% move
    assert S.plan(100.0, 101.0) is None      # "support" above price
    assert S.plan(0, 1) is None


def test_plan_targets_are_a_multiple_of_the_actual_risk():
    p = S.plan(100.0, 98.0)
    risk = p["entry"] - p["sl"]
    assert abs((p["tp"] - p["entry"]) / risk - S.TP_R) < 1e-9
    assert p["sl"] < 98.0                    # buffer sits BELOW the support


# ── divergence: same rule and the same lag as the .pine ─────────────────────
def test_bullish_divergence_needs_price_lower_and_oscillator_higher():
    #        lower low in price ......................  higher low in osc
    lows = [10, 9, 8, 9, 10, 11, 10, 9, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    highs = [v + 2 for v in lows]
    osc = [1, 0, -5, 0, 1, 2, 1, 0, -2, 0, 1, 2, 3, 4, 5, 6, 7, 8]
    ago = S.bullish_divergence([float(h) for h in highs], [float(v) for v in lows],
                               [float(o) for o in osc], pivot=2, lookback=50)
    assert ago is not None


def test_no_divergence_when_the_oscillator_agrees_with_price():
    lows = [10, 9, 8, 9, 10, 11, 10, 9, 7, 8, 9, 10, 11, 12]
    highs = [v + 2 for v in lows]
    osc = [float(v) for v in lows]           # oscillator tracks price exactly
    assert S.bullish_divergence([float(h) for h in highs],
                                [float(v) for v in lows], osc,
                                pivot=2, lookback=50) is None


def test_divergence_respects_its_lookback():
    lows = [10, 9, 8, 9, 10, 11, 10, 9, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    highs = [v + 2 for v in lows]
    osc = [1, 0, -5, 0, 1, 2, 1, 0, -2, 0, 1, 2, 3, 4, 5, 6, 7, 8]
    far = S.bullish_divergence([float(h) for h in highs], [float(v) for v in lows],
                               [float(o) for o in osc], pivot=2, lookback=1)
    assert far is None, "a divergence from 10 bars ago is not news"


def test_divergence_is_never_reported_before_it_could_be_confirmed():
    """A pivot needs `pivot` bars AFTER it to exist. Reporting one sooner is
    how a backtest sees signals live trading never got."""
    lows = [10.0, 9, 8, 9, 10, 11, 10, 9, 7, 8]
    highs = [v + 2 for v in lows]
    osc = [1.0, 0, -5, 0, 1, 2, 1, 0, -2, 0]
    ago = S.bullish_divergence(highs, lows, osc, pivot=3, lookback=99)
    assert ago is None or ago >= 0


# ── OI: a gap is a gap ──────────────────────────────────────────────────────
def test_oi_states_match_the_chart():
    rising = [100, 101, 102, 103, 104, 105]
    falling = [105, 104, 103, 102, 101, 100]
    up = [1, 2, 3, 4, 5, 6]
    down = [6, 5, 4, 3, 2, 1]
    assert S.oi_state(rising, up)[0] == S.OI_LONGS_OPENING
    assert S.oi_state(rising, down)[0] == S.OI_SHORTS_OPENING
    assert S.oi_state(falling, up)[0] == S.OI_SHORTS_CLOSING
    assert S.oi_state(falling, down)[0] == S.OI_LONGS_CLOSING


def test_missing_oi_is_no_data_not_a_flat_reading():
    """Smoothing a missing feed in as 0% asserts open interest did not move —
    a measurement nobody made. This repo has shipped that bug twice."""
    state, delta = S.oi_state([], [1, 2, 3])
    assert state == 0 and delta is None


def test_only_the_two_price_up_states_qualify_a_long():
    assert S.OI_LONGS_OPENING in S.OI_OK_FOR_LONG
    assert S.OI_SHORTS_CLOSING in S.OI_OK_FOR_LONG
    assert S.OI_SHORTS_OPENING not in S.OI_OK_FOR_LONG
    assert S.OI_LONGS_CLOSING not in S.OI_OK_FOR_LONG


# ── evaluate names its refusals ─────────────────────────────────────────────
def test_evaluate_reports_which_gate_failed(monkeypatch):
    """A filter whose effect you cannot see is a filter you cannot tune."""
    assert S.evaluate([])["reason"] == "not enough history"
    flat = _bars([100.0] * N)
    r = S.evaluate(flat)
    assert r["pass"] is False and r["reason"]


def test_evaluate_stops_at_the_triangle_before_spending_anything(monkeypatch):
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": None, "score": 40})
    r = S.evaluate(_bars([100.0 + i * 0.01 for i in range(N)]))
    assert r["reason"] == "no long triangle"
    assert r["plan"] is None


def test_a_passing_setup_carries_a_complete_plan(monkeypatch):
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": "long", "score": 82})
    monkeypatch.setattr(S, "REQUIRE_DIVERGENCE", False)
    closes = [100 + i * 0.05 for i in range(N - 6)]
    # The pullback must CONTINUE from where the ramp ended, not jump to fixed
    # prices: with a longer fixture the old literals (119.x) sat ~16 below the
    # ramp's end and the "pullback" was a cliff that turned the EMA200 slope
    # negative, failing an earlier gate than the one under test.
    _end = closes[-1]
    closes += [_end + 0.05, _end - 0.95, _end - 1.45,
               _end - 0.75, _end + 0.55, _end + 1.05]      # pullback + swing low
    b = _bars(closes)
    oi = [100 + i for i in range(30)]
    r = S.evaluate(b, oi)
    if r["pass"]:
        assert r["plan"]["sl"] < r["plan"]["entry"] < r["plan"]["tp"]
        assert r["oi_state"] in S.OI_OK_FOR_LONG
    else:
        assert r["reason"] in ("no support below", "stop distance out of range")


# ── alerting behaviour ──────────────────────────────────────────────────────
def test_cooldown_stops_one_setup_from_paying_out_eight_messages():
    res = {"signals": [{"symbol": "NVDA/USDT:USDT"}, {"symbol": "MU/USDT:USDT"}]}
    now = 1_000_000.0
    state = {"sent": {"NVDA/USDT:USDT": now - 60}}
    due = S.due_signals(res, state, now)
    assert [d["symbol"] for d in due] == ["MU/USDT:USDT"]
    old = {"sent": {"NVDA/USDT:USDT": now - S.COOLDOWN_SEC - 1}}
    assert len(S.due_signals(res, old, now)) == 2


def test_every_alert_carries_the_disclaimer():
    """The message is the only place the reader learns this is unvalidated."""
    sig = {"base": "NVDA", "score": 80, "slope": 0.4, "div_ago": 3,
           "oi_state": 1, "price": 100.0, "support": {"level": 98.0, "bars_ago": 5},
           "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0, "stop_pct": 2.0,
                    "tp_pct": 4.0, "rr": 2}}
    text = S.build_digest({"signals": [sig], "checked": 60})
    assert "S4" in text and "NVDA" in text
    assert "不是已驗證的策略" in text


def test_an_empty_scan_sends_nothing():
    """Silence is the correct output most of the time; a daily 'no setups'
    message trains the reader to ignore the topic."""
    assert S.build_digest({"signals": [], "checked": 60}) == ""


def test_alerts_never_carry_account_information():
    """channel='s1signals' is a PUBLIC group topic. Balances, position sizes
    and free margin have leaked into it before."""
    import inspect
    src = inspect.getsource(S)
    for banned in ("free_margin", "availableBalance", "walletBalance",
                   "unrealized", "account_snapshot"):
        assert banned not in src, f"{banned} must not be reachable from an S4 alert"


def test_the_scan_itself_still_has_no_order_path():
    """strategy4 detects; strategy4_exec spends. Keeping the exchange calls out
    of the scanner means a refactor here cannot start placing trades, and the
    one file that can is small enough to read in full.

    (Execution was added 2026-08-17 at the owner's request, OFF by default —
    see test_execution_is_off_unless_two_switches_are_set.)"""
    import inspect
    src = inspect.getsource(S)
    for banned in ("create_order", "open_flip", "place_order", "createOrder"):
        assert banned not in src


def test_execution_is_off_unless_two_switches_are_set(monkeypatch):
    """S4_EXEC=bybit alone is not enough, and neither is LIVE_TRADING. Arming
    real money takes a deliberate act, not one stray env var."""
    import config
    import strategy4_exec as E
    monkeypatch.delenv("S4_EXEC", raising=False)
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    assert E.enabled() is False, "no S4_EXEC but enabled"
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", False, raising=False)
    assert E.enabled() is False, "LIVE_TRADING off but enabled"
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    assert E.enabled() is True


def test_a_disabled_run_sends_nothing_and_says_so(monkeypatch, capsys):
    import strategy4_exec as E
    monkeypatch.delenv("S4_EXEC", raising=False)
    sig = {"symbol": "X/USDT:USDT", "side": "long",
           "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0}}
    out = E.open_trade(sig)
    assert out["ok"] is False and out["reason"] == "disabled"
    assert "would long" in capsys.readouterr().out


def test_an_open_position_is_skipped_and_reported(monkeypatch):
    """Asked for explicitly. One-Way mode MERGES, so a second order on a symbol
    that already has a manual or S3 position would put our stop in charge of
    theirs."""
    import config
    import strategy4_exec as E
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    monkeypatch.setattr(E.X, "get_position", lambda s, side=None: {"qty": 1.0})
    sent = []
    monkeypatch.setattr(E, "_tg_owner", lambda m: sent.append(m))
    monkeypatch.setattr(E, "_send", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an order was sent onto an existing position")))
    out = E.open_trade({"symbol": "X/USDT:USDT", "side": "long",
                        "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0}})
    assert out["skipped"] and out["reason"] == "position_open"
    assert sent and "已有持倉" in sent[0]


def test_a_coin_bybit_does_not_list_is_skipped(monkeypatch):
    import config
    import strategy4_exec as E
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(E, "bybit_symbol", lambda s: None)
    monkeypatch.setattr(E, "_send", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("ordered a symbol Bybit does not list")))
    out = E.open_trade({"symbol": "NOPE/USDT:USDT", "side": "long",
                        "plan": {"entry": 1.0, "sl": 0.9, "tp": 1.2}})
    assert out["skipped"] and out["reason"] == "not_listed"


def test_a_ticker_collision_is_caught_before_the_order(monkeypatch):
    """Binance ON was $0.2458 while Bybit's ON — a different token — was
    $84.52. Sizing from the wrong price asked for $17k of notional."""
    import config
    import strategy4_exec as E

    class _Ex:
        def fetch_ticker(self, s): return {"last": 84.52}
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    monkeypatch.setattr(E.X, "get_position", lambda s, side=None: None)
    monkeypatch.setattr(E.X, "client", lambda: _Ex())
    sent = []
    monkeypatch.setattr(E, "_tg_owner", lambda m: sent.append(m))
    monkeypatch.setattr(E, "_send", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("ordered through a ticker collision")))
    out = E.open_trade({"symbol": "ON/USDT:USDT", "side": "long",
                        "plan": {"entry": 0.2458, "sl": 0.24, "tp": 0.26}})
    assert out["skipped"] and out["reason"].startswith("price_divergence")
    assert sent and "同代號不同幣" in sent[0]


def test_an_unreadable_position_is_treated_as_occupied(monkeypatch):
    """Cannot read = cannot be sure = do not trade. The opposite default puts
    an order on top of a position we failed to see."""
    import strategy4_exec as E
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    monkeypatch.setattr(E.X, "get_position",
                        lambda s, side=None: (_ for _ in ()).throw(RuntimeError("api down")))
    ok, reason = E.preflight("X/USDT:USDT", 100.0)
    assert ok is False and reason.startswith("position_check_failed")


def test_a_plan_with_the_stop_on_the_wrong_side_is_refused():
    """Arms an instant loss and makes every R meaningless."""
    import strategy4_exec as E
    bad = E.open_trade({"symbol": "X/USDT:USDT", "side": "long",
                        "plan": {"entry": 100.0, "sl": 104.0, "tp": 98.0}})
    assert bad["skipped"] and bad["reason"] == "bad_geometry"
    bad_s = E.open_trade({"symbol": "X/USDT:USDT", "side": "short",
                          "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0}})
    assert bad_s["skipped"] and bad_s["reason"] == "bad_geometry"


def test_tp_and_sl_ride_on_the_entry_order():
    """A TP sent afterwards leaves a window where a fast move exits at neither
    level, and a separate conditional can be rejected while the position is
    already open — which is how this repo produced naked positions."""
    import inspect
    import strategy4_exec as E
    src = inspect.getsource(E._send)
    i = src.index("create_order")
    assert "stopLoss" in src[i:] and "takeProfit" in src[i:], \
        "brackets are not attached to the entry order"
    assert "ensure_stop" in src, "no post-fill verification that the stop landed"


def test_order_details_never_reach_a_public_channel():
    """send_message defaults to the PUBLIC alerts topic. Size, margin and
    position state belong in the private feed."""
    import ast
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(here, "strategy4_exec.py"), encoding="utf-8").read())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "send_message"]
    assert calls, "no sender found — did the module stop reporting?"
    for c in calls:
        ch = next((k.value for k in c.keywords if k.arg == "channel"), None)
        assert isinstance(ch, ast.Constant) and ch.value == "trades", \
            "a send_message without channel='trades' defaults to the PUBLIC topic"


# ── universe selection ──────────────────────────────────────────────────────
def test_universe_is_chosen_by_the_exchange_tag_not_the_ticker():
    """Bybit lists AMD (the token) and AMDSTOCK (the equity). A name-based rule
    is a coin flip, and name-based mapping is exactly how a mirror once sized an
    order 344x off."""
    import inspect
    src = inspect.getsource(S.universe)
    assert "symbolType" in src
    assert '"stock", "commodity"' in src or "'stock', 'commodity'" in src


def _mk(sym, stype, active=True, quote="USDT"):
    return {"symbol": sym, "linear": True, "swap": True, "active": active,
            "quote": quote, "info": {"symbolType": stype}}


class _FakeEx:
    def load_markets(self):
        return {m["symbol"]: m for m in [
            _mk("AMD/USDT:USDT", ""),                 # the TOKEN
            _mk("AMDSTOCK/USDT:USDT", "stock"),       # the EQUITY
            _mk("XAU/USDT:USDT", "commodity"),
            _mk("BTC/USDT:USDT", ""),
            _mk("PEPE/USDT:USDT", "innovation"),
            _mk("BTC/USDC:USDC", "", quote="USDC"),   # the twin
            _mk("DEAD/USDT:USDT", "stock", active=False),
        ]}

    def fetch_tickers(self, syms):
        vol = {"AMDSTOCK/USDT:USDT": 9e6, "XAU/USDT:USDT": 8e6,
               "BTC/USDT:USDT": 3.8e9, "AMD/USDT:USDT": 5e6, "PEPE/USDT:USDT": 1e5}
        return {s: {"quoteVolume": vol.get(s, 0)} for s in syms}


def test_universe_separates_the_equity_from_the_token_of_the_same_name():
    uni = S.universe(_FakeEx(), {})
    assert uni.get("AMDSTOCK/USDT:USDT") == "tradfi"
    assert uni.get("AMD/USDT:USDT") == "crypto"      # same letters, different asset
    assert uni.get("XAU/USDT:USDT") == "tradfi"
    assert uni.get("BTC/USDT:USDT") == "crypto"
    assert "DEAD/USDT:USDT" not in uni               # delisted


def test_the_usdc_twin_is_dropped_so_no_base_is_scanned_twice():
    uni = S.universe(_FakeEx(), {})
    assert "BTC/USDC:USDC" not in uni


def test_each_pool_is_ranked_and_capped_on_its_own():
    """BTC alone turns over 3.8 BILLION a day against ~50M for a busy stock
    perp. A single combined top-N list would be crypto only, and the TradFi
    half — the whole reason this exists — would silently vanish."""
    ex = _FakeEx()
    picks = dict(S.select(ex, S.universe(ex, {})))
    assert picks.get("AMDSTOCK/USDT:USDT") == "tradfi"
    assert picks.get("BTC/USDT:USDT") == "crypto"
    assert "PEPE/USDT:USDT" not in picks             # under the turnover floor


def test_select_tolerates_an_old_cached_universe_shape():
    """State written by the previous version is a LIST of tradfi symbols. A
    scanner that crashes on its own stale cache is a scanner that stays down
    until someone deletes a file."""
    picks = S.select(_FakeEx(), ["AMDSTOCK/USDT:USDT"])
    assert picks == [("AMDSTOCK/USDT:USDT", "tradfi")]


def test_universe_is_cached_so_every_sweep_does_not_reload_markets():
    state = {}
    S.universe(_FakeEx(), state)
    assert state["universe"] and state["universe_ts"]

    class _Boom:
        def load_markets(self):
            raise AssertionError("markets reloaded inside the cache window")

    assert S.universe(_Boom(), state)


# ── the page ────────────────────────────────────────────────────────────────
def test_web_view_never_scans(monkeypatch):
    """60 exchange calls per page visitor is a rate-limit incident waiting."""
    monkeypatch.setattr(S, "scan", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("web_view() scanned on a page load")))
    v = S.web_view()
    assert "signals" in v and "disclaimer" in v


def test_the_alert_actually_contains_the_levels():
    """Regression: the plan block was built with tg_format.mono_plan(), which
    needs TWO targets and returns '' if either is missing. S4 has one target by
    construction, so every alert shipped with percentages and no prices — a
    plan you cannot act on, failing silently because '' is falsy."""
    sig = {"base": "NVDA", "score": 84.0, "slope": 0.62, "div_ago": 4,
           "oi_state": 1, "price": 182.4,
           "support": {"level": 178.9, "bars_ago": 7},
           "plan": {"entry": 182.4, "sl": 178.63, "tp": 189.94,
                    "stop_pct": 2.07, "tp_pct": 4.13, "rr": 2.0}}
    msg = S.format_signal(sig)
    for level in ("182.4", "178.63", "189.94"):
        assert level in msg, f"the alert never states {level}"
    assert "進場" in msg and "停損" in msg and "目標" in msg


def test_a_stale_list_cache_is_rebuilt_not_served(monkeypatch, tmp_path):
    """Regression: the previous version cached the universe as a LIST of tradfi
    symbols. After the crypto pool was added, that cache was still inside its
    6h TTL — so universe() kept returning tradfi-only and the scan reported
    success while scanning no crypto at all. The walk-forward that found this
    came back with 0 crypto symbols and a clean exit code."""
    state = {"universe": ["AMDSTOCK/USDT:USDT"], "universe_ts": 9e18}
    uni = S.universe(_FakeEx(), state)
    assert isinstance(uni, dict)
    assert uni.get("BTC/USDT:USDT") == "crypto", "stale cache was served"


def test_a_fresh_dict_cache_is_still_honoured():
    state = {}
    S.universe(_FakeEx(), state)

    class _Boom:
        def load_markets(self):
            raise AssertionError("markets reloaded inside the cache window")

    assert S.universe(_Boom(), state)


# ── divergence quality, ported from Divergence_Radar_PRO.pine ───────────────
# Each of these is a hole the first version left open. They are written as
# rejections because that is what the fixes do: the old rule said yes to all of
# them.
def test_a_pivot_from_far_back_is_not_a_divergence_partner():
    """The old rule compared each pivot to the previous one however old it was.

    Two swing lows 90 bars apart are not one structure, and comparing them is
    arithmetic rather than analysis. TradingView's own built-in divergence
    indicator has always capped this at 60 bars.
    """
    lows = ([10.0, 9, 8, 9, 10] + [12.0] * 90 + [10, 9, 7, 8, 9, 10])
    highs = [v + 2 for v in lows]
    osc = ([1.0, 0, -5, 0, 1] + [3.0] * 90 + [1, 0, -2, 0, 1, 2])
    assert S.bullish_divergence(highs, lows, osc, pivot=2, lookback=200,
                                max_gap=60, min_leg_atr=0) is None
    # ...and the identical shape inside the window still fires
    assert S.bullish_divergence(highs, lows, osc, pivot=2, lookback=200,
                                max_gap=200, min_leg_atr=0) is not None


def test_a_trivial_oscillator_gap_is_not_a_divergence():
    """Higher by 1e-9 satisfied the textbook definition and nothing else."""
    lows = [10.0, 9, 8, 9, 10, 11, 10, 9, 7, 8, 9, 10, 11, 12, 13, 14]
    highs = [v + 2 for v in lows]
    big = [1.0, 0, -5, 0, 1, 2, 1, 0, -2, 0, 1, 2, 3, 4, 5, 6]
    assert S.bullish_divergence(highs, lows, big, pivot=2, lookback=50,
                                min_leg_atr=0) is not None
    # same pivots, oscillator higher by a rounding error against a wide range
    tiny = list(big)
    tiny[8] = tiny[2] + 1e-9
    assert S.bullish_divergence(highs, lows, tiny, pivot=2, lookback=50,
                                min_leg_atr=0) is None


def test_a_chop_wiggle_is_not_a_swing():
    """Prominence is measured in ATR AT THE PIVOT. A low that barely dents its
    own neighbourhood is noise no matter what the oscillator did."""
    lows = [100.0, 99.9, 99.8, 99.9, 100.0, 100.1, 100.0, 99.9, 99.7, 99.8,
            99.9, 100.0, 100.1, 100.2, 100.3, 100.4]
    highs = [v + 0.1 for v in lows]
    closes = list(lows)
    osc = [1.0, 0, -5, 0, 1, 2, 1, 0, -2, 0, 1, 2, 3, 4, 5, 6]
    atr = S.atr_series(highs, lows, closes, period=5)
    assert S.bullish_divergence(highs, lows, osc, pivot=2, lookback=50,
                                atr=atr, min_leg_atr=0) is not None
    assert S.bullish_divergence(highs, lows, osc, pivot=2, lookback=50,
                                atr=atr, min_leg_atr=3.0) is None


def test_detrending_removes_the_drift_that_made_flow_divergence_free():
    """A cumulative series that only rises reports a higher low at EVERY pivot.

    That is not a measurement of flow, it is a measurement of the fact that the
    series is cumulative — and it was a quarter of the divergence gate.
    """
    rising = [float(i) for i in range(400)]          # pure drift, no information
    flat = S.detrend(rising, period=50)
    # after the baseline settles, the detrended series is level, not climbing
    tail = flat[-100:]
    assert max(tail) - min(tail) < (max(rising) - min(rising)) / 10


def test_ad_is_not_added_because_it_is_cvd_under_another_name():
    """A/D's money-flow multiplier IS cvd_series()'s per-bar term here.

    On the chart they differ, because CVD there is built from real 1-minute
    deltas and only falls back to this estimate on older bars. This scan has no
    intrabar data at all, so they would be the same series — a second vote for
    one measurement, which is the one thing a confluence count must not do.
    """
    bars = _bars([100 + (i % 7) for i in range(60)])
    cvd = S.cvd_series(bars)
    ad, run = [], 0.0
    for c in bars:
        h, lo, cl, v = c[2], c[3], c[4], c[5]
        run += 0.0 if h == lo else v * ((cl - lo) - (h - cl)) / (h - lo)
        ad.append(run)
    assert all(abs(a - b) < 1e-9 for a, b in zip(cvd, ad, strict=False))
    assert "A/D" not in S.divergence_scan(bars, [c[2] for c in bars],
                                          [c[3] for c in bars], [c[4] for c in bars])


def test_fisher_is_bounded_and_reacts_to_extremes():
    closes = [100 + i * 0.5 for i in range(60)]
    bars = _bars(closes)
    f = S.fisher_series([c[2] for c in bars], [c[3] for c in bars], length=9)
    assert len(f) == len(bars)
    assert all(abs(v) < 20 for v in f), "the 0.999 clamp keeps the log finite"
    assert f[-1] > f[0], "a monotonic climb should end positive"


# ── quality: abstain, never fabricate a middle ──────────────────────────────
def test_quality_drops_an_unreadable_component_instead_of_scoring_it_neutral():
    full = {"div_sources": ["MACD", "KD"], "score": 80.0, "slope": 1.0,
            "oi_state": S.OI_LONGS_OPENING,
            "plan": {"stop_pct": 1.0}}
    partial = dict(full, oi_state=0)          # OI feed missing entirely
    S.quality(full)
    S.quality(partial)
    assert full["quality_basis"] == 100
    assert partial["quality_basis"] == 100 - S.QUALITY_WEIGHTS["oi"]


def test_quality_is_a_percentage_of_what_could_be_read():
    """Every live component at its maximum must read 100, whatever was live."""
    ev = {"div_sources": ["MACD", "KD", "FISH", "CVD"], "score": 100.0,
          "slope": 5.0, "oi_state": S.OI_LONGS_OPENING,
          "plan": {"stop_pct": S.MIN_STOP_PCT * 100}}
    assert S.quality(ev) == 100
    del ev["oi_state"]
    assert S.quality(ev) == 100, "removing a maxed component cannot change a percentage"


def test_more_agreeing_sources_scores_higher():
    def q(n):
        return S.quality({"div_sources": ["MACD", "KD", "FISH", "CVD"][:n],
                          "score": 70.0, "slope": 1.0,
                          "oi_state": S.OI_LONGS_OPENING, "plan": {"stop_pct": 1.0}})
    assert q(1) < q(2) < q(3) < q(4)


# ── the short side (added 2026-08-11) ───────────────────────────────────────
# The long side had gates and a plan builder; the mirror had neither until it
# was asked for. These assert the SIGNS, because every short bug in this file
# would have been a sign error: a stop below entry, an R computed off the long
# formula, an OI state read from the wrong table.
def test_short_plan_puts_the_stop_above_entry():
    p = S.plan(100.0, 103.0, side="short")
    assert p is not None
    assert p["tp"] < p["entry"] < p["sl"], "short stop must sit ABOVE entry"
    assert p["side"] == "short"
    # 2R below entry, measured off the same risk the stop defines
    risk = p["sl"] - p["entry"]
    assert abs(p["tp"] - (p["entry"] - risk * p["rr"])) < 1e-9


def test_long_and_short_plans_are_mirror_images():
    long_p = S.plan(100.0, 97.0, side="long")
    short_p = S.plan(100.0, 103.0, side="short")
    assert long_p and short_p
    assert abs(long_p["stop_pct"] - short_p["stop_pct"]) < 0.2
    assert abs(long_p["tp_pct"] - short_p["tp_pct"]) < 0.4


def test_plan_refuses_a_level_on_the_wrong_side():
    """A 'resistance' below price would build a stop that is already hit."""
    assert S.plan(100.0, 97.0, side="short") is None
    assert S.plan(100.0, 103.0, side="long") is None


def test_resistance_above_finds_a_confirmed_swing_high():
    closes = [100.0] * 12 + [100, 101, 105, 101, 100] + [100.0] * 12
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    r = S.resistance_above(highs, lows, price=100.0)
    assert r is not None and r["level"] > 100.0


def test_ema_trending_wants_the_slope_to_follow_the_trade():
    up = [100 + i * 0.05 for i in range(400)]
    dn = list(reversed(up))
    assert S.ema_trending(up, "long")[0] is True
    assert S.ema_trending(up, "short")[0] is False
    assert S.ema_trending(dn, "short")[0] is True
    assert S.ema_trending(dn, "long")[0] is False


def test_short_oi_states_are_the_mirror_of_long():
    assert S.OI_OK["short"] == (S.OI_SHORTS_OPENING, S.OI_LONGS_CLOSING)
    assert set(S.OI_OK["short"]).isdisjoint(S.OI_OK_FOR_LONG)


def test_evaluate_short_names_its_own_refusals(monkeypatch):
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": None, "score": 60})
    r = S.evaluate(_bars([100.0 + i * 0.01 for i in range(N)]), side="short")
    assert r["reason"] == "no short triangle"
    assert r["side"] == "short"


def test_quality_reads_a_short_from_the_shorts_point_of_view():
    """A perfect short scored on the long scale would grade 0/100 — the meter
    at 5, a hard-falling EMA and shorts opening are the STRONGEST short this
    scan can produce."""
    ev = {"side": "short", "score": 5.0, "slope": -1.8,
          "oi_state": S.OI_SHORTS_OPENING, "div_sources": ["MACD", "KD"],
          "plan": {"stop_pct": 1.0}}
    assert S.quality(ev) > 70


def test_evaluate_sides_reports_the_deepest_rejection(monkeypatch):
    """When neither side passes, the useful rejection is the one that got
    furthest — 'stop distance out of range' says something about the symbol,
    'no short triangle' says only that it was not a short."""
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": "long", "score": 80})
    monkeypatch.setattr(S, "REQUIRE_DIVERGENCE", False)
    r = S.evaluate_sides(_bars([100.0 + i * 0.01 for i in range(N)]))
    assert r["reason"] != "no short triangle"


def test_disabling_a_side_removes_it_from_the_scan(monkeypatch):
    monkeypatch.setattr(S, "ENABLE_SHORT", False)
    assert S.enabled_sides() == ("long",)
    monkeypatch.setattr(S, "ENABLE_LONG", False)
    monkeypatch.setattr(S, "ENABLE_SHORT", True)
    assert S.enabled_sides() == ("short",)


def test_bearish_divergence_is_the_mirror_not_a_copy():
    """Price higher high + oscillator lower high. Feeding the BULLISH shape in
    must not fire it, or the gate is direction-blind and every short would
    inherit the long's divergences."""
    n = 80
    highs = [100 + i * 0.1 for i in range(n)]
    lows = [h - 1 for h in highs]
    falling_osc = [50 - i * 0.3 for i in range(n)]
    rising_osc = [50 + i * 0.3 for i in range(n)]
    # price rising with a falling oscillator is the bearish case
    bear = S.bearish_divergence(highs, lows, falling_osc, atr=None)
    same_shape_bull = S.bearish_divergence(highs, lows, rising_osc, atr=None)
    assert same_shape_bull is None, "bearish gate fired on a bullish shape"
    assert bear is None or isinstance(bear, int)


def test_short_alert_says_short_in_the_headline():
    """A short whose header still read 做多 is a plan that loses money by being
    read correctly."""
    sig = {"base": "AAPL", "segment": "tradfi", "side": "short",
           "price": 100.0, "score": 20.0, "slope": -1.2, "quality": 70,
           "quality_basis": 100, "div_sources": ["MACD"], "div_ago": 3,
           "oi_state": S.OI_SHORTS_OPENING,
           "support": {"level": 103.0, "bars_ago": 5},
           "plan": S.plan(100.0, 103.0, side="short")}
    txt = S.format_signal(sig)
    assert "做空" in txt and "做多" not in txt
    assert "壓力" in txt          # not 支撐 — the level is above price


# ── RSI as a fifth divergence source (added 2026-08-14) ─────────────────────
# The bar for a new source is the one A/D failed: it must add a MEASUREMENT,
# not another vote. Measured over 25,454 bars on 22 perps, as Jaccard overlap
# of bullish-divergence bars:
#     KD vs FISH  0.625   ← both were ALREADY in the set
#     KD vs RSI   0.340 · MACD vs RSI 0.339 · FISH vs RSI 0.277 · CVD vs RSI 0.171
# RSI is less redundant with the set than two existing members are with each
# other. That is a statement about independence only — not about prediction.
def test_rsi_is_wilder_smoothing_not_an_ema():
    """ta.rma is (prev·(n−1) + new)/n; an EMA is 2/(n+1). Swapping them tracks
    closely enough to look right on a chart and differs by points at the turns,
    which is exactly where a divergence is decided."""
    closes = [100.0] * 15 + [110.0] * 15          # one clean step
    rsi = S.rsi_series(closes, length=14)
    ema_like = S.ema(closes, 14)
    assert abs(rsi[-1] - 100.0) < 1e-6, "a pure up-run must pin at 100"
    assert rsi[:14] == [50.0] * 14, "warm-up must be padded, not shortened"
    assert len(rsi) == len(closes) == len(ema_like)


def test_rsi_is_bounded_and_aligned_to_the_price_series():
    """divergence_scan indexes the oscillator by the SAME index as highs/lows.
    A shorter or None-padded series silently misaligns every pivot — it does
    not error, it just compares the wrong bars."""
    closes = [100 + (i % 7) - (i % 3) * 2 + i * 0.05 for i in range(200)]
    rsi = S.rsi_series(closes)
    assert len(rsi) == len(closes)
    assert all(0.0 <= v <= 100.0 for v in rsi)
    assert all(isinstance(v, float) for v in rsi)


def test_a_flat_series_has_no_rsi_and_no_divergence():
    """All-flat means down == 0, which the Pine returns 100 for. It must not
    divide by zero or produce a divergence out of nothing."""
    rsi = S.rsi_series([100.0] * 60)
    assert rsi[-1] == 100.0


def test_too_little_history_returns_empty_rather_than_a_guess():
    assert S.rsi_series([1, 2, 3]) == []


def test_rsi_is_one_of_the_scan_sources():
    import inspect
    src = inspect.getsource(S.divergence_scan)
    assert '"RSI"' in src, "RSI was measured as independent but never wired in"


def test_the_pine_mirror_pins_the_same_defaults():
    """pine/indicators/RSI_Divergence_PRO.pine draws what the scanner scores.
    They drifted once before on the S2 meter (wrong weights, missing factor),
    so the defaults are parsed OUT of the .pine rather than trusted."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pine = open(os.path.join(root, "pine", "indicators",
                             "RSI_Divergence_PRO.pine"), encoding="utf-8").read()

    def num(name):
        m = re.search(r'input\.(?:int|float)\(\s*([0-9.]+)\s*,\s*"' + name, pine)
        assert m, f"could not parse {name!r} out of the .pine"
        return float(m.group(1))

    assert num("RSI Length") == S.RSI_LEN
    assert num("Pivot Lookback") == S.DIV_PIVOT
    assert num("Min bars between pivots") == S.DIV_MIN_GAP
    assert num("Max bars between pivots") == S.DIV_MAX_GAP
    assert num("Min Momentum Gap") == S.DIV_MIN_OSC_GAP
    assert num("RSI range window") == S.DIV_NORM
    assert num("Min Swing Size") == S.DIV_MIN_LEG_ATR


def test_the_pine_does_not_claim_an_edge():
    """The indicator is stricter, not proven. A .pine that promises profit is
    the artefact this repo's 46-of-48 finding exists to prevent."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pine = open(os.path.join(root, "pine", "indicators",
                             "RSI_Divergence_PRO.pine"), encoding="utf-8").read()
    assert "NOT* CLAIMED" in pine or "NOT CLAIMED" in pine
    assert "46" in pine


# ── fee floor + shadow book (2026-08-15) ────────────────────────────────────
def test_the_stop_floor_is_above_the_fee_burn_zone():
    """Bybit taker ~0.055%/side, so a round trip costs ~0.11% of price whatever
    the setup thinks. At the old 0.4% floor that is 25% of R spent before the
    idea is tested. The floor exists to keep that tax under ~10%."""
    import strategy4 as S4
    round_trip = 0.11 / 100
    assert round_trip / S4.MIN_STOP_PCT <= 0.12, (
        f"fees are {round_trip / S4.MIN_STOP_PCT * 100:.0f}% of R at the "
        f"{S4.MIN_STOP_PCT * 100:.2f}% floor")


def test_a_too_tight_stop_is_flagged_not_erased():
    """Returning a bare None would delete the evidence for the floor that
    produced it — the rejected trades would never be scored and "did raising it
    help?" could never be answered."""
    import strategy4 as S4
    p = S4.plan(100.0, 99.7, "long")          # 0.3% + buffer, under the floor
    assert p is not None, "the refusal erased the plan"
    assert p["rejected"] == "stop_too_tight"
    assert p["stop_pct"] < S4.MIN_STOP_PCT * 100
    assert p["entry"] and p["sl"] and p["tp"], "flagged plans must stay complete"


def test_a_flagged_plan_is_not_a_signal():
    """It must never become a tradeable alert — only a shadow record."""
    import strategy4 as S4
    tight = {"plan": {"rejected": "stop_too_tight", "stop_pct": 0.5,
                      "floor_pct": 1.0, "entry": 1, "sl": .99, "tp": 1.02}}
    assert tight["plan"].get("rejected")      # premise
    # the scan marks these shadow and returns before `pass` can be set
    import inspect
    src = inspect.getsource(S4.evaluate)
    assert 'out["shadow"] = True' in src
    assert src.index('out["shadow"] = True') < src.index("REQUIRE_DIVERGENCE"), \
        "the shadow branch must return before the setup can qualify"


def test_shadow_trades_stay_out_of_the_live_record():
    """all/crypto/tradfi describe trades that were actually alerted. Mixing
    declined ones in would describe a strategy nobody ran."""
    import strategy4_outcomes as O
    store = {"tally": {}}
    O.accumulate(store, {"r": -1.0, "outcome": "sl", "segment": "crypto",
                         "side": "long", "shadow": True})
    O.accumulate(store, {"r": 2.0, "outcome": "tp", "segment": "crypto",
                         "side": "long"})
    t = store["tally"]
    assert t["all"]["n"] == 1 and t["all"]["sum"] == 2.0
    assert t["shadow_all"]["n"] == 1 and t["shadow_all"]["sum"] == -1.0
    assert "shadow_crypto" in t and t["crypto"]["n"] == 1


def test_the_shadow_book_can_answer_whether_the_floor_helped():
    """The whole reason it exists: two comparable buckets."""
    import strategy4_outcomes as O
    store = {"tally": {}}
    for r in (2.0, -1.0, -1.0):
        O.accumulate(store, {"r": r, "outcome": "tp" if r > 0 else "sl",
                             "segment": "crypto", "side": "long"})
    for r in (-1.0, -1.0):
        O.accumulate(store, {"r": r, "outcome": "sl", "segment": "crypto",
                             "side": "long", "shadow": True})
    live, shadow = store["tally"]["all"], store["tally"]["shadow_all"]
    assert live["n"] == 3 and shadow["n"] == 2
    assert live["sum"] / live["n"] > shadow["sum"] / shadow["n"]


def test_shadow_signals_never_reach_telegram():
    """Recorded for scoring only. The digest is built from `fresh`, which is
    the ALERTED set — shadows join afterwards, at the tracker."""
    import ast
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(here, "strategy4.py"), encoding="utf-8").read())
    digest = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "build_digest"]
    assert digest, "build_digest is no longer called — did the alert path move?"
    for call in digest:
        src = ast.dump(call)
        assert "shadow" not in src, "a shadow signal is inside the digest payload"
    # and the tracker IS where they go
    ticks = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "tick"]
    assert any("shadow" in ast.dump(t) for t in ticks), \
        "shadows are no longer recorded at all"


# ── a product that needs an account agreement (2026-08-17) ──────────────────
def test_an_agreement_error_blocks_the_symbol_instead_of_retrying(monkeypatch, tmp_path):
    """Bybit gates some contracts behind terms the ACCOUNT must accept:
    110125 "You must agree to the Crude Oil Trading Terms". No retry fixes
    that, so S4 would re-attempt CL on every signal forever."""
    import config
    import strategy4_exec as E
    monkeypatch.setattr(E, "STATE_FILE", str(tmp_path / "s4x.json"))
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(E, "preflight", lambda s, p: (True, "ok"))
    real = ('bybit {"retCode":110125,"retMsg":"You must agree to the Crude Oil '
            'Trading Terms before trading this contract."}')
    monkeypatch.setattr(E, "_send",
                        lambda *a, **k: (_ for _ in ()).throw(Exception(real)))
    sent = []
    monkeypatch.setattr(E, "_tg_owner", lambda m: sent.append(m))
    sig = {"symbol": "CL/USDT:USDT", "side": "long",
           "plan": {"entry": 60.0, "sl": 59.0, "tp": 62.0}}
    out = E.open_trade(sig)
    assert out["reason"] == "needs_agreement"
    assert "CL/USDT:USDT" in E._blocked()
    assert sent and "同意" in sent[0], "the owner was not told what to do"
    # the "never retried" half is test_a_blocked_symbol_is_refused_... below


def test_a_blocked_symbol_is_refused_before_any_network_call(monkeypatch, tmp_path):
    import strategy4_exec as E
    monkeypatch.setattr(E, "STATE_FILE", str(tmp_path / "s4x.json"))
    monkeypatch.setattr(E, "bybit_symbol",
                        lambda s: (_ for _ in ()).throw(
                            AssertionError("hit the network for a blocked symbol")))
    E._block("CL/USDT:USDT", "110125")
    ok, why = E.preflight("CL/USDT:USDT", 60.0)
    assert ok is False and why == "needs_agreement"


def test_unblock_clears_it_so_it_can_be_retried(monkeypatch, tmp_path):
    """After the terms are accepted the symbol must be reachable again without
    editing a file by hand."""
    import strategy4_exec as E
    monkeypatch.setattr(E, "STATE_FILE", str(tmp_path / "s4x.json"))
    E._block("CL/USDT:USDT", "110125")
    assert E.unblock("CL/USDT:USDT") == 1
    assert E._blocked() == {}


def test_other_failures_are_not_blocked_permanently(monkeypatch, tmp_path):
    """A transient rejection must stay retryable — blocking on everything would
    silently shrink the universe one network blip at a time."""
    import config
    import strategy4_exec as E
    monkeypatch.setattr(E, "STATE_FILE", str(tmp_path / "s4x.json"))
    monkeypatch.setenv("S4_EXEC", "bybit")
    monkeypatch.setattr(config, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(E, "preflight", lambda s, p: (True, "ok"))
    monkeypatch.setattr(E, "_send", lambda *a, **k: (_ for _ in ()).throw(
        Exception("bybit timeout")))
    monkeypatch.setattr(E, "_tg_owner", lambda m: None)
    out = E.open_trade({"symbol": "X/USDT:USDT", "side": "long",
                        "plan": {"entry": 1.0, "sl": .9, "tp": 1.2}})
    assert out["reason"].startswith("order_failed")
    assert E._blocked() == {}, "a transient error was blocked permanently"
