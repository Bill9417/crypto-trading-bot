"""🌊 隧道翻多 — the detector, the record, and the claims the card makes.

The reference case is not decoration. The request came with one bar attached
(DASH/USDT, 2026-08-19 22:00 台北, the 1h bar that closes at 23:00), and a
detector that agrees with a backtest but not with the chart it was asked for
is answering a different question. That bar is checked here from a fixture, so
the test does not depend on a live exchange.
"""
import json
import os

import vegas_reclaim as V
import vegas_outcomes as O
import vegas_scan as S

HERE = os.path.dirname(os.path.abspath(__file__))


def _dash():
    """The real bar the request was built from: DASH/USDT 1h, 2026-08-19 22:00
    台北 (the bar that closes at 23:00). Real Binance candles, committed, so
    the reference case is checked without a network call."""
    with open(os.path.join(HERE, "fixtures", "dash_vegas_20260819.json"),
              encoding="utf-8") as f:
        return json.load(f)


def test_the_reference_bar_from_the_request_fires():
    """If this ever stops firing, the scanner and the chart it was asked for
    have parted company — which is the failure no backtest would show."""
    fx = _dash()
    rows = fx["rows"]
    sig = V.signal(rows[:-1])          # last row is the forming bar
    assert sig, "the DASH bar in the request does not fire"
    assert sig["ts"] == fx["fire_bar_ts"], "fired on a different bar"
    assert round(sig["vol_mult"], 1) == 4.4
    assert sig["reclaim"] and sig["turn"] and sig["buyer_bar"] and sig["surge"]


def _no_turn():
    """A real bar that reclaims the tunnel on a volume surge while EMA200 is
    still falling — the 16% of otherwise-qualifying bars the turn gate drops."""
    with open(os.path.join(HERE, "fixtures", "reclaim_no_turn.json"),
              encoding="utf-8") as f:
        return json.load(f)


def test_the_turn_gate_actually_rejects_a_red_ema200():
    """Three gates pass, EMA200 is still red, so it must not fire.

    The DASH fixture cannot see this gate — it fires with or without it — so a
    suite resting only on the reference case would let the turn gate be
    deleted silently.
    """
    rows = _no_turn()["rows"]
    r = V.read(rows[:-1])
    assert r["reclaim"] and r["buyer_bar"] and r["surge"], "fixture went stale"
    assert r["turn"] is False and r["established"] is False
    assert V.signal(rows[:-1]) == {}, "a red EMA200 fired anyway"
    assert "還沒翻正" in V.why_not(r)


def test_the_atr_floor_is_enforced_on_a_real_bar(monkeypatch):
    """The reference bar's ATR is 0.62%. Raise the floor above it and the same
    bar must stop firing; that is the only version of this check that can fail.
    """
    rows = _dash()["rows"]
    assert V.signal(rows[:-1]), "the fixture does not fire at the shipped floor"
    monkeypatch.setattr(V, "MIN_ATR_PCT", 0.80)
    assert V.signal(rows[:-1]) == {}, "the ATR floor is not enforced"
    monkeypatch.setattr(V, "MIN_ATR_PCT", 0.30)
    assert V.signal(rows[:-1]), "the floor rejects at its shipped value"


def test_the_reference_bar_is_the_only_one_in_its_history():
    """A detector that fires on 1 bar in 330 is selective; one that fires on
    50 is a volume alert wearing an EMA costume."""
    rows = _dash()["rows"]
    hits = [i for i in range(V.WARMUP, len(rows) - 1) if V.signal(rows[:i + 1])]
    assert len(hits) == 1, f"fired on {len(hits)} bars of one symbol's history"


def test_each_gate_alone_would_have_fired_more():
    """Names the reference bar's readings, so a gate silently becoming a
    no-op is visible here rather than only in aggregate."""
    rows = _dash()["rows"]
    r = V.read(rows[:-1])
    assert r["slope"] > 0 and r["vol_mult"] > V.VOL_MULT
    assert r["atr_pct"] < 0.67,         "the reference bar no longer sits under the old ATR floor"


# ── synthetic series ─────────────────────────────────────────────────────────
def _bars(n=400, start=100.0, drift=-0.05):
    """A falling series: EMA200 red, price under the tunnel."""
    rows, px = [], start
    for i in range(n):
        px = max(1.0, px + drift)
        rows.append([1_600_000_000_000 + i * 3_600_000,
                     px, px * 1.004, px * 0.996, px, 1000.0])
    return rows


def _turn_up(rows, bars=40, drift=1.2, vol=1000.0):
    """Bend the series upward hard enough to lift EMA200 and clear the tunnel."""
    px = rows[-1][4]
    t = rows[-1][0]
    for i in range(bars):
        px += drift
        t += 3_600_000
        rows.append([t, px - drift * 0.9, px * 1.004, px * 0.996, px, vol])
    return rows


def test_the_three_gates_must_all_be_true():
    rows = _turn_up(_bars(), 60)
    # A real fire needs a volume surge on the reclaim bar; the ramp above has
    # flat volume, so nothing may fire.
    assert V.signal(rows) == {}, "fired without a volume surge"


def test_a_reclaim_with_volume_and_a_turn_fires():
    fired = V.signal(_dash()["rows"][:-1])
    assert fired, "a full setup did not fire"
    assert fired["side"] == "long"
    assert fired["vol_mult"] >= V.VOL_MULT


def test_the_volume_gate_is_load_bearing():
    """Ablation says the volume gate is the only one worth +0.12R. Prove the
    code actually honours it rather than passing everything through."""
    fx = _dash()
    rows = [list(r) for r in fx["rows"]]
    rows[-2][5] = rows[-2][5] / 10.0          # same bar, ordinary volume
    assert V.signal(rows[:-1]) == {}, "the volume gate is not enforced"


def test_the_forming_candle_is_dropped():
    """consider() must ignore the last row — strategy2_scanner patches a live
    price onto it, and a reclaim confirmed on a moving bar un-confirms itself.
    """
    rows = _dash()["rows"]
    fire_at = len(rows) - 2
    # With the firing bar LAST it is the forming candle, so nothing may fire.
    assert V.consider("DASH/USDT:USDT", rows[:fire_at + 1], {}, 0) == {}
    # With one bar behind it, it is closed, and it fires.
    assert V.consider("DASH/USDT:USDT", rows[:fire_at + 2], {}, 0)


def test_vol_mult_is_none_not_zero_without_history():
    """A missing reading must not be written as a neutral default — 0.0x
    asserts 'volume did not surge' about a bar nobody measured."""
    rows = _bars(400)
    for r in rows:
        r[5] = 0.0
    read = V.read(rows)
    assert read["vol_mult"] is None, "a missing volume average became a number"
    assert read["surge"] is False


def test_an_established_uptrend_is_not_a_turn():
    rows = _turn_up(_bars(), 260)      # green for a long time by the end
    r = V.read(rows)
    assert r["turn"] is False, "an old uptrend counted as a fresh turn"
    assert r["established"] is True


def test_why_not_names_an_old_uptrend_separately_from_a_red_one():
    """'EMA200 還沒翻正' and 'EMA200 早就是綠的' are opposite situations and the
    scan's diagnostics are useless if they collapse into one message."""
    base = {"reclaim": True, "turn": False, "buyer_bar": True, "surge": True,
            "atr_pct": 5.0, "vol_mult": 9.0}
    assert "早就是綠的" in V.why_not({**base, "established": True})
    assert "還沒翻正" in V.why_not({**base, "established": False})


def test_the_measured_verdict_is_negative_and_says_so():
    """The card frames the signals with MEASURED. If the numbers ever go
    positive that is a real change and this test should be updated
    deliberately — not a thing that drifts silently."""
    m = V.MEASURED
    assert m["per_hour_hi"] < 0, "the per-hour interval no longer excludes zero"
    assert m["per_hour_lo"] < m["per_hour_r"] < m["per_hour_hi"]
    assert m["hours"] < m["n"], "clustering was not recorded"
    # mean positive, median negative — the pair is the whole point
    for _h, mean, median in m["fwd"]:
        assert mean > 0 > median, "the mean/median disagreement was lost"


def test_the_module_ships_no_trade_plan():
    """Observe-only is enforced by the payload, not by a comment."""
    sig = V.signal(_dash()["rows"][:-1])
    assert sig
    for banned in ("entry", "sl", "tp", "plan", "stop_pct", "rr"):
        assert banned not in sig, f"an observe-only signal shipped {banned}"


# ── the forward record ───────────────────────────────────────────────────────
def test_settle_enters_on_the_next_bar_not_the_signal_bar():
    """The signal is read at a bar's close, so that bar cannot fill it."""
    bar = 1_700_000_000_000
    rows = [[bar + i * 3_600_000, 10.0 + i, 11.0 + i, 9.0 + i, 10.0 + i, 1.0]
            for i in range(60)]
    row = {"bar_ts": bar, "symbol": "X"}
    out = O.settle(row, rows, 0)
    assert out["entry"] == rows[1][1], "entry was not the NEXT bar's open"


def test_settle_waits_for_the_longest_horizon():
    bar = 1_700_000_000_000
    short = [[bar + i * 3_600_000, 10.0, 11.0, 9.0, 10.0, 1.0] for i in range(20)]
    assert O.settle({"bar_ts": bar, "symbol": "X"}, short, 0) is None


def test_stats_report_median_beside_mean():
    store = O._blank()
    # one big winner, three small losers: mean positive, median negative
    for i, v in enumerate((30.0, -1.0, -1.0, -1.0)):
        store["closed"].append({"key": f"k{i}", "fwd_6h": v,
                                "fwd_24h": v, "fwd_48h": v})
    st = O.stats(store)
    h = st["horizons"][6]
    assert h["mean"] > 0 > h["median"], "the shape of the distribution was lost"
    assert h["n"] == 4


def test_an_empty_book_reports_none_not_zero():
    st = O.stats(O._blank())
    for h in O.HORIZONS:
        assert st["horizons"][h]["mean"] is None
        assert st["horizons"][h]["median"] is None


# ── the scan ─────────────────────────────────────────────────────────────────
class _Client:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def call(self, _m, sym, _tf, _since, limit):
        self.calls += 1
        return self.data[sym][-limit:]


def test_the_funnel_counts_rather_than_reading_zero():
    """The first gate is a cross EVENT, so on a normal hour every symbol fails
    it and a first-failure tally reads the same as a scanner returning False.
    The funnel must show survivors at each stage."""
    # A symbol that RECLAIMS but does not fire. Counting only the fired path
    # would leave the funnel reading 0/0/0/0 on every hour that produced no
    # signal — identical to a funnel that never counts, which is exactly the
    # ambiguity it exists to remove.
    data = {"B/USDT:USDT": _no_turn()["rows"]}
    st = {}
    out = S.scan(_Client(data), list(data), st, now=0)
    assert out["fired"] == 0
    assert out["funnel"]["reclaim"] == 1, "a survivor was not counted"
    assert out["funnel"]["turn"] == 0, "a rejected symbol counted as a survivor"

    # …and the fired path must count through every stage too.
    out2 = S.scan(_Client({"A/USDT:USDT": _dash()["rows"]}),
                  ["A/USDT:USDT"], {}, now=0)
    assert out2["fired"] == 1
    assert out2["funnel"]["surge"] == 1 and out2["funnel"]["atr"] == 1


def test_the_scan_reports_what_it_did_not_look_at():
    """A capped scan that says 'nothing fired' without saying what it covered
    is indistinguishable from a complete one."""
    data = {f"S{i}/USDT:USDT": _bars(400) for i in range(10)}
    st = {}
    out = S.scan(_Client(data), list(data), st, now=0, max_symbols=4)
    assert out["checked"] == 4
    assert out["universe"] == 10, "the cap was applied but never disclosed"
    v = S.web_view(st)
    assert v["scanned"] == 4 and v["universe"] == 10


def test_the_scan_view_does_not_lose_its_signals_to_the_outcome_book(tmp_path,
                                                                    monkeypatch):
    """Both dicts have a key called `recent` and they mean different things.

    Merging the outcome book's view LAST overwrote the scan's live signals
    with a list that stays empty until a signal has aged 48 hours — twelve
    live coins rendered as "過去 36 小時沒有符合的幣". Neither call site shows
    the collision, so it is pinned here.
    """
    monkeypatch.setattr(O, "STORE_FILE", str(tmp_path / "o.json"))  # empty book
    data = {"DASH/USDT:USDT": _dash()["rows"]}
    st = {}
    out = S.scan(_Client(data), list(data), st, now=0)
    assert out["fired"] == 1
    v = S.web_view(st)
    assert len(v["recent"]) == 1, "the live signal was lost merging the record"
    assert v["recent"][0]["base"] == "DASH"
    # …and the record half must still arrive.
    assert v["measured"] and v["params"] and v["live"] is not None


def test_is_due_gates_the_universe_build(tmp_path, monkeypatch):
    """The caller must be able to skip building the universe (a fetch_tickers
    call) on the eleven sweeps an hour that would discard it.

    Pointed at a tmp file: a test that writes the real vegas_state.json would
    make the live scanner think it had just run.
    """
    import time as _t
    monkeypatch.setattr(S, "STATE_FILE", str(tmp_path / "v.json"))
    now = _t.time()
    assert S.is_due(now) is True, "a never-run scan is not due"
    S.save({"ran_ts": now})
    assert S.is_due(now) is False, "a scan that just ran is due again"
    assert S.is_due(now + S.RUN_EVERY_SEC + 1) is True


def test_a_dead_symbol_does_not_fail_the_scan():
    class Boom(_Client):
        def call(self, m, sym, tf, since, limit):
            if sym == "B/USDT:USDT":
                raise RuntimeError("delisted")
            return super().call(m, sym, tf, since, limit)
    data = {"A/USDT:USDT": _bars(400), "B/USDT:USDT": _bars(400)}
    out = S.scan(Boom(data), list(data), {}, now=0)
    assert out["errors"] == 1 and out["checked"] == 1


def test_the_cooldown_stops_the_same_bar_firing_twice():
    rows = _dash()["rows"]
    st = {}
    assert V.consider("DASH/USDT:USDT", rows, st, 0)
    assert V.consider("DASH/USDT:USDT", rows, st, 60) == {}


def test_record_dedupes_on_the_bar_not_the_clock():
    store = O._blank()
    sig = {"symbol": "A/USDT:USDT", "base": "A", "ts": 123, "close": 1.0}
    assert O.record(sig, store, now_ts=0) is True
    assert O.record(sig, store, now_ts=999) is False, "the same bar recorded twice"


# ── the surfaces ─────────────────────────────────────────────────────────────
def _tpl(name):
    with open(os.path.join(os.path.dirname(HERE), "templates", name),
              encoding="utf-8") as f:
        return f.read()


def test_the_card_puts_the_verdict_above_the_signals():
    h = _tpl("index.html")
    seg = h[h.index('data-card="vegas"'):]
    seg = seg[:seg.index("</section>")]
    assert seg.index('id="vegas-verdict"') < seg.index('id="vegas-row"'), \
        "the verdict renders under the coins, where it reads as a disclaimer"


def test_the_verdict_says_observe_only_in_words():
    """Position alone is not the claim. A verdict box rendered above the coins
    that says something encouraging is worse than none, so the text that makes
    this observe-only is pinned here."""
    h = _tpl("index.html")
    seg = h[h.index('data-card="vegas"'):]
    seg = seg[:seg.index("})();", seg.index("function verdict"))]
    assert "只做觀察，不是進場訊號" in seg, "the card no longer says observe-only"
    assert "不給進場價" in seg, "the card no longer says why there is no plan"
    # …and it must show the interval rather than a bare number.
    assert "per_hour_lo" in seg and "per_hour_hi" in seg, \
        "the verdict quotes a point estimate with no interval"


def test_the_card_handles_a_404_without_claiming_the_market_is_quiet():
    h = _tpl("index.html")
    seg = h[h.index('data-card="vegas"'):]
    seg = seg[:seg.index("})();", seg.index("function load()"))]
    assert "if(!r.ok) return {http:r.status}" in seg.replace(" ", "").replace(
        "if(!r.ok)return{http:r.status}", "if(!r.ok) return {http:r.status}") \
        or "r.ok" in seg, "a failed fetch is not detected"
    assert "d.neterr" in seg, "a network error leaves 載入中 up forever"
    assert "/restart" in seg, "a 404 does not tell the user what to do"


def test_the_state_messages_survive_the_mobile_stylesheet():
    """This card renders every state message with its own style, not into
    .analytics-subtitle — belt and braces alongside the scoping fix below."""
    h = _tpl("index.html")
    seg = h[h.index('data-card="vegas"'):]
    seg = seg[:seg.index("</script>")]
    # The one legitimate use is the header line, which every card styles alike.
    offenders = [ln.strip() for ln in seg.split("\n")
                 if "analytics-subtitle" in ln and 'id="vegas-sub"' not in ln]
    assert not offenders, f"invisible on mobile: {offenders}"


def test_the_mobile_rule_hides_headers_not_state_messages():
    """Under 768px the dashboard used to hide EVERY .analytics-subtitle — the
    same class the cards render their state messages into.

    Measured in a real browser at 430px: the watch card's
    '這個功能還沒生效 —— 網站程式要重啟才會有（/restart）' resolved to
    display:none, height:0. The text was in the DOM and invisible on the only
    device this dashboard is read on, so the card looked like an unexplained
    blank — which is precisely what that message exists to prevent. Seven
    cards were affected.

    The declutter it was written for is the explanatory line under each panel
    TITLE, so that is what the selector must target.
    """
    import re
    h = _tpl("index.html")
    hiding = [sel.strip() for sel, body in
              re.findall(r"([^{}]+)\{([^}]*)\}", h)
              if "analytics-subtitle" in sel and re.search(r"display:\s*none", body)]
    assert hiding, "the mobile declutter rule vanished entirely"
    for sel in hiding:
        assert "analytics-head" in sel, \
            f"'{sel}' hides state messages, not just panel headers"


def test_the_card_shows_median_beside_mean():
    """Both halves must be RENDERED, not merely mentioned. Asserting on the
    word alone passes while the template prints the mean twice — the prose
    around it says 中位數 alone."""
    h = _tpl("index.html")
    seg = h[h.index('data-card="vegas"'):]
    seg = seg[:seg.index("})();", seg.index("function record"))]
    # The exact emitting expressions, not the bare index. `f[2]` also appears
    # in the sign test next to it, so checking for the substring alone passes
    # while the template renders the mean into the median's slot.
    for expr in ("num(f[1],2)", "num(f[2],2)",       # backtest mean, median
                 "num(v.mean,2)", "num(v.median,2)"):  # live mean, median
        assert expr in seg, f"the card does not render {expr}"


def test_the_watchlist_knows_the_source():
    import daily_watch
    assert daily_watch.SRC_ZH.get("vegas"), "the watchlist would print a raw key"


def test_a_vegas_signal_reaches_the_observation_list(tmp_path, monkeypatch):
    """The whole point of the request: these coins land in 每日觀察清單.

    Reads the SCAN state, not the outcome book — the book holds a signal only
    while it waits for its 48h horizon, so sourcing the watchlist from it
    would drop each coin the moment it became measurable.
    """
    import daily_watch as W
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "daily_watch.json"))
    (tmp_path / "vegas_state.json").write_text(json.dumps({
        "recent": [{"base": "DASH", "symbol": "DASH/USDT:USDT",
                    "vol_mult": 4.413, "fired_ts": 1}]}), encoding="utf-8")
    sightings = W._sightings()
    assert "DASH" in sightings, "a fired signal never reached the watchlist"
    v = sightings["DASH"]["vegas"]
    assert v["side"] == "long"
    assert "4.4x" in v["note"]


def test_an_unknown_volume_is_not_written_as_flat(tmp_path, monkeypatch):
    """vol_mult is None when there was no volume history. Rendering that as
    '量 0.0x' would tell the watchlist the bar was quiet — the opposite of
    what fired it."""
    import daily_watch as W
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "daily_watch.json"))
    (tmp_path / "vegas_state.json").write_text(json.dumps({
        "recent": [{"base": "X", "symbol": "X/USDT:USDT",
                    "vol_mult": None, "fired_ts": 1}]}), encoding="utf-8")
    note = W._sightings()["X"]["vegas"]["note"]
    assert "0.0x" not in note
    assert "不明" in note


def test_the_card_is_registered_and_grouped():
    import dashboard_layout as D
    assert "vegas" in D.CARD_IDS
    D.assert_groups_cover_every_card()
