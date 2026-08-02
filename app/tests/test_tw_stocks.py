"""tw_stocks — pure signal/regime/digest/scheduling logic."""
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


def test_weekly_scorecard_tallies_wins_losses_and_open():
    state = {"active_setups": [
        _week_setup("2330", "2026-07-14", {"kind": "tp", "date": "2026-07-16", "time": "10:00"}),
        _week_setup("2317", "2026-07-15", {"kind": "sl", "date": "2026-07-17", "time": "11:00"}),
        _week_setup("2454", "2026-07-16"),                     # still open
        _week_setup("2882", "2026-07-06"),                     # last week — excluded
    ]}
    card = tw_stocks.weekly_scorecard(state, SUNDAY)
    assert card["total"] == 3 and card["wins"] == 1 and card["losses"] == 1
    assert card["still_open"] == 1
    assert abs(card["total_r"] - (5.0 / 3.0 - 1.0)) < 1e-9


def test_weekly_scorecard_empty_week():
    card = tw_stocks.weekly_scorecard({}, SUNDAY)
    assert card == {"iso_year": 2026, "iso_week": 29, "total": 0, "wins": 0,
                    "losses": 0, "still_open": 0, "total_r": 0.0}


def test_scorecard_plain_empty_week_message():
    card = tw_stocks.weekly_scorecard({}, SUNDAY)
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "本週沒有新增訊號" in msg


def test_scorecard_plain_shows_tally_and_r():
    card = {"iso_year": 2026, "iso_week": 29, "total": 3, "wins": 2, "losses": 1,
            "still_open": 0, "total_r": 2.33}
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "達標 2 檔" in msg and "停損 1 檔" in msg and "+2.3R" in msg
    assert "⚠️" in msg


def test_scorecard_plain_negative_r_keeps_minus_sign():
    card = {"iso_year": 2026, "iso_week": 29, "total": 2, "wins": 0, "losses": 2,
            "still_open": 0, "total_r": -2.0}
    msg = tw_stocks.build_scorecard_plain(SUNDAY, card)
    assert "-2.0R" in msg and "+-2.0R" not in msg


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
