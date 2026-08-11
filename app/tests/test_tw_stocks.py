"""tw_stocks — pure signal/regime/digest/scheduling logic."""
import os
from datetime import datetime

import tw_stocks


def _rows_from_closes(closes):
    """Flat bars (o=h=l=c) — enough for regime(), which only reads closes."""
    return [(86400 * i, c, c, c, c, 1000) for i, c in enumerate(closes)]


def _uptrend_with_pullback(n=80):
    """Rising closes 100,101,… whose LAST bar dips to the 20-day SMA and
    closes back above its open — the exact setup pattern."""
    rows = []
    for i in range(n - 1):
        c = 100.0 + i
        rows.append((86400 * i, c - 0.5, c + 1.0, c - 1.0, c, 1000))
    c = 100.0 + (n - 1)                      # 179
    rows.append((86400 * (n - 1), c - 0.5, c + 1.0, c - 10.0, c, 1000))
    return rows


# ── regime ───────────────────────────────────────────────────────────────────
def test_regime_bull():
    reg = tw_stocks.regime(_rows_from_closes([100.0 + i for i in range(130)]))
    assert reg["ok"] is True
    assert reg["close"] > reg["sma100"]
    assert reg["mom20"] > 0


def test_regime_bear_below_sma100():
    reg = tw_stocks.regime(_rows_from_closes([230.0 - i for i in range(130)]))
    assert reg["ok"] is False


def test_regime_needs_positive_momentum_too():
    # above the 100-day SMA but drifting DOWN for the last 21 sessions
    closes = [100.0 + i for i in range(109)] + [208.0 - 0.5 * i for i in range(1, 22)]
    reg = tw_stocks.regime(_rows_from_closes(closes))
    assert reg["close"] > reg["sma100"]
    assert reg["mom20"] < 0
    assert reg["ok"] is False


def test_regime_insufficient_data():
    assert tw_stocks.regime(_rows_from_closes([100.0] * 50))["ok"] is False


# ── setup ────────────────────────────────────────────────────────────────────
def test_setup_fires_on_pullback_and_levels_match():
    rows = _uptrend_with_pullback()
    s = tw_stocks.setup(rows)
    assert s is not None
    ref, atr = s["ref"], s["atr"]
    assert ref == rows[-1][4]
    assert abs(s["sl"] - (ref - tw_stocks.SL_ATR * atr)) < 1e-9
    assert abs(s["tp"] - (ref + tw_stocks.TP_ATR * atr)) < 1e-9
    assert s["sl"] < ref < s["tp"]


def test_setup_rejects_downtrend():
    rows = []
    for i in range(80):
        c = 200.0 - i
        rows.append((86400 * i, c + 0.5, c + 1.0, c - 10.0, c, 1000))
    assert tw_stocks.setup(rows) is None


def test_setup_rejects_no_pullback():
    # uptrend but the last low never reaches the 20-day SMA
    rows = _uptrend_with_pullback()
    t, o, h, l, c, v = rows[-1]
    rows[-1] = (t, o, h, c - 1.0, c, v)
    assert tw_stocks.setup(rows) is None


def test_setup_rejects_red_close():
    # tags the SMA20 but closes below the open (sellers won the bar)
    rows = _uptrend_with_pullback()
    t, o, h, l, c, v = rows[-1]
    rows[-1] = (t, c + 0.5, h, l, c, v)
    assert tw_stocks.setup(rows) is None


def test_setup_insufficient_data():
    assert tw_stocks.setup(_uptrend_with_pullback(50)) is None


def test_setup_tags_kind_pullback():
    assert tw_stocks.setup(_uptrend_with_pullback())["kind"] == "pullback"


# ── breakout (momentum) ───────────────────────────────────────────────────────
def _rising_to_new_high(n=90):
    """Steadily rising bars where the LAST close is the highest high of the
    window — a fresh 60-session high (the breakout trigger)."""
    rows = []
    for i in range(n):
        c = 100.0 + i                      # monotonic up ⇒ each close a new high
        rows.append((86400 * i, c - 0.5, c + 0.4, c - 0.6, c, 1000))
    return rows


def test_setup_breakout_fires_on_fresh_high():
    s = tw_stocks.setup_breakout(_rising_to_new_high())
    assert s is not None and s["kind"] == "breakout"
    ref, atr = s["ref"], s["atr"]
    assert abs(s["sl"] - (ref - tw_stocks.BO_SL_ATR * atr)) < 1e-9
    assert abs(s["tp"] - (ref + tw_stocks.BO_TP_ATR * atr)) < 1e-9
    assert s["tp"] - ref > ref - s["sl"]           # 6×ATR target wider than 3×ATR stop


def test_setup_breakout_rejects_below_high():
    rows = _rising_to_new_high()
    t, o, h, l, c, v = rows[-1]
    # today closes well BELOW the recent high → no breakout
    rows[-1] = (t, o, h, l, c - 20.0, v)
    assert tw_stocks.setup_breakout(rows) is None


def test_setup_breakout_insufficient_data():
    assert tw_stocks.setup_breakout(_rising_to_new_high(40)) is None


def _bars_from(date_str, closes):
    """Daily bars starting at date_str (Taipei), h=c+1, l=c-1."""
    d0 = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tw_stocks.TZ)
    return [(int((d0.timestamp()) + 86400 * i), c, c + 1.0, c - 1.0, c, 1000)
            for i, c in enumerate(closes)]


def test_update_trail_ratchets_up_with_new_highs():
    rows = _bars_from("2026-01-01", [100.0 + i for i in range(40)])   # steady climb
    rec = {"code": "X", "date": "2026-01-01", "ref": 100.0, "sl": 90.0}
    assert tw_stocks.update_trail(rec, rows) is True
    first_trail, first_peak = rec["trail"], rec["peak"]
    assert first_peak == max(r[2] for r in rows)
    assert first_trail == first_peak - tw_stocks.TRAIL_ATR * tw_stocks._atr14(rows)

    # price climbs further → trail must move UP
    rows2 = rows + _bars_from("2026-02-10", [140.0 + i for i in range(5)])
    tw_stocks.update_trail(rec, rows2)
    assert rec["peak"] > first_peak and rec["trail"] > first_trail


def test_update_trail_never_moves_down():
    rows = _bars_from("2026-01-01", [100.0 + i for i in range(40)])
    rec = {"code": "X", "date": "2026-01-01", "ref": 100.0, "sl": 90.0}
    tw_stocks.update_trail(rec, rows)
    high_water = rec["trail"]
    # now the stock falls back hard — a trailing stop must NOT follow it down
    rows2 = rows + _bars_from("2026-02-10", [110.0, 105.0, 100.0])
    tw_stocks.update_trail(rec, rows2)
    assert rec["trail"] == high_water


def test_update_trail_never_below_original_stop():
    rows = _bars_from("2026-01-01", [100.0] * 30)      # flat → raw trail is low
    rec = {"code": "X", "date": "2026-01-01", "ref": 100.0, "sl": 96.0}
    tw_stocks.update_trail(rec, rows)
    assert rec["trail"] >= 96.0


def test_update_trail_failsoft_on_bad_input():
    rows = _bars_from("2026-01-01", [100.0 + i for i in range(40)])
    assert tw_stocks.update_trail({"date": "not-a-date"}, rows) is False
    assert tw_stocks.update_trail({"date": "2026-01-01"}, []) is False
    # entry date AFTER the last bar → nothing to measure
    assert tw_stocks.update_trail({"date": "2030-01-01", "sl": 1}, rows) is False


def test_web_view_exposes_ratcheted_trail(monkeypatch, tmp_path):
    import stocks_data
    monkeypatch.setattr(tw_stocks, "STATE_FILE", str(tmp_path / "tw.json"))
    monkeypatch.setattr(stocks_data, "tw_quote_map", lambda: {})
    tw_stocks._save_state({
        "last_run_date": "2026-07-24",
        "last_regime": {"ok": True},
        "active_setups": [
            # trail ratcheted ABOVE entry → profit locked
            {"code": "2330", "name": "台積電", "date": "2026-07-10", "ref": 1000.0,
             "sl": 900.0, "tp": 1200.0, "trail": 1080.0, "peak": 1250.0},
            # trail still at/below the original stop → not shown yet
            {"code": "2317", "name": "鴻海", "date": "2026-07-10", "ref": 200.0,
             "sl": 180.0, "tp": 240.0, "trail": 180.0, "peak": 205.0},
        ],
    })
    v = tw_stocks.web_view(now=FRIDAY)
    by = {s["code"]: s for s in v["setups"]}
    assert by["2330"]["trail"] == 1080.0 and by["2330"]["trail_locked"] is True
    assert by["2330"]["trail_s"] == "1,080"
    assert "trail" not in by["2317"]           # hidden until it clears the stop


def test_digest_labels_both_strategies():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    pull = {"ref": 100.0, "sl": 94.0, "tp": 110.0, "atr": 2.0, "turnover": 2.0,
            "kind": "pullback"}
    brk = {"ref": 500.0, "sl": 470.0, "tp": 560.0, "atr": 10.0, "turnover": 9.0,
           "kind": "breakout"}
    msg = tw_stocks.build_digest(_now(), reg, [("2454", "聯發科", brk),
                                               ("2330", "台積電", pull)])
    assert "突破" in msg and "回踩" in msg          # both strategy tags present
    plain = tw_stocks.build_digest_plain(_now(), reg, [("2454", "聯發科", brk)])
    assert "突破" in plain


# ── digest ───────────────────────────────────────────────────────────────────
def _now():
    return datetime(2026, 7, 10, 14, 5, tzinfo=tw_stocks.TZ)   # a Friday


def test_digest_bull_with_setup():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    s = {"ref": 2415.0, "sl": 2255.0, "tp": 2680.0, "atr": 53.0, "turnover": 1e9}
    msg = tw_stocks.build_digest(_now(), reg, [("2330", "台積電", s)])
    assert "✅" in msg and "2330" in msg and "台積電" in msg
    assert "2,415" in msg and "2,255" in msg and "2,680" in msg   # 進/損/標 columns
    assert "<pre>" in msg and "進場=下一交易日開盤參考" in msg
    assert "勝率≠賺錢" in msg          # the honesty footer is not optional


def test_digest_bull_no_setups():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    msg = tw_stocks.build_digest(_now(), reg, [])
    assert "今日無符合條件" in msg


def test_digest_bear_blocks_setups():
    reg = {"ok": False, "close": 26000.0, "sma100": 27000.0, "mom20": -0.02}
    msg = tw_stocks.build_digest(_now(), reg, [], pattern_blocked=3)
    assert "⛔" in msg and "觀望" in msg
    assert "3 檔符合型態" in msg
    assert "進場參考" not in msg


def test_digest_caps_listed_setups():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    s = {"ref": 100.0, "sl": 94.0, "tp": 110.0, "atr": 2.0, "turnover": 1.0}
    many = [(f"{1000 + i}", f"股票{i}", dict(s)) for i in range(14)]
    msg = tw_stocks.build_digest(_now(), reg, many)
    assert f"…另有 {14 - tw_stocks.MAX_SHOW} 檔未列出" in msg


# ── scheduling ───────────────────────────────────────────────────────────────
def test_due_after_close_on_weekday():
    assert tw_stocks._due({}, datetime(2026, 7, 10, 14, 1, tzinfo=tw_stocks.TZ))


def test_not_due_before_send_hour():
    assert not tw_stocks._due({}, datetime(2026, 7, 10, 13, 59, tzinfo=tw_stocks.TZ))


def test_not_due_on_weekend():
    assert not tw_stocks._due({}, datetime(2026, 7, 11, 15, 0, tzinfo=tw_stocks.TZ))


def test_not_due_twice_same_day():
    state = {"last_run_date": "2026-07-10"}
    assert not tw_stocks._due(state, datetime(2026, 7, 10, 15, 0, tzinfo=tw_stocks.TZ))


# ── weekly scorecard ─────────────────────────────────────────────────────────
SUNDAY = datetime(2026, 7, 19, 10, 0, tzinfo=tw_stocks.TZ)   # 2026-07-19 is a Sunday


def _week_setup(code, date, hit=None):
    d = {"code": code, "name": f"股{code}", "date": date,
         "ref": 100.0, "sl": 91.0, "tp": 115.0}
    if hit:
        d["hit"] = hit
    return d


def test_scorecard_due_only_sunday_after_hour():
    assert tw_stocks._scorecard_due({}, SUNDAY)
    assert not tw_stocks._scorecard_due({}, SUNDAY.replace(hour=8))            # too early
    assert not tw_stocks._scorecard_due({}, SUNDAY.replace(day=20))            # Monday


def test_scorecard_due_once_per_iso_week():
    state = {"scorecard_sent_week": "2026-W29"}
    assert not tw_stocks._scorecard_due(state, SUNDAY)         # 07-19 is ISO week 29
    assert tw_stocks._scorecard_due({}, SUNDAY)


def _outcome(code, exit_date, kind, r, source="live"):
    return {"code": code, "name": f"股{code}", "date": "2026-06-01",
            "kind": kind, "exit_date": exit_date, "r": r, "source": source}


def test_weekly_scorecard_tallies_what_resolved_this_week():
    outcomes = [
        _outcome("2330", "2026-07-16", "tp", 1.667),
        _outcome("2317", "2026-07-17", "sl", -1.0),
        _outcome("2454", "2026-07-15", "timeout", 0.4),
        _outcome("2882", "2026-07-06", "tp", 1.667),           # last week — excluded
    ]
    state = {"active_setups": [_week_setup("6505", "2026-07-16")]}
    card = tw_stocks.weekly_scorecard(state, SUNDAY, outcomes=outcomes)
    assert card["closed"] == 3
    assert card["wins"] == 1 and card["losses"] == 1 and card["timeouts"] == 1
    assert abs(card["total_r"] - (1.667 - 1.0 + 0.4)) < 1e-2   # stored to 2dp
    assert card["still_open"] == 1


def test_weekly_scorecard_counts_by_exit_date_not_signal_date():
    """The bug this replaced: the card tallied by SIGNAL date, so a setup that
    fired weeks ago and resolved THIS week was invisible in every scorecard —
    its own signal week had already passed, and by then the row had been pruned
    from active_setups. Trades hold up to MAX_HOLD sessions, so that describes
    almost every trade: the card reported 0/0 forever."""
    old_signal = _outcome("2330", "2026-07-16", "tp", 1.667)
    old_signal["date"] = "2026-05-02"                          # signalled long before
    card = tw_stocks.weekly_scorecard({}, SUNDAY, outcomes=[old_signal])
    assert card["closed"] == 1 and card["wins"] == 1


def test_weekly_scorecard_ignores_replayed_rows():
    """A reconstructed backtest row must never be counted as a signal that was
    actually sent to anyone."""
    rows = [_outcome("2330", "2026-07-16", "tp", 1.667, source="replay")]
    assert tw_stocks.weekly_scorecard({}, SUNDAY, outcomes=rows)["closed"] == 0


def test_weekly_scorecard_empty_week():
    card = tw_stocks.weekly_scorecard({}, SUNDAY, outcomes=[])
    assert card["closed"] == 0 and card["still_open"] == 0
    assert card["iso_week"] == 29


def test_scorecard_plain_empty_week_message():
    card = tw_stocks.weekly_scorecard({}, SUNDAY, outcomes=[])
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "本週沒有訊號結算" in msg


def test_scorecard_plain_shows_tally_and_r():
    card = {"iso_year": 2026, "iso_week": 29, "closed": 3, "wins": 2, "losses": 1,
            "timeouts": 0, "still_open": 0, "total_r": 2.33, "new_signals": 0}
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "達標 2 檔" in msg and "停損 1 檔" in msg and "+2.3R" in msg
    assert "⚠️" in msg


def test_scorecard_plain_negative_r_keeps_minus_sign():
    card = {"iso_year": 2026, "iso_week": 29, "closed": 2, "wins": 0, "losses": 2,
            "timeouts": 0, "still_open": 0, "total_r": -2.0, "new_signals": 0}
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "-2.0R" in msg and "+-2.0R" not in msg


def test_scorecard_plain_appends_running_record_with_small_sample_warning():
    card = {"iso_year": 2026, "iso_week": 29, "closed": 1, "wins": 1, "losses": 0,
            "timeouts": 0, "still_open": 2, "total_r": 1.7, "new_signals": 1}
    rec = {"total": 7, "wins": 4, "losses": 2, "timeouts": 1, "scored": 7,
           "win_pct": 66.7, "total_r": 3.4, "avg_r": 0.486}
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card, rec)
    assert "累計成績" in msg and "共 7 檔" in msg and "+3.4R" in msg
    assert "樣本還太少" in msg                        # n<30 must be caveated
    rec_big = dict(rec, total=44)
    assert "樣本還太少" not in tw_stocks.build_scorecard_plain(SUNDAY, card, rec_big)


# ── outcome ledger ───────────────────────────────────────────────────────────
def _settled(code="2330", kind="tp", **kw):
    rec = {"code": code, "name": "台積電", "date": "2026-07-14", "strategy": "pullback",
           "ref": 1000.0, "sl": 940.0, "tp": 1100.0, "held": 12}
    rec["hit"] = {"kind": kind, "date": "2026-07-20", "time": "—",
                  "price": kw.get("price", rec["tp"] if kind == "tp" else rec["sl"])}
    rec.update({k: v for k, v in kw.items() if k != "price"})
    return rec


def test_outcome_row_scores_r_against_the_setups_own_risk():
    row = tw_stocks.outcome_row(_settled(kind="tp"))
    assert row["kind"] == "tp"
    assert abs(row["r"] - (1100.0 - 1000.0) / (1000.0 - 940.0)) < 1e-3   # stored to 3dp
    assert tw_stocks.outcome_row(_settled(kind="sl"))["r"] == -1.0


def test_outcome_row_scores_a_timeout_at_its_exit_price():
    """A time-out is a real exit at a real price, not a zero. Scoring it as
    'flat' would understate both good and bad time-outs."""
    row = tw_stocks.outcome_row(_settled(kind="timeout", price=1030.0))
    assert abs(row["r"] - 0.5) < 1e-6


def test_outcome_row_leaves_an_unpriced_timeout_unscored():
    """No exit price means we do not know the result. Recording 0.0 would
    assert 'it went nowhere', which is a different and unearned claim."""
    rec = _settled(kind="timeout")
    rec["hit"].pop("price")
    assert tw_stocks.outcome_row(rec)["r"] is None


def test_append_outcome_is_idempotent_per_setup(monkeypatch, tmp_path):
    """The intraday watcher and the daily reconcile can both settle the same
    setup; it must be recorded once."""
    monkeypatch.setattr(tw_stocks, "OUTCOMES_FILE", str(tmp_path / "out.json"))
    assert tw_stocks._append_outcome(_settled()) is True
    assert tw_stocks._append_outcome(_settled()) is False
    assert len(tw_stocks._load_outcomes()) == 1


def test_append_outcome_ignores_unresolved_setups(monkeypatch, tmp_path):
    monkeypatch.setattr(tw_stocks, "OUTCOMES_FILE", str(tmp_path / "out.json"))
    rec = _settled()
    rec.pop("hit")
    assert tw_stocks._append_outcome(rec) is False
    assert tw_stocks._load_outcomes() == []


def test_record_stats_excludes_unscored_rows_from_the_average():
    rows = [{"kind": "tp", "r": 1.667}, {"kind": "sl", "r": -1.0},
            {"kind": "timeout", "r": None}]
    st = tw_stocks.record_stats(rows)
    assert st["total"] == 3 and st["scored"] == 2
    assert st["wins"] == 1 and st["losses"] == 1 and st["timeouts"] == 1
    assert abs(st["total_r"] - 0.667) < 1e-2        # stored to 2dp
    assert abs(st["avg_r"] - 0.334) < 1e-3          # /2 scored, not /3 rows
    assert st["win_pct"] == 50.0                    # tp vs sl only


def test_record_stats_win_pct_is_none_without_decided_trades():
    assert tw_stocks.record_stats([{"kind": "timeout", "r": 0.1}])["win_pct"] is None


def test_all_time_record_separates_live_from_replay(monkeypatch, tmp_path):
    monkeypatch.setattr(tw_stocks, "OUTCOMES_FILE", str(tmp_path / "out.json"))
    tw_stocks._save_outcomes([
        _outcome("2330", "2026-07-16", "tp", 1.667, source="live"),
        _outcome("2317", "2026-07-17", "sl", -1.0, source="replay"),
    ])
    assert tw_stocks.all_time_record(source="live")["total"] == 1
    assert tw_stocks.all_time_record(source="replay")["total"] == 1
    assert tw_stocks.all_time_record()["total"] == 2


def test_ledger_and_state_are_isolated_from_the_real_files():
    """Guard for the guard. The ledger is append-only and permanent, so a test
    row written to the real file is a fake result that lives forever in a track
    record — which is exactly what happened the day mark_hit() learned to
    record outcomes. conftest redirects both paths; if that redirect is ever
    dropped, fail here rather than in production data."""
    import tempfile
    tmp = os.path.realpath(tempfile.gettempdir())
    for path in (tw_stocks.OUTCOMES_FILE, tw_stocks.STATE_FILE):
        assert os.path.realpath(path).startswith(tmp), f"{path} escapes tmp"


def test_ledger_survives_a_corrupt_file(monkeypatch, tmp_path):
    p = tmp_path / "out.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(tw_stocks, "OUTCOMES_FILE", str(p))
    assert tw_stocks._load_outcomes() == []
    assert tw_stocks.all_time_record()["total"] == 0


# ── position sizing ──────────────────────────────────────────────────────────
def test_position_size_risks_the_budget_not_the_spend():
    """The point of the helper: a −24% stop and a −4% stop must risk the same
    money, so the wide one simply buys fewer shares."""
    wide = tw_stocks.position_size(190.0, 144.25, budget=10000)
    tight = tw_stocks.position_size(190.0, 182.0, budget=10000)
    assert wide["shares"] < tight["shares"]
    assert wide["risk"] <= 10000 and tight["risk"] <= 10000
    assert abs(wide["risk"] - 10000) < 190          # within one share of budget
    assert wide["cost"] < tight["cost"]             # and ties up less capital


def test_position_size_rejects_unusable_geometry():
    assert tw_stocks.position_size(100.0, 100.0) == {}      # zero risk
    assert tw_stocks.position_size(100.0, 120.0) == {}      # stop above entry
    assert tw_stocks.position_size(None, 90.0) == {}
    assert tw_stocks.position_size(100.0, 90.0, budget=0) == {}


def test_position_size_returns_nothing_when_one_share_busts_the_budget():
    assert tw_stocks.position_size(2000.0, 1500.0, budget=100) == {}


def test_scorecard_tick_sends_once_and_marks_state(monkeypatch, tmp_path):
    import line_push

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return SUNDAY

    monkeypatch.setattr(tw_stocks, "STATE_FILE", str(tmp_path / "tw.json"))
    monkeypatch.setattr(tw_stocks, "datetime", _FixedDT)
    tw_stocks._save_state({"active_setups": []})

    sent = []
    monkeypatch.setattr(line_push, "enabled", lambda: True)
    monkeypatch.setattr(line_push, "send", lambda text: sent.append(text) or True)

    assert tw_stocks.scorecard_tick() is True
    assert sent and "本週" in sent[0]
    assert tw_stocks._load_state()["scorecard_sent_week"] == "2026-W29"

    # same week → no second send
    assert tw_stocks.scorecard_tick() is False
    assert len(sent) == 1


def test_scorecard_tick_noop_when_line_disabled(monkeypatch, tmp_path):
    import line_push

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return SUNDAY

    monkeypatch.setattr(tw_stocks, "STATE_FILE", str(tmp_path / "tw.json"))
    monkeypatch.setattr(tw_stocks, "datetime", _FixedDT)
    tw_stocks._save_state({"active_setups": []})
    monkeypatch.setattr(line_push, "enabled", lambda: False)
    assert tw_stocks.scorecard_tick() is False
    # state still marks the week as handled — no retry storm every sweep
    assert tw_stocks._load_state()["scorecard_sent_week"] == "2026-W29"


def test_tw_command_reads_state(monkeypatch):
    import tg_commands
    monkeypatch.setattr(tw_stocks, "_load_state",
                        lambda: {"last_digest_text": "🇹🇼 台股掃描 · test"})
    assert tg_commands.handle("tw") == "🇹🇼 台股掃描 · test"


def test_tw_command_no_state_yet(monkeypatch):
    import tg_commands
    monkeypatch.setattr(tw_stocks, "_load_state", lambda: {})
    assert "尚未有台股掃描" in tg_commands.handle("tw")


def test_clean_is_admin_only(monkeypatch):
    import tg_commands
    monkeypatch.setattr(tg_commands, "OWNER_IDS", {"42"})
    monkeypatch.setattr(tg_commands, "_group_admin_ids", lambda: {"77"})
    # /clean: owner ✓, group admin ✓, random member ✗
    assert tg_commands.authorized("clean", 42)
    assert tg_commands.authorized("purge", 77)
    assert not tg_commands.authorized("clean", 12345)
    assert not tg_commands.authorized("cleanall", 12345)
    # read-only MARKET commands stay open to everyone in the allowed chats.
    # (/winrate used to be the example here; it reads the owner's real
    # account and became owner-only on 2026-08-02 — see test_tg_commands.)
    assert tg_commands.authorized("signals", 12345)
    assert tg_commands.authorized("liq", None)


def test_cleanall_needs_confirmation_then_sweeps(monkeypatch):
    import telegram_utils
    import tg_commands
    # without the explicit "yes" it only warns — nothing is deleted
    out = tg_commands.handle("cleanall", "")
    assert "cleanall yes" in out
    # with "yes": sweeps below the FIRST ledger-recorded id of the group chat
    monkeypatch.setattr(tg_commands.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setattr(telegram_utils, "_load_sent",
                        lambda: [{"chat": "-100123", "mid": 750},
                                 {"chat": "-100123", "mid": 700},
                                 {"chat": "999", "mid": 5}])      # other chat ignored
    swept = {}

    def fake_deep_clean(chat, upto):
        swept["chat"], swept["upto"] = chat, upto
        return {"deleted": 42, "skipped": 10, "tried": 699}

    monkeypatch.setattr(telegram_utils, "deep_clean", fake_deep_clean)
    out = tg_commands.handle("cleanall", "yes")
    assert swept == {"chat": "-100123", "upto": 700}
    assert "已刪除 42 則" in out


def test_group_admin_ids_cached(monkeypatch):
    import tg_commands
    calls = []

    def fake_api(method, **params):
        calls.append(method)
        return {"result": [{"user": {"id": 77}}, {"user": {"id": 88}}]}

    monkeypatch.setattr(tg_commands, "_api", fake_api)
    monkeypatch.setattr(tg_commands.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setattr(tg_commands, "_admin_cache", {"ts": 0.0, "ids": set()})
    assert tg_commands._group_admin_ids() == {"77", "88"}
    assert tg_commands._group_admin_ids() == {"77", "88"}   # served from cache
    assert len(calls) == 1


def test_clean_command_parses_hours(monkeypatch):
    import telegram_utils
    import tg_commands
    called = {}

    def fake_clean(hours):
        called["hours"] = hours
        return {"deleted": 2, "too_old": 1, "kept": 3, "failed": 0}

    monkeypatch.setattr(telegram_utils, "clean_old_messages", fake_clean)
    out = tg_commands.handle("clean", "6")
    assert called["hours"] == 6.0
    assert "已刪除 2 則" in out and "超過48h" in out and "3 則未到時限" in out

    tg_commands.handle("clean", "")          # default
    assert called["hours"] == 24.0
    tg_commands.handle("clean", "999")       # clamped under Telegram's 48h wall
    assert called["hours"] == 47.0
    tg_commands.handle("clean", "abc")       # junk → default
    assert called["hours"] == 24.0


# ── web_view (/tw dad-facing page payload) ────────────────────────────────────
FRIDAY = datetime(2026, 7, 24, 15, 0, tzinfo=tw_stocks.TZ)   # a weekday afternoon


def _seed_web_state(monkeypatch, tmp_path, regime_ok=True):
    """State with one fresh (today) setup, one older tracking, one TP-hit,
    one SL-hit — the four card kinds web_view must group."""
    monkeypatch.setattr(tw_stocks, "STATE_FILE", str(tmp_path / "tw.json"))
    tw_stocks._save_state({
        "last_run_date": "2026-07-24",
        "last_regime": {"ok": regime_ok, "close": 1.0, "sma100": 1.0, "mom20": 0.1},
        "active_setups": [
            {"code": "2330", "name": "台積電", "date": "2026-07-24",
             "ref": 1000.0, "sl": 900.0, "tp": 1200.0},          # fresh → new
            {"code": "2317", "name": "鴻海", "date": "2026-07-17",
             "ref": 200.0, "sl": 180.0, "tp": 240.0},            # older → tracking
            {"code": "2454", "name": "聯發科", "date": "2026-07-10",
             "ref": 50.0, "sl": 45.0, "tp": 60.0,
             "hit": {"kind": "tp", "date": "2026-07-15", "time": "10:30"}},
            {"code": "2308", "name": "台達電", "date": "2026-07-08",
             "ref": 80.0, "sl": 72.0, "tp": 96.0,
             "hit": {"kind": "sl", "date": "2026-07-12", "time": "13:00"}},
        ],
    })


def test_web_view_groups_statuses_and_levels(monkeypatch, tmp_path):
    import stocks_data
    _seed_web_state(monkeypatch, tmp_path)
    # live quote near 台積電's entry (→ buy_zone), 鴻海 unquoted
    monkeypatch.setattr(stocks_data, "tw_quote_map",
                        lambda: {"2330": {"price": 1010.0, "change_pct": 1.5}})

    v = tw_stocks.web_view(now=FRIDAY)
    assert v["regime_ok"] is True and v["as_of"] == "2026-07-24"
    by = {s["code"]: s for s in v["setups"]}

    # statuses
    assert by["2330"]["status"] == "new"          # today + bullish regime
    assert by["2317"]["status"] == "tracking"     # older, unhit
    assert by["2454"]["status"] == "tp"
    assert by["2308"]["status"] == "sl"
    # open before closed in the ordering
    assert [s["status"] for s in v["setups"]][:2] == ["new", "tracking"]
    assert v["open_count"] == 2 and v["new_count"] == 1

    tsmc = by["2330"]
    assert tsmc["risk_pct"] == 10.0 and tsmc["gain_pct"] == 20.0
    assert tsmc["rr"] == 2.0                       # (1200-1000)/(1000-900)
    assert tsmc["price"] == 1010.0 and tsmc["dist_pct"] == 1.0
    assert tsmc["buy_zone"] is True                # within ¼ stop-distance of entry
    assert tsmc["ref_s"] == "1,000" and tsmc["sl_s"] == "900.0"  # _px formatting
    assert by["2317"].get("price") is None         # unquoted → no live fields
    assert v["buy_zone_count"] == 1


def test_web_view_buy_zone_excludes_faded_setup(monkeypatch, tmp_path):
    import stocks_data
    _seed_web_state(monkeypatch, tmp_path)
    # price 70% of the way down to the stop — near the stop, NOT the entry
    monkeypatch.setattr(stocks_data, "tw_quote_map",
                        lambda: {"2330": {"price": 930.0, "change_pct": -3.0}})
    v = tw_stocks.web_view(now=FRIDAY)
    tsmc = next(s for s in v["setups"] if s["code"] == "2330")
    assert tsmc["price"] == 930.0
    assert tsmc["buy_zone"] is False               # faded toward stop, not a buy


def test_web_view_regime_off_marks_no_new(monkeypatch, tmp_path):
    import stocks_data
    _seed_web_state(monkeypatch, tmp_path, regime_ok=False)
    monkeypatch.setattr(stocks_data, "tw_quote_map", lambda: {})
    v = tw_stocks.web_view(now=FRIDAY)
    assert v["regime_ok"] is False and v["new_count"] == 0
    # today's setup is downgraded to tracking when the regime is off
    assert next(s for s in v["setups"] if s["code"] == "2330")["status"] == "tracking"


def test_web_view_regime_fallback_from_plain_digest(monkeypatch, tmp_path):
    import stocks_data
    monkeypatch.setattr(tw_stocks, "STATE_FILE", str(tmp_path / "tw.json"))
    monkeypatch.setattr(stocks_data, "tw_quote_map", lambda: {})
    tw_stocks._save_state({                        # pre-last_regime state shape
        "last_run_date": "2026-07-24",
        "last_digest_plain": "🇹🇼 台股掃描\n\n✅ 大盤多頭 — 加權指數 …",
        "active_setups": [],
    })
    assert tw_stocks.web_view(now=FRIDAY)["regime_ok"] is True


def test_web_view_survives_quote_failure(monkeypatch, tmp_path):
    import stocks_data
    _seed_web_state(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("MIS down")

    monkeypatch.setattr(stocks_data, "tw_quote_map", boom)
    v = tw_stocks.web_view(now=FRIDAY)             # must not raise
    assert len(v["setups"]) == 4


def test_web_view_attaches_financials_per_setup(monkeypatch, tmp_path):
    """conftest's autouse guard stubs tw_financials.summary to all-None —
    this proves web_view actually calls it and attaches the result per
    setup (keyed correctly by code), not just that the page doesn't crash."""
    import stocks_data
    import tw_financials
    _seed_web_state(monkeypatch, tmp_path)
    monkeypatch.setattr(stocks_data, "tw_quote_map", lambda: {})
    fake = {
        "2330": {"rev_yoy": 32.4, "rev_month": "2026-06", "eps_cur": 11.0,
                 "eps_yoy": 25.0, "eps_season": "2026 Q2", "next_deadline": "2026-08-14"},
        "2317": {"rev_yoy": None, "rev_month": None, "eps_cur": None,
                 "eps_yoy": None, "eps_season": None, "next_deadline": "2026-08-14"},
    }
    monkeypatch.setattr(tw_financials, "summary", lambda code: fake.get(code, {}))

    v = tw_stocks.web_view(now=FRIDAY)
    by = {s["code"]: s for s in v["setups"]}
    assert by["2330"]["fin"]["rev_yoy"] == 32.4
    assert by["2330"]["fin"]["eps_season"] == "2026 Q2"
    assert by["2317"]["fin"]["rev_yoy"] is None       # attached, correctly all-None
    assert all(s.get("price") is None for s in v["setups"])


def test_biz_days_since_counts_weekdays_only():
    mon = datetime(2026, 7, 20, 12, 0, tzinfo=tw_stocks.TZ)   # Monday
    assert tw_stocks._biz_days_since("2026-07-20", mon) == 0   # same day
    assert tw_stocks._biz_days_since("2026-07-17", mon) == 1   # Fri→Mon = 1 biz day
    assert tw_stocks._biz_days_since("2026-07-13", mon) == 5   # prior Mon→Mon
    assert tw_stocks._biz_days_since("garbage", mon) == 0      # unparseable → 0


# ── settling tracked setups against their own daily bars ────────────────────
# tw_intraday only stamps a level it watched cross LIVE, so anything that
# happened while it was not polling was never recorded: 2382 廣達 first traded
# below its 344.5 stop on 2026-07-17 and sat below it on 14 of the next 17
# sessions, still showing 未觸發 three weeks later. The weekly scorecard counted
# that stop-out as an open trade, so the win rate it reported was not the
# strategy's.
def _daily(prices, start_ts=1_784_000_000):
    """[(ts,o,h,l,c,v)] from (high, low) pairs, one per session."""
    return [(start_ts + i * 86400, l, h, l, c if (c := (h + l) / 2) else h, 1000)
            for i, (h, l) in enumerate(prices)]


def _rec(**kw):
    base = {"code": "2382", "date": "2026-07-14", "ref": 380.0,
            "sl": 344.5, "tp": 439.1}
    base.update(kw)
    return base


def _rows_from(day0, pairs):
    import datetime as dt
    d = dt.datetime.strptime(day0, "%Y-%m-%d").replace(tzinfo=tw_stocks.TZ)
    return [(int((d + dt.timedelta(days=i + 1)).timestamp()), l, h, l, (h + l) / 2, 1000)
            for i, (h, l) in enumerate(pairs)]


def test_a_missed_stop_is_settled_from_the_daily_bars():
    rec = tw_stocks.reconcile_setup(_rec(), _rows_from("2026-07-14", [(390, 370), (360, 324.5)]))
    assert rec["hit"]["kind"] == "sl" and rec["hit"]["date"] == "2026-07-16"


def test_a_target_is_settled_too():
    rec = tw_stocks.reconcile_setup(_rec(), _rows_from("2026-07-14", [(400, 380), (445, 430)]))
    assert rec["hit"]["kind"] == "tp"


def test_the_stop_wins_when_both_trade_in_one_session():
    """The backtest resolves it stop-first. A tracker that resolved it the
    other way would report a record the rules cannot produce."""
    rec = tw_stocks.reconcile_setup(_rec(), _rows_from("2026-07-14", [(445, 320)]))
    assert rec["hit"]["kind"] == "sl"


def test_the_entry_session_itself_cannot_resolve_it():
    """Entry is the NEXT session's open, so the signal bar is not a hold day."""
    rec = tw_stocks.reconcile_setup(_rec(), [(int(tw_stocks.datetime.strptime(
        "2026-07-14", "%Y-%m-%d").replace(tzinfo=tw_stocks.TZ).timestamp()), 380, 500, 300, 400, 1)])
    assert not rec.get("hit")


def test_the_hold_is_counted_in_sessions_not_calendar_days():
    """MAX_HOLD is 40 TRADING sessions ≈ 56 calendar days. Retiring on 30
    calendar days abandoned trades ~26 days before the rules say to, so the
    tracked record could not match the backtest that justifies the rules."""
    quiet = [(400, 390)] * (tw_stocks.MAX_HOLD + 5)
    rec = tw_stocks.reconcile_setup(_rec(), _rows_from("2026-07-14", quiet))
    assert rec["hit"]["kind"] == "timeout"


def test_an_open_setup_is_left_alone_and_counted():
    rec = tw_stocks.reconcile_setup(_rec(), _rows_from("2026-07-14", [(400, 390)] * 3))
    assert not rec.get("hit") and rec["held"] == 3


def test_an_already_stamped_setup_is_not_re_settled():
    """An intraday stamp carries the real touch TIME; a daily re-read would
    overwrite it with '—'."""
    rec = _rec(hit={"kind": "sl", "date": "2026-07-17", "time": "09:59"})
    out = tw_stocks.reconcile_setup(rec, _rows_from("2026-07-14", [(445, 300)]))
    assert out["hit"]["time"] == "09:59"


def test_a_junk_date_does_not_crash_the_scan():
    assert not tw_stocks.reconcile_setup(_rec(date="not-a-date"), []).get("hit")
    assert not tw_stocks.reconcile_setup({"code": "X"}, []).get("hit")
