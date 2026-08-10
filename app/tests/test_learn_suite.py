"""signal outcomes + ETH-MOM forward test + engine verdicts + balance history."""
from datetime import datetime
from zoneinfo import ZoneInfo

import daily_report
import eth_mom
import signal_outcomes as SO

TS = 1_783_900_000.0


def _sig(direction="long", entry=100.0, sl=95.0, tp1=105.0, tp2=110.0):
    return {"symbol": "ETH/USDT:USDT", "base": "ETH", "direction": direction,
            "ts": TS, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "score": 90}


def _candle(hours_after, o=100.0, h=100.0, l=100.0, c=100.0):  # noqa: E741
    return (int((TS + hours_after * 3600) * 1000), o, h, l, c, 1.0)


# ── outcome evaluation ───────────────────────────────────────────────────────
def test_outcome_tp2_before_stop():
    candles = [_candle(1, h=106.0), _candle(2, h=111.0)]
    res = SO.evaluate(_sig(), candles)
    assert res["outcome"] == "tp2" and res["hours"] == 2.0


def test_outcome_stop_first_is_pessimistic():
    # same candle touches BOTH tp2 and sl → the stop wins the tie
    candles = [_candle(1, h=111.0, l=94.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "sl"


def test_outcome_partial_winner_then_stop():
    candles = [_candle(1, h=106.0), _candle(3, l=94.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "tp1→sl"


def test_outcome_short_direction_mirrors():
    sig = _sig("short", entry=100.0, sl=105.0, tp1=95.0, tp2=90.0)
    candles = [_candle(1, l=89.0)]
    assert SO.evaluate(sig, candles)["outcome"] == "tp2"


def test_outcome_nothing_hit_in_window():
    candles = [_candle(1), _candle(47), _candle(60, h=120.0)]  # 60h > window
    assert SO.evaluate(_sig(), candles)["outcome"] == "none"


def test_outcome_candles_before_signal_ignored():
    candles = [_candle(-1, l=90.0), _candle(1, h=111.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "tp2"


# ── exit-rule laboratory ─────────────────────────────────────────────────────
# _sig() geometry: entry 100, SL 95, TP1 105, TP2 110 → risk 5, r1 = 1R, r2 = 2R
def _exits(candles, sig=None):
    return SO.evaluate(sig or _sig(), candles)["exits"]


def test_exits_record_the_plan_geometry():
    res = SO.evaluate(_sig(), [_candle(1, h=111.0)])
    assert res["r1"] == 1.0 and res["r2"] == 2.0
    assert set(res["exits"]) == set(SO.EXIT_RULES)


def test_exits_absent_without_an_entry_price():
    sig = _sig()
    del sig["entry"]                       # can't measure R without the entry
    res = SO.evaluate(sig, [_candle(1, h=111.0)])
    assert res["outcome"] == "tp2" and "exits" not in res


def test_exits_full_stop_is_minus_one_r_for_every_rule():
    ex = _exits([_candle(1, l=94.0)])
    assert all(round(v, 3) == -1.0 for v in ex.values())


def test_exits_clean_tp2_pays_two_r():
    ex = _exits([_candle(1, h=111.0)])
    assert ex["hold"] == 2.0 and ex["be"] == 2.0
    assert ex["tp1_only"] == 1.0                 # capped at the near target
    assert ex["partial"] == 1.5                  # half at 1R + half at 2R


def test_exits_split_the_tp1_then_stop_bucket():
    # the 962-signal leak: +1R reached, then all the way back to a full stop
    ex = _exits([_candle(1, h=106.0), _candle(3, l=94.0)])
    assert ex["hold"] == -1.0                    # what the alert says to do
    assert ex["be"] == 0.0                       # scratched instead of lost
    assert ex["partial"] == 0.0                  # +0.5R booked, -0.5R stopped
    assert ex["partial_be"] == 0.5
    assert ex["tp1_only"] == 1.0


def test_breakeven_stop_also_kills_winners():
    """The trade-off that decides whether BE is worth it: TP1 → pull back
    through entry → TP2. 'hold' collects 2R; 'be' was already stopped flat."""
    ex = _exits([_candle(1, h=106.0), _candle(2, l=99.0), _candle(3, h=111.0)])
    assert ex["hold"] == 2.0
    assert ex["be"] == 0.0
    assert ex["partial_be"] == 0.5


def test_stop_tightening_waits_for_the_next_candle():
    """TP1 and the pullback to entry inside ONE candle: the order is unknowable,
    so the tightened stop must not fire on the candle that armed it."""
    candles = [_candle(1, h=106.0, l=99.0),      # touches TP1 AND dips below entry
               _candle(2, o=101.0, h=111.0, l=101.0, c=110.0)]
    assert _exits(candles)["be"] == 2.0          # survived, then reached TP2


def test_trailing_rule_lets_a_runner_run_past_tp2():
    candles = [_candle(1, h=106.0), _candle(2, o=106.0, h=130.0, l=106.0, c=130.0)]
    ex = _exits(candles)
    assert ex["hold"] == 2.0                     # capped by the fixed target
    assert ex["trail_1r"] == 6.0                 # marked out at the window close


def test_unresolved_signals_are_marked_to_the_last_close():
    res = SO.evaluate(_sig(), [_candle(1, c=102.0)])
    assert res["outcome"] == "none"              # counting it as 0R would flatter
    assert res["exits"]["hold"] == 0.4


def test_exits_mirror_for_shorts():
    sig = _sig("short", entry=100.0, sl=105.0, tp1=95.0, tp2=90.0)
    assert _exits([_candle(1, l=89.0)], sig)["hold"] == 2.0
    assert _exits([_candle(1, h=106.0)], sig)["hold"] == -1.0


def test_expectancy_ranks_rules_and_reports_pf():
    outs = [{"exits": {"hold": 2.0, "be": 0.0}},
            {"exits": {"hold": -1.0, "be": 0.0}},
            {"exits": {"hold": -1.0, "be": -1.0}}]
    st = SO.expectancy(outs)
    assert st["hold"]["n"] == 3
    assert round(st["hold"]["exp"], 4) == 0.0
    assert st["hold"]["pf"] == 1.0
    assert round(st["be"]["exp"], 4) == round(-1 / 3, 4)
    assert st["be"]["pf"] == 0.0                 # no winners at all
    # PF is undefined, not infinite, when a rule never lost
    assert SO.expectancy([{"exits": {"hold": 1.0}}])["hold"]["pf"] is None


def test_expectancy_ignores_records_without_exit_data():
    assert SO.expectancy([{"outcome": "sl"}, {"outcome": "tp2"}]) == {}


def test_exit_table_flags_a_losing_current_rule():
    outs = [{"exits": {"hold": -1.0, "be": 1.0}} for _ in range(3)]
    txt = SO.exit_table(outs)
    assert "期望值是<b>負的</b>" in txt           # the warning is mandatory
    assert "還不夠下結論" in txt                  # so is the small-sample note
    assert "樣本數不等於獨立次數" in txt          # and the correlation caveat
    assert SO._RULE_ZH["be"] in txt
    assert "最好的是" in txt                      # be(+1R) genuinely beats hold


def test_exit_table_refuses_to_recommend_the_best_of_six_losers():
    outs = [{"exits": {"hold": -1.0, "be": -0.9}} for _ in range(3)]
    txt = SO.exit_table(outs)
    assert "每一種出場規則都是負的" in txt
    assert "問題不在出場，在進場" in txt
    assert "最好的是" not in txt                  # ranking losers ≠ advice


def test_exit_table_ignores_a_noise_sized_improvement():
    outs = [{"exits": {"hold": 1.0, "be": 1.02}} for _ in range(3)]
    assert "最好的是" not in SO.exit_table(outs)  # +0.02R is not a finding


def test_exit_table_empty_without_data():
    assert SO.exit_table([{"outcome": "sl"}]) == ""


def test_totals_survive_the_thirty_day_prune():
    state: dict = {}
    SO._accumulate(state, {"exits": {"hold": 2.0}, "eval_ts": TS})
    SO._accumulate(state, {"exits": {"hold": -1.0}, "eval_ts": TS})
    SO._accumulate(state, {"outcome": "sl"})              # no exits → ignored
    # Subset, not equality: the accounting fields are what this test is about,
    # and pinning the exact key set made adding sumsq (2026-08-10, for the
    # confidence intervals on /reality) look like a regression in the prune.
    bucket = state["totals"]["all"]["hold"]
    for k, v in {"n": 2, "sum": 1.0, "wins": 1, "gain": 2.0, "loss": 1.0}.items():
        assert bucket[k] == v, f"{k} drifted"
    st = SO.totals_stats(state["totals"]["all"])
    assert st["hold"]["exp"] == 0.5 and st["hold"]["pf"] == 2.0
    assert state["totals_since"] == TS


def test_totals_record_a_sum_of_squares_for_later_confidence_intervals():
    """The per-trade R values are gone once RETAIN_D prunes the records, so if
    the running tally does not keep a sum of squares the variance is lost
    forever and no honest error bar can ever be computed — which is exactly the
    hole /reality had to work around for its historical data.

    sumsq_n tracks COVERAGE separately: buckets that predate this carry a
    sumsq describing only part of their n, and reality.ci() must be able to
    tell that apart from a fully covered bucket.
    """
    state: dict = {}
    SO._accumulate(state, {"exits": {"hold": 2.0}, "eval_ts": TS})
    SO._accumulate(state, {"exits": {"hold": -1.0}, "eval_ts": TS})
    bucket = state["totals"]["all"]["hold"]
    assert bucket["sumsq"] == 5.0                 # 2² + (−1)²
    assert bucket["sumsq_n"] == bucket["n"] == 2  # fully covered

    import reality
    lo, hi, basis = reality.ci(bucket)
    assert basis == "exact"
    assert lo < 0.5 < hi                          # centred on the mean


def test_accumulate_splits_by_cohort():
    state: dict = {}
    SO._accumulate(state, {"exits": {"hold": 2.0}, "direction": "long"})
    SO._accumulate(state, {"exits": {"hold": -1.0}, "direction": "short",
                           "premium": True, "hc": True})
    t = state["totals"]
    assert t["all"]["hold"]["n"] == 2
    assert t["long"]["hold"]["sum"] == 2.0
    assert t["short"]["hold"]["sum"] == -1.0
    assert t["premium"]["hold"]["n"] == 1 and t["hc"]["hold"]["n"] == 1
    assert "premium" not in SO.cohorts_of({"direction": "long"})


def test_cohort_table_calls_out_a_filter_that_underperforms():
    state: dict = {}
    for _ in range(3):                       # ordinary signals: +1R each
        SO._accumulate(state, {"exits": {"hold": 1.0}, "direction": "long"})
    SO._accumulate(state, {"exits": {"hold": -1.0}, "direction": "long",
                           "premium": True})   # the "best" ones lose
    txt = SO.cohort_table(state["totals"])
    assert "⭐ 精選" in txt and "做多" in txt
    assert "這層篩選目前沒有加分" in txt


def test_cohort_table_silent_when_the_filter_earns_its_keep():
    state: dict = {}
    SO._accumulate(state, {"exits": {"hold": -1.0}, "direction": "long"})
    SO._accumulate(state, {"exits": {"hold": 2.0}, "direction": "long",
                           "premium": True})
    assert "沒有加分" not in SO.cohort_table(state["totals"])


def test_cohort_table_empty_without_data():
    assert SO.cohort_table({}) == ""


def test_summarize_can_suppress_the_lab_table():
    outs = [{"outcome": "sl", "exits": {"hold": -1.0}}]
    assert "出場規則實測" in SO.summarize(outs)
    assert "出場規則實測" not in SO.summarize(outs, lab=False)


def test_tick_persists_exits_and_the_lifetime_tally(monkeypatch):
    """End to end: a due signal → evaluated record carrying R + exits, and a
    cohort tally that outlives the 30-day prune. STATE_FILE/SIGNALS_FILE are
    redirected to tmp by the autouse guard in conftest."""
    import json
    now = TS + 60 * 3600                       # signal is 60h old → due
    sig = {**_sig(), "symbol": "ETH/USDT:USDT", "premium": True}
    with open(SO.SIGNALS_FILE, "w", encoding="utf-8") as f:
        json.dump({"signals": [sig]}, f)
    monkeypatch.setattr(SO.time, "time", lambda: now)

    class _Client:
        def call(self, method, *a, **k):
            assert method == "fetch_ohlcv"
            return [_candle(1, h=106.0), _candle(3, l=94.0)]   # TP1 then stop

    assert SO.tick(_Client()) == 1
    with open(SO.STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    rec = next(iter(state["evaluated"].values()))
    assert rec["outcome"] == "tp1→sl"
    assert rec["r1"] == 1.0 and rec["r2"] == 2.0
    assert rec["exits"]["hold"] == -1.0 and rec["exits"]["be"] == 0.0
    assert state["totals"]["all"]["hold"]["n"] == 1
    assert state["totals"]["premium"]["hold"]["sum"] == -1.0

    assert SO.tick(_Client()) == 0              # never evaluated (or tallied) twice
    with open(SO.STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    assert state["totals"]["all"]["hold"]["n"] == 1
    assert state["signals"] == {}               # snapshot retired on the next pass


def test_batch_size_scales_with_the_backlog():
    assert SO._batch_size(0) == SO.EVAL_PER_TICK
    assert SO._batch_size(10) == SO.EVAL_PER_TICK          # short queue = trickle
    assert SO._batch_size(300) == 6
    assert SO._batch_size(5000) == SO.EVAL_MAX_PER_TICK    # capped, no API storm


def test_pending_drains_oldest_first():
    snaps = {"new": {"ts": TS - 50 * 3600, "sl": 1.0},
             "old": {"ts": TS - 200 * 3600, "sl": 1.0}}
    keys = [k for k, _ in SO._pending(snaps, {}, TS)]
    assert keys == ["old", "new"]        # closest to ageing out goes first


def test_aged_out_signals_are_reported_not_dropped_silently(capsys):
    state = {"signals": {"gone": {"ts": TS - 40 * 86400, "sl": 1.0}},
             "evaluated": {}}
    SO._snapshot(state, [], TS)
    assert state["signals"] == {}
    assert "aged out unevaluated" in capsys.readouterr().out


def test_outcomes_research_is_owner_only(tmp_path, monkeypatch):
    """Members get the scorecard. Whether the signals have any edge at all is
    the owner's to read first — the group must not be told by accident."""
    import json
    state = {"evaluated": {"a": {"outcome": "sl", "sig_ts": TS,
                                 "exits": {"hold": -1.0}}},
             "totals": {"all": {"hold": {"n": 9, "sum": -9.0, "wins": 0,
                                         "gain": 0.0, "loss": 9.0}}}}
    f = tmp_path / "outcomes.json"
    f.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(SO, "STATE_FILE", str(f))
    monkeypatch.setattr(SO.time, "time", lambda: TS + 3600)

    public = SO.report()
    assert "訊號成績單" in public
    assert "出場規則實測" not in public and "哪一種訊號在賺錢" not in public

    private = SO.report(owner=True)
    assert "出場規則實測" in private


def test_summarize_counts_and_small_sample_honesty():
    outs = [{"outcome": "tp2"}, {"outcome": "sl"}, {"outcome": "tp1→sl"}]
    txt = SO.summarize(outs)
    assert "🎯 到 TP2" in txt and "33%" in txt
    assert "⚠️ 停損" in txt and "67%" in txt
    assert "樣本只有 3 個" in txt          # small-n warning is mandatory


# ── ETH momentum rule ────────────────────────────────────────────────────────
def test_mom_direction_long_short_insufficient():
    up = [100.0 + i * 0.1 for i in range(200)]
    dn = [200.0 - i * 0.1 for i in range(200)]
    assert eth_mom.direction(up) == "long"
    assert eth_mom.direction(dn) == "short"
    assert eth_mom.direction(up[:100]) is None          # < N+1 bars


def test_mom_closed_bars_drops_forming_candle():
    now = TS
    bars = [(int((now - 7200 * 2) * 1000),) + (1, 1, 1, 1, 1),
            (int((now - 3600) * 1000),) + (1, 1, 1, 1, 1)]   # opened 1h ago → forming
    assert len(eth_mom.closed_bars(bars, now)) == 1
    assert len(eth_mom.closed_bars(bars, now + 7200)) == 2


def test_mom_leg_pnl_signs():
    assert abs(eth_mom.leg_pnl_pct("long", 100.0, 110.0) - 10.0) < 1e-9
    assert abs(eth_mom.leg_pnl_pct("short", 100.0, 110.0) + 10.0) < 1e-9


# ── engine verdicts + balance history ────────────────────────────────────────
def test_engine_verdict_flags_losing_engine():
    s = {"ok": True, "n_trades": 40, "profit_factor": 0.34,
         "daily": [{"net": -5.0}] * 7}
    lines = daily_report._engine_verdict("Bybit S3", s)
    assert "沒有支付它的風險" in lines[0]
    good = {"ok": True, "n_trades": 40, "profit_factor": 1.5, "daily": []}
    assert "✅" in daily_report._engine_verdict("X", good)[0]
    assert daily_report._engine_verdict("Y", None) == ["  Y: 無成交紀錄"]


def test_monday_report_includes_verdicts():
    data = {"binance_pnl": {"ok": True, "n_trades": 12, "profit_factor": 0.8,
                            "daily": []},
            "bybit_pnl": None}
    monday = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    txt = daily_report.build_report(data, monday)
    assert "🧪 引擎週檢" in txt
    tuesday = datetime(2026, 7, 14, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert "🧪" not in daily_report.build_report(data, tuesday)


def test_record_balance_appends_and_replaces(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "BALANCE_FILE", str(tmp_path / "bal.json"))
    now = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    data = {"binance_snap": {"balance": {"total": 21.5}},
            "bybit_snap": {"balance": {"total": 231.0}}}
    assert daily_report.record_balance(data, now) is True
    data["bybit_snap"]["balance"]["total"] = 240.0      # same day → replace
    assert daily_report.record_balance(data, now) is True
    import json
    with open(tmp_path / "bal.json") as f:
        hist = json.load(f)
    assert len(hist) == 1
    assert hist[0] == {"date": "2026-07-13", "binance": 21.5,
                       "bybit": 240.0, "total": 261.5}


def test_record_balance_skips_when_both_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "BALANCE_FILE", str(tmp_path / "bal.json"))
    now = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert daily_report.record_balance({"binance_snap": None,
                                        "bybit_snap": None}, now) is False
