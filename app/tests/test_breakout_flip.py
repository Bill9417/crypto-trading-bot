"""🚀 Resistance→support flip — mostly a test suite about lookahead.

The shape is easy to describe and easy to detect WRONG in a way that flatters
it enormously, because every mistake available here involves using information
that had not printed yet. A swing high that becomes a pivot three bars later, a
forming candle whose high is still moving, a ceiling that only exists in
hindsight — each one makes the detector look prescient.

So most of what follows is not "does it find the pattern" but "does it refuse
to use the future while finding it".
"""
import pytest

import breakout_flip as B


def bars(seq, start_ts=1_000_000_000_000, step=900_000):
    """[[ts,o,h,l,c,v]] from (high, low, close) or flat prices."""
    out = []
    for i, s in enumerate(seq):
        if isinstance(s, (int, float)):
            h = l = c = float(s)
        else:
            h, l, c = (float(x) for x in s)
        out.append([start_ts + i * step, c, h, l, c, 100.0])
    return out


def flat(n, price=100.0):
    return [price] * n


def ramp(n, a, b):
    return [a + (b - a) * i / max(1, n - 1) for i in range(n)]


# ── pivots and the future ───────────────────────────────────────────────────
def test_a_swing_high_does_not_exist_until_it_is_confirmed():
    """THE lookahead trap. A pivot at bar i needs `right` bars to its right; it
    is not knowable at bar i. Drawing resistance from unconfirmed pivots means
    using bars that had not printed when the decision was made."""
    highs = [1, 2, 3, 9, 3, 2, 1]          # peak at index 3
    assert swing_idx(B.swing_highs(highs, 3, 3)) == [3]
    # at bar 3 the peak has no right-hand bars yet — invisible
    assert B.swing_highs(highs, 3, 3, known_by=3) == []
    assert B.swing_highs(highs, 3, 3, known_by=5) == []
    assert swing_idx(B.swing_highs(highs, 3, 3, known_by=6)) == [3]


def swing_idx(pivots):
    return [i for i, _ in pivots]


def test_overhead_is_judged_only_on_what_had_printed():
    """A high made AFTER the decision cannot retroactively be the ceiling that
    was in the way — that is hindsight dressed as risk management."""
    highs = flat(40, 100.0) + [130.0] + flat(10, 100.0)
    early = B.overhead_room(highs, 105.0, upto=35)
    assert early["blue_sky"], "a future spike was counted as present resistance"
    late = B.overhead_room(highs, 105.0, upto=len(highs) - 1)
    assert not late["blue_sky"] and late["nearest"] == 130.0


# ── zones ───────────────────────────────────────────────────────────────────
def test_nearby_highs_are_one_zone_not_several():
    """A wall hit at 100.0 and 100.5 is one wall. Split into two one-touch
    levels, neither would clear MIN_TOUCHES and real resistance would be
    invisible to the detector."""
    z = B.cluster_zones([(5, 100.0), (20, 100.5), (40, 100.2)], tol_pct=0.8)
    assert len(z) == 1
    assert z[0]["touches"] == 3
    assert z[0]["top"] == 100.5


def test_far_apart_highs_stay_separate():
    z = B.cluster_zones([(5, 100.0), (20, 130.0)], tol_pct=0.8)
    assert len(z) == 2


def test_zones_are_ranked_by_how_often_they_rejected_price():
    z = B.cluster_zones([(1, 100.0), (9, 100.3), (5, 130.0)], tol_pct=0.8)
    assert z[0]["touches"] == 2 and abs(z[0]["price"] - 100.15) < 0.1


# ── the four conditions ─────────────────────────────────────────────────────
def _setup(break_close=103.0, retest_low=100.2, retest_close=101.5,
           after=(102.0, 102.5), overhead=None):
    """Two rejections at 100, a break, a retest that holds, then drift."""
    seq = []
    seq += [(99, 97, 98)] * 12
    seq += [(100.0, 98, 99)]                       # touch 1
    seq += [(99, 96, 97)] * 8
    seq += [(100.2, 98, 99)]                       # touch 2
    seq += [(99, 96, 97)] * 8
    seq += [(break_close + .2, 99, break_close)]   # BREAK
    seq += [(break_close, 101.5, 102.0)] * 2
    seq += [(102.5, retest_low, retest_close)]     # RETEST into the zone
    seq += [(c + .3, c - .5, c) for c in after]
    # 30 bars of preamble, not 20: detect() requires 60 bars of history before
    # it will decide anything, and the first fixture was 56 — so every
    # condition passed in isolation and detect() still returned {}. A fixture
    # shorter than the minimum tests the minimum, not the pattern.
    pre = [(90.5, 89.5, 90)] * 30
    if overhead is not None:
        # The ceiling must sit where a pivot can actually FORM — index >= 3,
        # with lower highs on both sides. Parked at index 0 it is invisible to
        # swing_highs (which needs `left` bars behind it), so the test passed a
        # ceiling the detector could never have seen and proved nothing.
        pre = ([(90.5, 89.5, 90)] * 5
               + [(overhead, overhead - 1, overhead - 0.5)]
               + [(90.5, 89.5, 90)] * 24)
    return bars(pre + seq)


def test_the_full_shape_is_detected():
    sig = B.detect(_setup())
    assert sig, "the canonical break→retest→hold was not found"
    assert sig["touches"] >= 2
    assert sig["zone_top"] == pytest.approx(100.2, abs=0.3)
    assert sig["blue_sky"] is True


def test_a_wick_through_the_zone_is_not_a_break():
    """The most common outcome at real resistance: a spike through and a close
    back inside. Counting it makes the detector fire on every failed attempt."""
    seq = bars([(90.5, 89.5, 90)] * 20
               + [(99, 97, 98)] * 12 + [(100.0, 98, 99)] + [(99, 96, 97)] * 8
               + [(100.2, 98, 99)] + [(99, 96, 97)] * 8
               + [(103.0, 98, 99.5)]                  # wick above, close BELOW
               + [(99.8, 98, 99)] * 6)
    assert not B.detect(seq)


def test_a_break_with_no_retest_is_not_the_setup():
    """This is a breakout, not a flip. The request was specifically about the
    zone being retested and HOLDING — that is the confirmation."""
    seq = bars([(90.5, 89.5, 90)] * 20
               + [(99, 97, 98)] * 12 + [(100.0, 98, 99)] + [(99, 96, 97)] * 8
               + [(100.2, 98, 99)] + [(99, 96, 97)] * 8
               + [(103, 99, 102.5)]
               + [(c + 1, c, c + .5) for c in ramp(8, 103, 112)])   # never returns
    assert not B.detect(seq)


def test_a_retest_that_closes_back_inside_the_zone_is_a_failure_not_a_flip():
    """The zone holding IS the premise. Closing back inside means the level
    rejected price again — the opposite of the setup."""
    assert not B.detect(_setup(retest_low=99.0, retest_close=99.2))


def test_price_falling_through_the_zone_ends_the_setup():
    """Once it has closed back below the zone bottom, a later touch is a new
    situation, not a delayed retest of the old break."""
    seq = bars([(90.5, 89.5, 90)] * 20
               + [(99, 97, 98)] * 12 + [(100.0, 98, 99)] + [(99, 96, 97)] * 8
               + [(100.2, 98, 99)] + [(99, 96, 97)] * 8
               + [(103, 99, 102.5)]
               + [(101, 95, 96)]                       # collapses through
               + [(100.5, 99, 100.4)] * 4)
    assert not B.detect(seq)


def test_a_ceiling_just_overhead_disqualifies_it():
    """Condition 4, and the whole point of the request. The same break into the
    middle of an old range has somewhere to stop; into blue sky it does not."""
    with_ceiling = _setup(overhead=104.0)
    assert not B.detect(with_ceiling), "fired with resistance 2% overhead"


def test_a_distant_ceiling_still_counts_as_clear():
    assert B.detect(_setup(overhead=140.0))


def test_a_stale_flip_is_not_reported_forever():
    """A flip from 30 bars ago is history, not a setup. Without this the
    scanner re-alerts the same event until the shape decays."""
    base = _setup()
    stretched = base + bars([(103, 102, 102.5)] * 20,
                            start_ts=base[-1][0] + 900_000)
    assert not B.detect(stretched)


def test_one_touch_is_a_high_not_resistance():
    seq = bars([(90.5, 89.5, 90)] * 20
               + [(99, 97, 98)] * 12 + [(100.0, 98, 99)]      # ONE touch
               + [(99, 96, 97)] * 8
               + [(103, 99, 102.5)] + [(103, 101.5, 102)] * 2
               + [(102.5, 100.2, 101.5)] + [(102, 101, 101.8)] * 2)
    assert not B.detect(seq, min_touches=2)


# ── decisions are made on closed bars only ──────────────────────────────────
def test_the_detector_never_reads_past_the_bar_it_is_asked_about():
    """`at` must be a hard wall. If a signal at bar N changes depending on what
    bars N+1.. contain, the whole measurement below is fiction."""
    seq = _setup()
    n = len(seq) - 1
    truth = B.detect(seq, at=n)
    # append wildly different futures; the verdict at n must not move
    for tail in ([(500, 400, 450)] * 5, [(1, 0.5, 0.6)] * 5):
        assert B.detect(seq + bars(tail, start_ts=seq[-1][0] + 900_000),
                        at=n) == truth


def test_too_little_history_returns_nothing_rather_than_guessing():
    assert B.detect(bars(flat(20))) == {}
    assert B.detect([]) == {}


# ── the plan ────────────────────────────────────────────────────────────────
def test_the_stop_sits_below_the_flipped_zone():
    """The zone holding is the premise, so a close back inside it means the
    idea was wrong — not that it needs more room. A percentage stop chosen to
    flatter the R would be a different trade wearing this one's name."""
    sig = B.detect(_setup())
    pl = B.plan(sig)
    assert pl["sl"] < sig["zone_bottom"]
    assert pl["entry"] > sig["zone_top"]
    assert pl["side"] == "long"


def test_the_target_is_two_r_from_a_structural_stop():
    sig = B.detect(_setup())
    pl = B.plan(sig)
    risk = pl["entry"] - pl["sl"]
    assert pl["tp"] == pytest.approx(pl["entry"] + 2 * risk, rel=1e-6)


def test_an_impossible_plan_is_refused_not_fudged():
    assert B.plan({"price": 100.0, "zone_bottom": 105.0, "zone_top": 106.0}) == {}
    assert B.plan({}) == {}
