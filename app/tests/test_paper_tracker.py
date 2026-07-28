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
    oh1h = [_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)]
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": oh1h})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    assert PT.tick(force=True) is True
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["pending"]
    assert "FAKE/USDT:USDT" in state["variants"]["s1_longs_only"]["pending"]


def test_tick_longs_only_variant_skips_short_signals(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    oh1h = [_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)]
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
    wild = _wild_candles(BT.WINDOW)
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": wild})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))

    PT.tick(force=True)
    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["pending"]
    assert "FAKE/USDT:USDT" not in state["variants"]["s1_lowvol"].get("pending", {})


def test_tick_lowvol_variant_takes_a_quiet_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr(PT, "STATE_FILE", str(tmp_path / "paper.json"))
    quiet = _quiet_candles(BT.WINDOW)
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
    base = [_candle(i * HOUR, 100, 100, 100, 100) for i in range(BT.WINDOW)]
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": base})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: (True, 100.0, 95.0, 110.0, 120.0, 7))
    PT.tick(force=True)                                       # opens pending

    fill_candle = [_candle((BT.WINDOW) * HOUR, 100, 101, 99, 100)]
    _stub_universe(monkeypatch, {"FAKE/USDT:USDT": fill_candle})
    monkeypatch.setattr(BT, "evaluate", lambda oh, ema, reg: None)
    PT.tick(force=True)                                       # fills

    state = PT._load()
    assert "FAKE/USDT:USDT" in state["variants"]["s1_full"]["open"]

    sl_candle = [_candle((BT.WINDOW + 1) * HOUR, 100, 100, 94, 95)]
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
