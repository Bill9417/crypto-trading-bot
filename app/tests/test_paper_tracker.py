"""paper_tracker — forward, no-money paper-tracking of S1 (full + longs-only).

No network: every test drives the pure state-machine pieces (_fill_pending,
_manage_open, tick()'s orchestration) with synthetic candles and a
monkeypatched _fetch_universe/BT.evaluate. conftest's autouse guard already
redirects STATE_FILE and stubs out real fetches for anything that forgets.
"""
import paper_tracker as PT
import backtest as BT

HOUR = 3600000


def _candle(ts, o, h, l_, c, v=100.0):
    return [ts, o, h, l_, c, v]


def _with_forming(bars):
    """`bars` plus a trailing still-forming candle, the way ccxt actually
    returns a live feed. closed_bars() drops it, so the tracker sees exactly
    `bars` — supplying only `bars` would leave it one short of BT.WINDOW."""
    nxt = (bars[-1][0] + HOUR) if bars else 0
    return bars + [_candle(nxt, 100, 100, 100, 100, 1.0)]


def _pending(entry=100.0, sl=95.0, tp1=110.0, tp2=120.0, dir_="LONG", last_ts=0):
    return {"symbol": "FAKE", "dir": dir_, "lights": 6, "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "last_ts": last_ts, "wait_bars": 0,
            "bars": 0, "partial": False}


# ── _fill_pending ────────────────────────────────────────────────────────────
def test_fill_pending_fills_when_price_touches_entry_long():
    pos = _pending(entry=100.0, last_ts=0)
    candles = [_candle(HOUR, 105, 106, 102, 104), _candle(2 * HOUR, 104, 104, 99, 100)]
    assert PT._fill_pending(pos, candles) == "filled"
    assert pos["opened_ts"] == 2 * HOUR


def test_fill_pending_expires_after_max_wait():
    pos = _pending(entry=50.0, last_ts=0)     # price never comes down to 50
    candles = [_candle(i * HOUR, 100, 101, 99, 100) for i in range(1, PT.MAX_WAIT_BARS + 2)]
    assert PT._fill_pending(pos, candles) == "expired"


def test_fill_pending_expires_if_target_prints_before_fill():
    """CANCEL_QUEUE_IF_TARGET_HIT equivalent — mirrors backtest.simulate_trade."""
    pos = _pending(entry=100.0, tp1=110.0, last_ts=0)
    candles = [_candle(HOUR, 105, 112, 104, 108)]     # tp1 (110) printed, entry (100) never touched
    assert PT._fill_pending(pos, candles) == "expired"


def test_fill_pending_still_waiting_returns_none():
    pos = _pending(entry=50.0, last_ts=0)
    candles = [_candle(HOUR, 100, 101, 99, 100)]      # one bar, no fill yet, under the wait cap
    assert PT._fill_pending(pos, candles) is None


# ── _manage_open ─────────────────────────────────────────────────────────────
def _open_pos(entry=100.0, sl=95.0, tp1=110.0, tp2=120.0, dir_="LONG", last_ts=0):
    p = _pending(entry, sl, tp1, tp2, dir_, last_ts)
    p["opened_ts"] = last_ts
    return p


def test_manage_open_sl_hit_closes_as_loss():
    pos = _open_pos(last_ts=0)
    candles = [_candle(HOUR, 100, 101, 94, 95)]       # dips through SL (95)
    closed = PT._manage_open(pos, candles)
    assert closed is not None
    assert closed["reason"] == "sl" and closed["win"] is False


def test_manage_open_tp1_then_breakeven_books_half():
    pos = _open_pos(entry=100.0, tp1=110.0, last_ts=0)
    candles = [
        _candle(HOUR, 100, 111, 100, 110),            # TP1 hit -> partial
        _candle(2 * HOUR, 110, 111, 99, 100),          # back to entry -> breakeven exit
    ]
    closed = PT._manage_open(pos, candles)
    assert closed is not None and closed["reason"] == "breakeven"
    # tp1_pct = 10%, half of that = 5% gross, minus costs
    assert closed["pnl"] > 0


def test_manage_open_tp1_then_tp2_books_full_runner():
    pos = _open_pos(entry=100.0, tp1=110.0, tp2=120.0, last_ts=0)
    candles = [
        _candle(HOUR, 100, 111, 100, 110),
        _candle(2 * HOUR, 110, 121, 110, 120),         # TP2 hit
    ]
    closed = PT._manage_open(pos, candles)
    assert closed is not None and closed["reason"] == "tp2"
    assert closed["win"] is True


def test_manage_open_time_cap_force_closes_never_partial():
    pos = _open_pos(entry=100.0, sl=50.0, tp1=200.0, last_ts=0)   # never hits either
    candles = [_candle(i * HOUR, 100, 101, 99, 103) for i in range(1, BT.MAX_HOLD_BARS + 2)]
    closed = PT._manage_open(pos, candles)
    assert closed is not None and closed["reason"] == "time"
    # THE POINT: a stuck trade gets a real recorded outcome here, unlike
    # backtest.simulate_trade which drops it entirely (see module docstring)
    assert "win" in closed


def test_manage_open_short_direction_mirrors_long():
    pos = _open_pos(entry=100.0, sl=105.0, tp1=90.0, tp2=80.0, dir_="SHORT", last_ts=0)
    candles = [_candle(HOUR, 100, 106, 99, 105)]      # rallies through SL (105)
    closed = PT._manage_open(pos, candles)
    assert closed["reason"] == "sl" and closed["win"] is False


# ── tick() orchestration ─────────────────────────────────────────────────────
def _stub_universe(monkeypatch, oh1h_by_symbol, btc_reg=None):
    def fake_fetch(progress=lambda *a: None):
        data = {sym: {"oh1h": oh, "ema4h": []} for sym, oh in oh1h_by_symbol.items()}
        return data, (btc_reg or [])
    monkeypatch.setattr(PT, "_fetch_universe", fake_fetch)


def test_tick_opens_a_pending_position_on_a_fresh_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    oh1h = _with_forming([_candle(i * HOUR, 100, 100, 100, 100)
                          for i in range(BT.WINDOW)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": oh1h})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    assert PT.tick(force=True) is True
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["pending"]
    assert "FAKE/USDT:USDT" in state["variants"]["s1_longs_only"]["pending"]


def test_tick_longs_only_variant_skips_short_signals(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    oh1h = _with_forming([_candle(i * HOUR, 100, 100, 100, 100)
                          for i in range(BT.WINDOW)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": oh1h})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (False, 100.0, 105.0, 90.0, 80.0, 7))

    PT.tick(force=True)
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["pending"]
    assert "FAKE/USDT:USDT" not in state["variants"]["s1_longs_only"].get("pending", {})


# ── the low-volatility variant (s1_regime_lab's surviving hypothesis) ────────
def _quiet_candles(n):
    """Tight ranges → ATR well under 1% of price."""
    return [_candle(i * HOUR, 100.0, 100.2, 99.8, 100.0) for i in range(n)]


def _wild_candles(n):
    """Wide ranges → ATR far above 1% of price."""
    return [_candle(i * HOUR, 100.0, 105.0, 95.0, 100.0) for i in range(n)]


def test_wants_lowvol_accepts_quiet_and_rejects_volatile():
    quiet, wild = _quiet_candles(60), _wild_candles(60)
    assert PT._wants("s1_lowvol", True, quiet) is True
    assert PT._wants("s1_lowvol", True, wild) is False
    # the other variants ignore volatility entirely
    assert PT._wants("s1_full", True, wild) is True
    assert PT._wants("s1_longs_only", True, wild) is True


def test_wants_lowvol_is_direction_agnostic():
    """THE POINT of this variant vs longs_only: the regime lab found both
    directions turn positive once volatility is controlled, so this gate
    must not quietly become a second longs-only filter."""
    quiet = _quiet_candles(60)
    assert PT._wants("s1_lowvol", True, quiet) is True
    assert PT._wants("s1_lowvol", False, quiet) is True


def test_wants_lowvol_abstains_on_unusable_history():
    """Too few bars to compute ATR → skip, never default to 'take it'."""
    assert PT._wants("s1_lowvol", True, _quiet_candles(3)) is False


def test_tick_lowvol_variant_skips_a_volatile_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    wild = _with_forming(_wild_candles(BT.WINDOW))
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": wild})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    PT.tick(force=True)
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["pending"]
    assert "FAKE/USDT:USDT" not in state["variants"]["s1_lowvol"].get("pending", {})


# ── the single-target exit (s1_lowvol_3r) ───────────────────────────────────
def _single_pos(entry=100.0, sl=95.0, tp2=115.0, dir_="LONG", last_ts=0):
    p = _pending(entry, sl, 110.0, tp2, dir_, last_ts)
    p["exit"] = "single"
    p["opened_ts"] = last_ts
    return p


def test_single_exit_ignores_tp1_and_rides_to_the_wide_target():
    """THE POINT of this variant: passing through the old TP1 must NOT book
    half or arm a breakeven stop — the whole position rides to the target."""
    pos = _single_pos(entry=100.0, sl=95.0, tp2=115.0)
    candles = [
        _candle(HOUR, 100, 111, 100, 110),        # sails through the old TP1 (110)
        _candle(2 * HOUR, 110, 109, 99, 100),      # back to entry — old rule exits here
    ]
    assert PT._manage_open(pos, candles) is None   # still open, no breakeven exit
    assert pos["partial"] is False

    pos2 = _single_pos(entry=100.0, sl=95.0, tp2=115.0)
    hit = [_candle(HOUR, 100, 116, 100, 115)]
    closed = PT._manage_open(pos2, hit)
    assert closed["reason"] == "target" and closed["win"] is True


def test_single_exit_takes_the_full_loss_at_the_stop():
    pos = _single_pos(entry=100.0, sl=95.0, tp2=115.0)
    closed = PT._manage_open(pos, [_candle(HOUR, 100, 101, 94, 95)])
    assert closed["reason"] == "sl" and closed["win"] is False


def test_single_exit_stop_wins_a_bar_that_spans_both():
    """Conservative fill on an ambiguous bar — same assumption the historical
    simulator makes, so forward and backtest numbers stay comparable."""
    pos = _single_pos(entry=100.0, sl=95.0, tp2=115.0)
    closed = PT._manage_open(pos, [_candle(HOUR, 100, 116, 94, 100)])
    assert closed["reason"] == "sl"


def test_tick_sets_a_3r_target_for_the_lowvol_3r_variant(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    quiet = _with_forming(_quiet_candles(BT.WINDOW))
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": quiet})
    # entry 100, sl 95 -> risk 5 -> a 3R target sits at 115
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    PT.tick(force=True)
    state = PT._load()
    plain = state["variants"]["s1_lowvol"]["pending"]["FAKE/USDT:USDT"]
    wide = state["variants"]["s1_lowvol_3r"]["pending"]["FAKE/USDT:USDT"]
    assert plain["exit"] == "bracket" and plain["tp2"] == 120.0   # untouched
    assert wide["exit"] == "single"
    assert wide["tp2"] == 100.0 + 5.0 * PT.SINGLE_TARGET_R        # 115.0
    assert wide["sl"] == 95.0                                      # stop unchanged


def test_tick_lowvol_variant_takes_a_quiet_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    quiet = _with_forming(_quiet_candles(BT.WINDOW))
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": quiet})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    PT.tick(force=True)
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_lowvol"]["pending"]


def test_tick_respects_the_throttle_unless_forced(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    monkeypatch.setattr(PT, "_last_tick", 9e18)   # "just ticked, far in the future"
    called = {"n": 0}

    def fake_fetch(progress=lambda *a: None):
        called["n"] += 1
        return {}, []
    monkeypatch.setattr(PT, "_fetch_universe", fake_fetch)

    assert PT.tick() is False
    assert called["n"] == 0


def test_full_lifecycle_pending_to_open_to_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    base = _with_forming([_candle(i * HOUR, 100, 100, 100, 100)
                          for i in range(BT.WINDOW)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": base})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))
    PT.tick(force=True)                                       # opens pending

    fill_candle = _with_forming([_candle((BT.WINDOW + 1) * HOUR, 100, 101, 99, 100)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": fill_candle})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: None)
    PT.tick(force=True)                                       # fills

    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["open"]

    sl_candle = _with_forming([_candle((BT.WINDOW + 3) * HOUR, 100, 100, 94, 95)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": sl_candle})
    PT.tick(force=True)                                       # stops out

    state = PT._load()
    assert "FAKE/USDT:USDT" not in state["variants"]["s1_full"]["open"]
    closed = state["variants"]["s1_full"]["closed"]
    assert len(closed) == 1 and closed[0]["reason"] == "sl"


# ── report_tg ────────────────────────────────────────────────────────────────
def test_report_tg_handles_no_trades_yet(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    text = PT.report_tg()
    assert "S1 (full, as live)" in text and "S1 longs-only" in text
    assert "no closed trades yet" in text


# ── the forming-candle bug (found 2026-08-01, five silent days) ──────────────
# ccxt returns the in-progress bar last. Everything here consumed it as final,
# so evaluate() judged a PARTIAL bar (its volume is a fraction of a full hour's,
# which fails S1's volume gate almost by construction) and _fill_pending /
# _manage_open stamped last_ts from it, hiding the rest of that hour forever.
# Result: 0 signals and 0 trades across all four variants for five days, with
# no log line to say whether that was a quiet market or a broken tracker.
def test_closed_bars_drops_the_forming_candle():
    bars = [_candle(i * HOUR, 100, 100, 100, 100) for i in range(3)]
    assert PT.closed_bars(bars) == bars[:-1]
    assert PT.closed_bars([]) == []
    assert PT.closed_bars([bars[0]]) == []


def test_evaluate_never_sees_the_forming_bar(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    closed = [_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)]
    forming = _candle(BT.WINDOW * HOUR, 100, 100, 100, 100, 1.0)   # 1% of a bar's volume
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": closed + [forming]})

    seen = {}
    def _eval(oh, ema, reg):
        seen["last_ts"] = oh[-1][0]
        seen["len"] = len(oh)
        return None
    monkeypatch.setattr(BT, "evaluate", _eval)
    PT.tick(force=True)
    assert seen["last_ts"] == closed[-1][0]        # the last CLOSED bar
    assert seen["len"] == BT.WINDOW


def test_a_partial_hour_is_re_read_once_it_closes():
    """The second half of the bug: last_ts must not advance past a bar whose
    high/low can still move, or the fill that prints later in that hour is
    never seen."""
    pos = _pending(entry=100.0, last_ts=0)
    partial = _candle(HOUR, 105, 106, 102, 104)          # hasn't reached entry yet
    assert PT._fill_pending(pos, PT.closed_bars([partial])) is None
    assert pos["last_ts"] == 0                            # nothing consumed
    final = _candle(HOUR, 105, 106, 99, 100)              # same hour, now closed
    assert PT._fill_pending(pos, PT.closed_bars([final, _candle(2 * HOUR, 100, 100, 100, 100)])) == "filled"


def test_a_failed_universe_fetch_retries_instead_of_burning_the_hour(monkeypatch, tmp_path):
    """_last_tick used to be stamped BEFORE the fetch, so a broken fetch looked
    exactly like a healthy tick and cost a full hour each time."""
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    monkeypatch.setattr(PT, "_last_tick", 0.0)
    monkeypatch.setattr(PT, "_fetch_universe", lambda progress=lambda *a: None: ({}, []))
    assert PT.tick() is False
    assert PT._last_tick == 0.0                    # not consumed

    oh1h = _with_forming([_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": oh1h})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: None)
    assert PT.tick() is True                       # next sweep proceeds
    assert PT._last_tick > 0.0


def test_every_tick_reports_what_it_did(monkeypatch, tmp_path, capsys):
    """0 trades has to be evidence, not ambiguity."""
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    oh1h = _with_forming([_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)])
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": oh1h})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: None)
    PT.tick(force=True)
    out = capsys.readouterr().out
    assert "universe=1" in out and "evaluated=4" in out    # 1 symbol x 4 variants
    assert "signals=0" in out and "new=0" in out


# ── exit shape: the asymmetry these variants exist to attack ─────────────────
def test_exit_shape_reports_best_worst_and_payoff(monkeypatch, tmp_path):
    """S1's live bracket has never returned more than 1.44R in 127 trades while
    a loser costs ~1.05R. Win rate and expectancy both hide that; best/worst
    and the payoff ratio are what actually answer it."""
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    PT._save({"variants": {v: {"open": {}, "closed": []} for v in PT.VARIANTS}
              | {"s1_full": {"open": {}, "closed": [
                  {"rr": 1.4}, {"rr": 1.2}, {"rr": -1.0}, {"rr": -1.0}]}}})
    sh = PT.exit_shape("s1_full")
    assert sh["n"] == 4
    assert sh["best"] == 1.4 and sh["worst"] == -1.0
    assert sh["avg_win"] == 1.3 and sh["avg_loss"] == -1.0
    assert sh["payoff"] == 1.3


def test_exit_shape_is_empty_before_any_trade(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    sh = PT.exit_shape("s1_full")
    assert sh["n"] == 0 and sh["best"] is None and sh["payoff"] is None


def test_exit_shape_survives_an_all_wins_book(monkeypatch, tmp_path):
    """No losses yet ⇒ no payoff ratio, rather than a divide-by-zero."""
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    PT._save({"variants": {"s1_full": {"open": {}, "closed": [{"rr": 2.0}]}}})
    sh = PT.exit_shape("s1_full")
    assert sh["payoff"] is None and sh["avg_loss"] is None


def test_report_carries_the_noise_caveat(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    rep = PT.report_tg()
    assert "紙上模擬" in rep and "0.07R" in rep
