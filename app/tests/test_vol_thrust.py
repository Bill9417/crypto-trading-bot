"""⚡ 隧道上方爆量 — above the middle tunnel with an hour of buying under it.

Asked for on 2026-08-21 with an explicit requirement: "make sure the alert will
be on time". That is the load-bearing part, and it is the part an obvious
implementation fails SILENTLY — strategy2_scanner caches candles per 15m bucket
and never patches VOLUME onto the forming bar, so a detector reading only the
sweep's candles cannot see volume that arrived inside the current bucket and is
always up to ~20 minutes late while looking like it works. Hence the split read
(15m tunnel, free; 5m surge, one call) and hence `age_s` on every signal.

The measured verdict is negative — both intervals entirely below zero — so the
tests also pin that the card cannot render the coins without the verdict.
"""
import json
import os
import time

import vol_thrust as T

HERE = os.path.dirname(os.path.abspath(__file__))
HOUR_MS = 3600_000


def _bars(n, tf_min, start=100.0, drift=0.0, vol=1000.0, t0=1_700_000_000_000):
    rows, px = [], start
    for i in range(n):
        px = max(0.01, px + drift)
        rows.append([t0 + i * tf_min * 60_000, px, px * 1.004, px * 0.996, px, vol])
    return rows


def _above_tunnel_15m(n=400):
    """A series that ends comfortably above its EMA144/169."""
    rows = _bars(300, 15, start=100.0, drift=-0.02)
    px, t = rows[-1][4], rows[-1][0]
    for _ in range(n - 300):
        px *= 1.004
        t += 15 * 60_000
        rows.append([t, px / 1.004, px * 1.005, px / 1.005, px, 1000.0])
    return rows


def _5m(baseline_vol=100.0, window_vol=None, up=True, hours=None):
    """24h of baseline 5m bars plus one window of `window_vol` per bar."""
    hours = T.BASELINE_HOURS if hours is None else hours
    per_hour = 12
    rows = _bars(per_hour * hours, 5, start=50.0, vol=baseline_vol)
    t = rows[-1][0]
    win = T.bars_in_window()
    wv = baseline_vol if window_vol is None else window_vol
    for _ in range(win):
        t += 5 * 60_000
        o, c = (50.0, 50.5) if up else (50.5, 50.0)
        rows.append([t, o, 51.0, 49.0, c, wv])
    rows.append([t + 5 * 60_000, 50.0, 50.0, 50.0, 50.0, 0.0])   # forming
    return rows


# ── gate 1: the tunnel ───────────────────────────────────────────────────────
def test_it_reads_price_above_the_middle_tunnel():
    t = T.tunnel_read(_above_tunnel_15m())
    assert t and t["above"] is True
    assert t["price"] > t["tunnel_top"] > 0
    assert t["above_pct"] >= T.MIN_ABOVE_PCT


def test_inside_the_tunnel_is_not_above_it():
    """A symbol oscillating across the line must not fire on every wobble."""
    rows = _above_tunnel_15m()
    t = T.tunnel_read(rows)
    # place the last closed bar just inside the band
    mid = (t["tunnel_top"] + t["tunnel_bottom"]) / 2
    rows[-2] = [rows[-2][0], mid, mid, mid, mid, 1000.0]
    t2 = T.tunnel_read(rows)
    assert t2["above"] is False
    assert "隧道" in T.why_not(t2)


def test_barely_above_the_line_is_not_above_it():
    """price > top is not enough — a symbol grazing the line would fire on
    every wobble. MIN_ABOVE_PCT is the margin, and only a bar ABOVE the top
    but inside that margin can test it."""
    rows = _above_tunnel_15m()

    def read_with(close):
        r = [list(x) for x in rows]
        r[-2] = [r[-2][0], close, close, close, close, 1000.0]
        return T.tunnel_read(r)

    # Solved, not assumed: the tunnel is an EMA of the closes, so moving the
    # last close moves the line it is being compared against. A fixed guess
    # lands wherever the feedback puts it (it landed at 0.36%, outside the
    # margin, and the test passed for the wrong reason).
    lo, hi = 0.5, 2.0
    target = T.MIN_ABOVE_PCT / 2.0
    for _ in range(60):
        mid = (lo + hi) / 2
        r = read_with(T.tunnel_read(rows)["tunnel_top"] * mid)
        if r["above_pct"] < target:
            lo = mid
        else:
            hi = mid
    t2 = read_with(T.tunnel_read(rows)["tunnel_top"] * ((lo + hi) / 2))
    assert t2["price"] > t2["tunnel_top"], "fixture is not above the line"
    assert 0 < t2["above_pct"] < T.MIN_ABOVE_PCT
    assert t2["above"] is False, "the noise margin is not enforced"
    assert "剛站上" in T.why_not(t2)


def test_not_enough_history_returns_nothing_not_below():
    """{} , never a dict saying 'below'. A missing read must not assert a
    position the data cannot support."""
    assert T.tunnel_read(_bars(50, 15)) == {}
    assert "K 棒不足" in T.why_not({})


# ── gate 2: the surge ────────────────────────────────────────────────────────
def test_a_quiet_hour_is_not_a_surge():
    s = T.surge_read(_5m(baseline_vol=100.0, window_vol=100.0))
    assert s and s["vol_mult"] is not None
    assert s["vol_mult"] < T.VOL_MULT
    assert T.is_surge(s) is False


def test_an_hour_of_heavy_buying_is():
    s = T.surge_read(_5m(baseline_vol=100.0, window_vol=600.0, up=True))
    assert s["vol_mult"] >= T.VOL_MULT
    assert s["buy_share"] == 1.0
    assert T.is_surge(s) is True


def test_heavy_selling_is_not_buying():
    """Without the buy-share gate a capitulation hour — enormous volume, price
    collapsing — reads identically to accumulation."""
    s = T.surge_read(_5m(baseline_vol=100.0, window_vol=600.0, up=False))
    assert s["vol_mult"] >= T.VOL_MULT, "the volume itself was not seen"
    assert s["buy_share"] == 0.0
    assert T.is_surge(s) is False
    assert "買方" in T.why_not({"above": True, "atr_pct": 5.0, "price": 2,
                                "tunnel_top": 1, "tunnel_bottom": 0.5,
                                "above_pct": 5}, s)


def test_the_window_is_an_hour_of_confirm_bars():
    assert T.bars_in_window("5m") == 12
    assert T.bars_in_window("15m") == 4
    s = T.surge_read(_5m())
    assert s["bars"] == T.bars_in_window()
    assert s["confirm_tf"] == T.CONFIRM_TF


def test_no_baseline_reports_none_rather_than_a_quiet_hour():
    """vol_mult 0.0 would assert the hour was normal. Nobody measured it."""
    rows = _5m(baseline_vol=0.0, window_vol=500.0)
    s = T.surge_read(rows)
    assert s["vol_mult"] is None
    assert T.is_surge(s) is False
    assert "基準" in T.why_not({"above": True, "atr_pct": 5.0, "price": 2,
                                "tunnel_top": 1, "tunnel_bottom": .5,
                                "above_pct": 5}, s)


# ── the requirement: on time ─────────────────────────────────────────────────
def test_every_signal_reports_how_late_it_is():
    """`age_s` is the whole 'on time' requirement, measured rather than
    asserted. A detector that cannot say how late it is turns a stale
    observation into a current one."""
    rows = _5m(baseline_vol=100.0, window_vol=600.0)
    last_close = rows[-2][0] / 1000.0 + 300         # -2 is the last CLOSED bar
    s = T.surge_read(rows, now=last_close + 42)
    assert s["age_s"] == 42


def test_the_forming_bar_is_dropped():
    """Its volume is a partial hour compared against a full hour's average,
    which under-reports early in the bar — the detector would be late in
    exactly the case it exists to catch, and would look like it worked."""
    rows = _5m(baseline_vol=100.0, window_vol=600.0)
    closed_only = T.surge_read(rows)
    # a huge forming bar must not change the answer
    rows[-1] = [rows[-1][0], 50.0, 51.0, 49.0, 50.9, 99_999.0]
    assert T.surge_read(rows)["vol_mult"] == closed_only["vol_mult"]


def test_the_ranking_key_is_not_a_gate():
    """recent_volume_rank picks WHO gets a 5m call. It reads bucket-old 15m
    data and cannot see the newest volume, so it must never decide outcome."""
    import inspect
    src = inspect.getsource(T.consider)
    assert "recent_volume_rank" not in src, \
        "the stale ranking key leaked into the decision"


# ── the whole thing ──────────────────────────────────────────────────────────
def _fetch5(_sym, _tf, _n, **kw):
    return _5m(baseline_vol=100.0, window_vol=600.0, up=True)


def test_consider_fires_only_when_both_gates_pass():
    st = {}
    sig = T.consider("A/USDT:USDT", _above_tunnel_15m(), st, time.time(),
                     fetch_tf=_fetch5)
    assert sig and sig["side"] == "long"
    assert sig["vol_mult"] >= T.VOL_MULT and sig["above_pct"] > 0
    assert "age_s" in sig


def test_a_quiet_hour_above_the_tunnel_does_not_fire():
    quiet = lambda *a, **k: _5m(baseline_vol=100.0, window_vol=100.0)  # noqa: E731
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), {}, time.time(),
                      fetch_tf=quiet) == {}


def test_a_surge_below_the_tunnel_does_not_fire():
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return _5m(baseline_vol=100.0, window_vol=600.0)

    below = _bars(400, 15, start=100.0, drift=-0.05)
    assert T.consider("A/USDT:USDT", below, {}, time.time(), fetch_tf=spy) == {}
    assert not calls, "spent a 5m call on a symbol that failed the free gate"


def test_the_cooldown_stops_a_repeat():
    st = {}
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), st, 1000.0,
                      fetch_tf=_fetch5)
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), st, 1060.0,
                      fetch_tf=_fetch5) == {}
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), st,
                      1000.0 + T.COOLDOWN_SEC + 1, fetch_tf=_fetch5)


def test_the_cooldown_survives_a_zero_timestamp():
    """`if last and ...` skips the guard entirely when the stored time is 0.
    Live that never happens; in a replay or a test it means the guard is not
    actually being exercised by the thing that claims to exercise it — which
    is how the same bug sat in vegas_reclaim until a test caught it."""
    st = {}
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), st, 0.0,
                      fetch_tf=_fetch5)
    assert (st.get("last") or {}).get("A/USDT:USDT") == 0.0
    assert T.consider("A/USDT:USDT", _above_tunnel_15m(), st, 1.0,
                      fetch_tf=_fetch5) == {}


def test_it_ships_no_trade_plan():
    sig = T.consider("A/USDT:USDT", _above_tunnel_15m(), {}, time.time(),
                     fetch_tf=_fetch5)
    for banned in ("entry", "sl", "tp", "plan", "stop_pct", "rr"):
        assert banned not in sig, f"an observe-only signal shipped {banned}"


# ── the record and the board ─────────────────────────────────────────────────
def test_the_card_lists_every_live_coin_not_just_the_top_slice():
    """Asked for 2026-08-22: the card counted 72 coins it would not show.
    The cap that matters is the BOOK's, not the card's — hiding rows from the
    person who asked for the detector serves nothing."""
    st = {"recent": [
        {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "fired_ts": 1000.0 + i,
         "vol_mult": float(i)} for i in range(20)]}
    v = T.web_view(st, now=1000.0)
    assert len(v["top"]) == T.TOP_N
    assert len(v["rest"]) == 20 - T.TOP_N
    assert v["live_n"] == len(v["top"]) + len(v["rest"]), \
        "the count contradicts the rows the card renders"
    # strongest first, across the join
    order = [r["vol_mult"] for r in v["top"] + v["rest"]]
    assert order == sorted(order, reverse=True)


def test_the_watchlist_stays_capped_even_though_the_card_does_not():
    """Two caps, two jobs. 每日觀察清單 ranks by how many INDEPENDENT engines
    agree, so a source flagging 23 coins would sit beside almost everything
    and stop distinguishing anything."""
    st = {"recent": [
        {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "fired_ts": 1000.0 + i,
         "vol_mult": float(i)} for i in range(20)]}
    assert len(T.top(st, now=1000.0)) == T.TOP_N
    assert len(T.live_rows(st, now=1000.0)) == 20


def test_a_coin_firing_twice_is_one_row_and_one_count():
    """The retain window can hold the same coin twice. The card renders one
    tile per SYMBOL, so counting raw rows made it claim 29 above 26 tiles."""
    st = {"recent": [
        {"symbol": "A/USDT:USDT", "base": "A", "fired_ts": 1000.0, "vol_mult": 4.0},
        {"symbol": "A/USDT:USDT", "base": "A", "fired_ts": 900.0, "vol_mult": 9.0},
        {"symbol": "B/USDT:USDT", "base": "B", "fired_ts": 950.0, "vol_mult": 5.0}]}
    v = T.web_view(st, now=1000.0)
    assert v["live_n"] == 2
    assert len(v["top"]) + len(v["rest"]) == 2
    # the NEWER fire wins, not the louder one
    a = next(r for r in v["top"] if r["base"] == "A")
    assert a["vol_mult"] == 4.0


def test_the_rows_say_whether_the_record_is_following_them(tmp_path,
                                                           monkeypatch):
    """The book holds 8 at once and declines the rest. A list of 23 with no
    note of that reads as '23 are being measured', which is the opposite of
    true — and is exactly what makes a paper record unreproducible."""
    import thrust_outcomes as TO
    monkeypatch.setattr(TO, "STORE_FILE", str(tmp_path / "t.json"))
    TO.save({"open": {"k": {"symbol": "S3/USDT:USDT"}}, "closed": [],
             "recent": []})
    st = {"recent": [
        {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "fired_ts": 1000.0 + i,
         "vol_mult": float(i)} for i in range(5)]}
    v = T.web_view(st, now=1000.0)
    flags = {r["base"]: r["tracked"] for r in v["top"] + v["rest"]}
    assert flags["S3"] is True
    assert all(not f for b, f in flags.items() if b != "S3")
    assert v["tracked_n"] == 1


def test_the_board_caps_and_says_it_capped():
    """This fires ~54x a day. A source that flags a third of the board would
    sit beside almost every coin and stop distinguishing anything."""
    st = {"recent": [
        {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "fired_ts": 1000.0 + i,
         "vol_mult": float(i)} for i in range(20)]}
    v = T.web_view(st, now=1000.0)
    assert len(v["top"]) == T.TOP_N
    assert v["live_n"] == 20
    assert v["hidden"] == 20 - T.TOP_N
    # strongest first
    assert v["top"][0]["vol_mult"] > v["top"][-1]["vol_mult"]


def test_an_unmeasured_multiple_does_not_sort_as_the_weakest():
    st = {"recent": [
        {"symbol": "A/USDT:USDT", "base": "A", "fired_ts": 1000.0, "vol_mult": None},
        {"symbol": "B/USDT:USDT", "base": "B", "fired_ts": 1001.0, "vol_mult": 9.0}]}
    top = T.top(st, now=1000.0)
    assert [r["base"] for r in top] == ["B", "A"]


def test_an_empty_board_has_no_latency_rather_than_zero():
    v = T.web_view({}, now=1000.0)
    assert v["age_median_s"] is None and v["age_max_s"] is None


def test_the_view_reports_what_the_cap_skipped():
    v = T.web_view({"scanned": 527, "confirmed": 40, "confirm_cap": 40,
                    "skipped_cap": 361}, now=1000.0)
    assert v["skipped_cap"] == 361 and v["confirmed"] == 40


def test_stale_rows_leave_the_board():
    old = 1000.0 - T.RETAIN_HOURS * 3600 - 1
    st = {"recent": [{"symbol": "A/USDT:USDT", "base": "A", "fired_ts": old,
                      "vol_mult": 9.0}]}
    assert T.web_view(st, now=1000.0)["top"] == []


# ── the verdict must travel with the coins ───────────────────────────────────
def test_the_measured_verdict_is_negative_on_both_counts():
    m = T.MEASURED
    assert m["hi"] < 0, "the per-signal interval no longer excludes zero"
    assert m["per_hour_hi"] < 0, "the per-hour interval no longer excludes zero"
    assert m["hours"] < m["n"], "clustering was not recorded"
    for _h, mean, median in m["fwd"][:3]:
        assert mean > 0 > median, "the mean/median disagreement was lost"
    # the counter-intuitive finding must survive
    shares = [x[0] for x in m["buy_share_ladder"]]
    exps = [x[1] for x in m["buy_share_ladder"]]
    assert shares == sorted(shares)
    assert exps == sorted(exps, reverse=True), \
        "the buy-share ladder no longer shows the gate making it worse"


def _tpl():
    with open(os.path.join(os.path.dirname(HERE), "templates", "index.html"),
              encoding="utf-8") as f:
        return f.read()


def _card():
    h = _tpl()
    seg = h[h.index('data-card="thrust"'):]
    return seg[:seg.index("</script>")]


def test_the_card_puts_the_verdict_above_the_coins():
    seg = _card()
    assert seg.index('id="thrust-verdict"') < seg.index('id="thrust-row"')


def test_the_verdict_says_measured_negative_not_merely_unproven():
    seg = _card()
    assert "實測是負的" in seg
    assert "不給進場價" in seg
    assert "per_hour_lo" in seg and "per_hour_hi" in seg, "no interval shown"


def test_the_card_admits_the_buy_share_finding():
    """The gate the request asked for is the one that costs the most. Hiding
    that would be reporting only the flattering half.

    Checked on the GUARD, not just on the strings: `if (false){ ... }` leaves
    every string in the file while rendering none of them.
    """
    seg = "".join(_card().split())          # whitespace-insensitive
    assert "if(m.buy_share_ladder&&m.buy_share_ladder.length){" in seg, \
        "the buy-share block is present but not reachable"
    assert "更差" in seg


def test_the_card_renders_every_row_it_counts():
    """`rest` shipping in the payload is not the same as the card drawing it."""
    seg = _card()
    assert "d.rest" in seg, "the card ignores the rows past the fold"
    assert "rest.map(tile)" in seg.replace(" ", "").replace(
        "rest.map(tile)", "rest.map(tile)") or "rest.map(tile)" in seg, \
        "the extra rows are fetched and never drawn"


def test_the_card_marks_which_rows_the_record_follows():
    """The book holds 8 and declines the rest, so a list of 23 without that
    note reads as '23 are being measured'."""
    seg = _card()
    assert "r.tracked" in seg, "the per-coin marker is gone"
    assert "紀錄只跟其中" in seg, "the card does not say how many are tracked"
    assert "max_concurrent" in seg, "the cap behind that number is not shown"


def test_the_card_shows_coverage_and_latency():
    seg = _card()
    assert "skipped_cap" in seg, "the cap is applied but never disclosed"
    assert "age_median_s" in seg, "the on-time claim is never evidenced"


def test_the_card_state_messages_survive_mobile():
    seg = _card()
    offenders = [ln.strip() for ln in seg.split("\n")
                 if "analytics-subtitle" in ln and 'id="thrust-sub"' not in ln]
    assert not offenders, f"invisible under 768px: {offenders}"


def test_it_is_registered_and_grouped():
    import dashboard_layout as D
    assert "thrust" in D.CARD_IDS
    D.assert_groups_cover_every_card()


def test_the_watchlist_knows_the_source():
    import daily_watch
    assert daily_watch.SRC_ZH.get("thrust")


def test_only_the_capped_slice_reaches_the_watchlist(tmp_path, monkeypatch):
    """54 hits a day beside every coin would break the breadth ranking, which
    is the one job the watchlist has."""
    import daily_watch as W
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "dw.json"))
    monkeypatch.setattr(T, "STATE_FILE", str(tmp_path / "thrust_state.json"))
    T.save_state({"recent": [
        {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "fired_ts": time.time(),
         "vol_mult": float(i)} for i in range(30)]})
    seen = [b for b, srcs in W._sightings().items() if "thrust" in srcs]
    assert len(seen) == T.TOP_N, f"{len(seen)} coins flagged, cap is {T.TOP_N}"


def test_an_unknown_multiple_is_not_written_as_flat(tmp_path, monkeypatch):
    import daily_watch as W
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "dw.json"))
    monkeypatch.setattr(T, "STATE_FILE", str(tmp_path / "thrust_state.json"))
    T.save_state({"recent": [{"symbol": "X/USDT:USDT", "base": "X",
                              "fired_ts": time.time(), "vol_mult": None}]})
    note = W._sightings()["X"]["thrust"]["note"]
    assert "0.0x" not in note and "不明" in note


def test_a_failed_write_does_not_destroy_the_existing_state(tmp_path,
                                                            monkeypatch):
    """Plain open(path, "w") TRUNCATES before it writes, so a crash mid-dump
    leaves a half-file that no longer parses. Two concurrent writers on a
    plain open() truncated a cache file during this session's own data
    collection — the write must land somewhere else and be renamed into place.
    """
    monkeypatch.setattr(T, "STATE_FILE", str(tmp_path / "s.json"))
    T.save_state({"recent": [{"symbol": "A", "base": "A", "fired_ts": 1.0}]})

    boom = json.dump

    def explode(obj, fh, **kw):
        fh.write('{"recent": [{"sym')          # partial, then die
        raise RuntimeError("disk full")

    monkeypatch.setattr(json, "dump", explode)
    T.save_state({"recent": [{"symbol": "B", "base": "B", "fired_ts": 2.0}]})
    monkeypatch.setattr(json, "dump", boom)

    with open(T.STATE_FILE, encoding="utf-8") as f:
        kept = json.load(f)
    assert kept["recent"][0]["base"] == "A", "a failed write destroyed the state"
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"
