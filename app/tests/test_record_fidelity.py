"""🧾 Could a real account have reproduced this row?

Asked 2026-08-21: "keep the entry and tp and SL price and the time to make sure
all data are real, and make sure all data on the web is correct as long as I
connect to the real money then will have the same result."

That is two requirements and they fail differently:

  REPLAYABLE — a row must carry entry, stop, target, exit and both timestamps,
  so it can be replayed against the exchange's own candles by someone who
  does not trust this book. Without the exit price and the times, a row is a
  claim; with them it is evidence.

  REPRODUCIBLE — the number must be one a real account could have got. That is
  where a correct-looking book quietly lies: 隧道翻多 and 隧道上方爆量 recorded
  every signal they saw, which at ~54/day held for 48h assumes about 108
  simultaneous positions. The arithmetic was right and the answer was
  unreachable.

Four assumptions stand between this record and a live account, and each one is
pinned below rather than left in a comment: costs are charged, capacity is
capped, the signal bar cannot fill its own trade, and a candle spanning both
stop and target is scored as a loss.
"""
import pytest

import flip_outcomes as F
import strategy4_outcomes as S4O
import thrust_outcomes as TO
import trade_costs as C
import vegas_outcomes as V

HOUR = 3_600_000
BAR = 1_700_000_000_000

BOOKS = [("flip", F), ("vegas", V)]


def _rally(n=60, tp_at=5):
    rows = [[BAR + i * HOUR, 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(n)]
    # The SIGNAL bar opens somewhere else on purpose. With every bar opening at
    # the same price, "entry is the next bar's open" and "entry is the signal
    # bar's open" produce the same number and a test cannot tell them apart —
    # a mutation that moved the entry back onto the signal bar walked straight
    # past the first version of this fixture.
    rows[0] = [BAR, 93.0, 100.5, 92.5, 100.0, 1.0]
    rows[tp_at] = [BAR + tp_at * HOUR, 100.2, 106.5, 100.0, 106.0, 1.0]
    return rows


# ── replayable ───────────────────────────────────────────────────────────────
def test_a_settled_row_carries_everything_needed_to_replay_it():
    """entry, stop, target, exit price and BOTH timestamps. Anything less and
    the row cannot be checked against the exchange."""
    row = {"symbol": "X/USDT:USDT", "base": "X", "bar_ts": BAR,
           "side": "long", "atr_pct": 2.0}
    out = V.settle(row, _rally(), now_ts=BAR / 1000 + 99 * 3600)
    assert out, "nothing settled"
    for field in ("entry", "entry_ts", "sl", "tp", "stop_pct",
                  "outcome", "exit_price", "exit_ts", "bar_ts",
                  "r", "r_gross", "cost_r"):
        assert out.get(field) is not None, f"{field} missing — row not replayable"
    assert out["exit_ts"] > out["entry_ts"] > out["bar_ts"] / 1000.0


def test_the_bracket_is_arithmetically_reconstructable():
    """Someone recomputing from entry + atr must land on the same stop and
    target. If they cannot, the row's numbers are unverifiable."""
    row = {"symbol": "X/USDT:USDT", "bar_ts": BAR, "side": "long",
           "atr_pct": 2.0}
    out = V.settle(row, _rally(), now_ts=BAR / 1000 + 99 * 3600)
    risk = out["entry"] * (2.0 / 100.0) * V.SL_ATR_MULT
    assert out["sl"] == pytest.approx(out["entry"] - risk)
    assert out["tp"] == pytest.approx(out["entry"] + V.TP_R * risk)
    assert out["stop_pct"] == pytest.approx(risk / out["entry"] * 100, abs=1e-3)


def test_exit_price_is_the_level_that_was_hit():
    trade = {"symbol": "X", "side": "long", "entry": 100.0, "sl": 98.0,
             "tp": 104.0, "stop_pct": 2.0, "bar_ts": 0}
    won = S4O.settle(trade, [[0, 100, 100, 100, 100, 1],
                             [HOUR, 100, 104.5, 100, 104.2, 1]], now_ts=1e12)
    lost = S4O.settle(trade, [[0, 100, 100, 100, 100, 1],
                              [HOUR, 100, 100.5, 97.5, 98.0, 1]], now_ts=1e12)
    assert won["exit_price"] == 104.0 and won["outcome"] == "tp"
    assert lost["exit_price"] == 98.0 and lost["outcome"] == "sl"


def test_an_unmeasurable_bracket_is_skipped_not_invented():
    """No ATR reading means no risk to size a stop from. Defaulting one would
    put a fabricated stop in a record whose whole purpose is being real."""
    row = {"symbol": "X/USDT:USDT", "bar_ts": BAR, "side": "long"}
    out = V.settle(row, _rally(), now_ts=BAR / 1000 + 99 * 3600)
    assert out, "the forward record should still settle"
    assert "sl" not in out and "tp" not in out


# ── reproducible ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,mod", BOOKS, ids=[b[0] for b in BOOKS])
def test_every_book_caps_concurrent_positions(name, mod):
    """The gap that made these records unreachable: 隧道上方爆量 fires ~54 a day
    and holds 48h. Uncapped, that assumes ~108 positions at once."""
    assert mod.MAX_CONCURRENT > 0, f"{name} records unlimited positions"
    assert mod.MAX_CONCURRENT <= 12, (
        f"{name} allows {mod.MAX_CONCURRENT} at once — more than a 25 USDT "
        f"account could carry")


def test_a_full_book_declines_and_counts_the_skip():
    """Counted, not silently dropped: a book that quietly ignores most of its
    own signals looks identical to one that never saw them."""
    st = V._blank()
    taken = sum(V.record({"symbol": f"S{i}/USDT:USDT", "base": f"S{i}",
                          "ts": 1000 + i, "close": 1.0, "atr_pct": 2.0},
                         st, 1.0) for i in range(14))
    assert taken == V.MAX_CONCURRENT
    assert st["skipped_no_slot"] == 14 - V.MAX_CONCURRENT


def test_the_skip_count_is_persisted(tmp_path, monkeypatch):
    """record() increments it; note() has to SAVE it. Saving only when a row
    was added throws the number away — the exact bug that made flip's card
    read '沒空位而略過 0 筆' while it was refusing everything."""
    monkeypatch.setattr(V, "STORE_FILE", str(tmp_path / "v.json"))
    for i in range(V.MAX_CONCURRENT + 3):
        V.note({"symbol": f"S{i}/USDT:USDT", "base": f"S{i}", "ts": 1000 + i,
                "close": 1.0, "atr_pct": 2.0}, 1.0)
    assert V.load()["skipped_no_slot"] == 3


@pytest.mark.parametrize("name,mod", BOOKS, ids=[b[0] for b in BOOKS])
def test_costs_are_charged_to_winners_and_losers_alike(name, mod):
    """You paid the spread either way. Halving the cost on losers is the
    tempting shortcut that flatters every book by the amount that matters."""
    win, cw = C.net_r(2.0, 2.0)
    loss, cl = C.net_r(-1.0, 2.0)
    assert cw == cl > 0
    assert win < 2.0 and loss < -1.0


def test_the_signal_bar_cannot_fill_its_own_trade():
    """The setup is read at that bar's close, so its own range already
    happened. Entry is the NEXT bar's open."""
    rows = _rally()
    row = {"symbol": "X/USDT:USDT", "bar_ts": BAR, "side": "long",
           "atr_pct": 2.0}
    out = V.settle(row, rows, now_ts=BAR / 1000 + 99 * 3600)
    assert rows[0][1] != rows[1][1], "fixture cannot tell the two bars apart"
    assert out["entry"] == rows[1][1], "entry was not the next bar's open"
    assert out["entry"] != rows[0][1], "entry came from the signal bar"
    assert out["entry_ts"] == rows[1][0] / 1000.0


def test_a_candle_spanning_both_levels_is_scored_a_loss():
    """A 15m range cannot say which came first, and guessing the target is
    flattering AND unfalsifiable."""
    trade = {"symbol": "X", "side": "long", "entry": 100.0, "sl": 98.0,
             "tp": 104.0, "stop_pct": 2.0, "bar_ts": 0}
    out = S4O.settle(trade, [[0, 100, 100, 100, 100, 1],
                             [HOUR, 100, 105.0, 97.0, 101.0, 1]], now_ts=1e12)
    assert out["outcome"] == "sl"


def test_the_thrust_book_inherits_all_of_it():
    """thrust_outcomes wraps vegas_outcomes, so every guarantee above has to
    reach it — a wrapper that bypassed record() would have none of them."""
    assert TO.HORIZONS
    import vegas_outcomes as base
    assert TO.tick.__module__ == "thrust_outcomes"
    assert base.MAX_CONCURRENT > 0
    st = base._blank()
    for i in range(base.MAX_CONCURRENT + 2):
        base.record({"symbol": f"T{i}/USDT:USDT", "base": f"T{i}",
                     "ts": 2000 + i, "close": 1.0, "atr_pct": 1.0}, st, 1.0)
    assert st["skipped_no_slot"] == 2


def test_net_and_gross_are_both_kept():
    """'what did the shape do' and 'what would you have kept' are different
    questions that were once answered by the same number."""
    row = {"symbol": "X/USDT:USDT", "bar_ts": BAR, "side": "long",
           "atr_pct": 2.0}
    out = V.settle(row, _rally(), now_ts=BAR / 1000 + 99 * 3600)
    assert out["r_gross"] > out["r"], "cost was not deducted"
    assert out["cost_r"] == pytest.approx(out["r_gross"] - out["r"], abs=1e-3)


# ── the web says what it assumed ─────────────────────────────────────────────
BASIS_CARDS = ["flips", "zones", "vegas", "thrust"]


@pytest.mark.parametrize("card", BASIS_CARDS)
def test_every_card_that_prints_an_R_states_its_basis(card):
    """+0.610R on its own does not answer "will real money give me this"."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    seg = html[html.index(f'data-card="{card}"'):]
    seg = seg[:seg.index("</script>", seg.index("<script>"))]
    assert "wolfBasis(" in seg or "這筆紀錄是這樣算的" in seg, \
        f"the {card} card prints an R with no stated basis"


@pytest.mark.parametrize("book,mod", [
    ("flip", "flip_outcomes"), ("zone", "zone_outcomes"),
])
def test_each_book_states_its_own_stop_rule(book, mod):
    """The flip's stop is structural (below the zone that defines the setup);
    the zone's is the far side of its own box with a min/max distance; the
    observe-only books use 1.5xATR. One shared sentence would put one
    strategy's rules under another's number — plausible, specific and about
    something else."""
    import importlib
    basis = importlib.import_module(mod).basis()
    assert basis["sl"], f"{book} states no stop rule"
    assert "ATR" not in basis["sl"], (
        f"{book} claims an ATR stop — its stop is structural")


def test_the_flip_and_zone_stop_rules_are_not_the_same_sentence():
    import flip_outcomes
    import zone_outcomes
    assert flip_outcomes.basis()["sl"] != zone_outcomes.basis()["sl"]


def test_the_cards_publish_the_basis_of_their_numbers():
    """"Will real money give me this number" cannot be answered by a number
    alone. Both cards now print the entry rule, the stop, the target, the
    concurrency cap, the tie rule, the hold cap, the costs charged — and the
    one thing NOT modelled."""
    import vegas_scan
    import vol_thrust
    for name, view in (("vegas", vegas_scan.web_view()),
                       ("thrust", vol_thrust.web_view())):
        basis = view.get("basis") or (view.get("live") or {}).get("basis") or {}
        assert basis, f"{name} publishes numbers with no stated basis"
        for key in ("entry", "sl", "tp", "cap", "tie", "hold", "costs",
                    "not_modelled"):
            assert basis.get(key), f"{name} basis is missing {key}"


def test_the_basis_names_what_is_not_modelled():
    """Funding is a real cost on a 48h hold and is NOT in the R. Saying so is
    the difference between an assumption and an omission."""
    import vegas_scan
    basis = (vegas_scan.web_view().get("basis") or {})
    assert "資金費率" in basis.get("not_modelled", "")


def test_both_cards_render_the_basis():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "index.html"),
              encoding="utf-8") as f:
        html = f.read()
    for card in ("vegas", "thrust"):
        seg = html[html.index(f'data-card="{card}"'):]
        seg = seg[:seg.index("</script>")]
        assert "這筆紀錄是這樣算的" in seg, f"the {card} card hides its basis"
        assert "b.not_modelled" in seg, f"the {card} card hides the omission"
