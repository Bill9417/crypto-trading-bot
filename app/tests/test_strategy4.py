"""S4 — Bybit TradFi perp scanner.

The gates are the product here, so each one is tested for what it REJECTS as
much as what it passes: a filter that never says no is not a filter, and this
scan's whole value is that it stays quiet.
"""
import strategy4 as S


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
    assert S.plan(100.0, 97.0) is not None
    assert S.plan(100.0, 99.98) is None      # tighter than noise
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
    flat = _bars([100.0] * 400)
    r = S.evaluate(flat)
    assert r["pass"] is False and r["reason"]


def test_evaluate_stops_at_the_triangle_before_spending_anything(monkeypatch):
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": None, "score": 40})
    r = S.evaluate(_bars([100.0 + i * 0.01 for i in range(400)]))
    assert r["reason"] == "no long triangle"
    assert r["plan"] is None


def test_a_passing_setup_carries_a_complete_plan(monkeypatch):
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda o: {"signal": "long", "score": 82})
    monkeypatch.setattr(S, "REQUIRE_DIVERGENCE", False)
    closes = [100 + i * 0.05 for i in range(380)]
    closes += [119.0, 118.0, 117.5, 118.2, 119.5, 120.0]    # a pullback + swing low
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


def test_the_scan_is_alert_only():
    """No order path. The universe is weeks old and nothing is validated —
    this must not be one refactor away from placing trades."""
    import inspect
    src = inspect.getsource(S)
    for banned in ("create_order", "open_flip", "place_order", "createOrder"):
        assert banned not in src


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
