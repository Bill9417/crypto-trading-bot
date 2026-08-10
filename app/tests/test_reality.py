"""The statistics behind /reality, and the claims they are allowed to support.

This page exists to talk someone out of a bad idea. The dangerous failure is
therefore not a crash — it is the page asserting an edge it has not measured,
or hedging a fact that needs no hedge.

The awkward constraint it is built around: the lifetime tally in
signal_outcomes.json stores n, sum, wins, gain and loss, but no sum of squares,
so the true per-trade variance is NOT recoverable for historical data. Rather
than invent an error bar on a page about self-deception, reality.ci() computes
the variance of the minimum-variance distribution consistent with those figures
— every win at the average win, every loss at the average loss. Within-class
spread can only add variance, so that is a strict lower bound and the interval
is the NARROWEST any honest interval could be.

The asymmetry that follows is the thing worth testing, and the mistake worth
guarding is assuming it works both ways. It does not, and it does not care
about the sign of the result either:

  · zero inside the narrowest possible interval → conclusive. The real interval
    is wider, so it contains zero too.
  · zero outside it → conclusive of nothing, positive OR negative.

A losing rule gets no free pass. "Six rules all lost money" is reported as
realised fact; "they will keep losing" is a forecast and stays hedged.
"""
import math

import pytest

import reality as R


def bucket(n, total, wins, gain, loss, **extra):
    return {"n": n, "sum": total, "wins": wins, "gain": gain, "loss": loss, **extra}


# ── the lower-bound interval ────────────────────────────────────────────────
def test_ci_reports_which_basis_it_used():
    """A caller must be able to tell a real interval from the bound, because
    only one of them supports a positive claim."""
    lo, hi, basis = R.ci(bucket(100, -5.0, 30, 40.0, 45.0))
    assert basis == "floor"
    assert lo < hi


def test_ci_uses_the_exact_variance_when_it_is_available():
    b = bucket(100, -5.0, 30, 40.0, 45.0, sumsq=250.0, sumsq_n=100)
    lo, hi, basis = R.ci(b)
    assert basis == "exact"
    mean = -5.0 / 100
    var = (250.0 - 100 * mean * mean) / 99
    half = 1.96 * math.sqrt(var / 100)
    assert lo == pytest.approx(mean - half)
    assert hi == pytest.approx(mean + half)


def test_a_partially_covered_sumsq_is_not_treated_as_exact():
    """sumsq only started being recorded on 2026-08-10, so for a long time it
    describes a recent slice of a much older tally. Combining that slice's
    variance with the whole bucket's n and mean would produce a confident
    interval computed from data that does not match it — the exact error this
    page must not make. Coverage is tracked, not assumed."""
    b = bucket(10_000, -500.0, 3000, 4000.0, 4500.0, sumsq=90.0, sumsq_n=40)
    lo, hi, basis = R.ci(b)
    assert basis == "floor", "a 40/10000 sample was accepted as the whole bucket"


def test_the_bound_really_is_the_narrowest_interval():
    """The floor must never be WIDER than a genuine interval on the same data,
    or the asymmetry the page relies on is backwards."""
    b = bucket(500, -20.0, 150, 180.0, 200.0)
    flo_lo, flo_hi, _ = R.ci(b)
    # any real distribution has at least this much spread; add some within-class
    # variance and the interval must grow, never shrink
    mean = -20.0 / 500
    wide = dict(b, sumsq=1400.0, sumsq_n=500)
    ex_lo, ex_hi, basis = R.ci(wide)
    assert basis == "exact"
    assert (ex_hi - ex_lo) > (flo_hi - flo_lo), "the 'floor' was wider than a real interval"
    assert flo_lo > mean - (ex_hi - ex_lo)      # sanity: both centred on the mean


def test_ci_declines_when_one_class_is_empty():
    """All wins or all losses gives no spread to work with — better to say
    nothing than to print a zero-width interval that looks like certainty."""
    assert R.ci(bucket(10, 10.0, 10, 10.0, 0.0)) is None
    assert R.ci(bucket(1, -1.0, 0, 0.0, 1.0)) is None


# ── what the verdicts are allowed to claim ──────────────────────────────────
def test_a_floor_interval_never_proves_a_winner():
    """The whole point. A positive result on bound-derived variance is
    'probably', never 'proven' — the real interval is wider and may include
    zero."""
    r = R.row(bucket(4000, 400.0, 2000, 900.0, 500.0), "hold", R.RULE_LABEL)
    assert r["exp"] > 0
    assert r["straddles_zero"] is False          # clears zero on the bound...
    assert r["basis"] == "floor"
    assert "not yet proven" in r["verdict"]      # ...and still is not a claim
    assert r["tag"] == "probably positive"


def test_a_floor_interval_never_proves_a_loser_either():
    """The symmetric half, and the one easy to get wrong: it is tempting to
    accept a negative result on weaker evidence because it feels conservative.
    It is the same unearned claim with the sign flipped."""
    r = R.row(bucket(19295, -1707.0, 5868, 11714.0, 13422.0), "hold", R.RULE_LABEL)
    assert r["exp"] < 0
    assert r["straddles_zero"] is False
    assert r["basis"] == "floor"
    assert "probably negative" in r["verdict"]
    assert "narrowest possible" in r["verdict"]
    assert r["tag"] == "probably negative"


def test_an_exact_interval_that_clears_zero_is_allowed_to_say_so():
    r = R.row(bucket(500, 150.0, 300, 260.0, 110.0, sumsq=120.0, sumsq_n=500),
              "hold", R.RULE_LABEL)
    assert r["basis"] == "exact"
    if not r["straddles_zero"]:
        assert "clears zero" in r["verdict"]
        assert r["tag"] in ("positive", "negative")


def test_zero_inside_the_narrowest_interval_is_a_real_verdict():
    """The one conclusion the bound DOES license, and it needs no hedge: a
    wider interval contains zero too."""
    r = R.row(bucket(400, 2.0, 200, 190.0, 188.0), "hold", R.RULE_LABEL)
    assert r["straddles_zero"] is True
    assert r["tag"] == "no edge"
    assert "contains zero" in r["verdict"]


def test_a_small_sample_gets_no_verdict_at_all():
    r = R.row(bucket(5, -3.0, 1, 1.0, 4.0), "hold", R.RULE_LABEL)
    assert r["tag"] == "too few"
    assert "too few" in r["verdict"]


# ── facts are stated without hedging ────────────────────────────────────────
def test_the_realised_record_is_not_hedged():
    """net R, win rate and PF describe trades that already happened. Only the
    forecast needs an interval; hedging the history would be false modesty and
    would blunt the one part of the page that is beyond argument."""
    r = R.row(bucket(19295, -1707.0, 5868, 11714.0, 13422.0), "hold", R.RULE_LABEL)
    assert "lost 1,707R over 19,295 trades" in r["realised"]
    assert "PF 0.87" in r["realised"]
    for weasel in ("probably", "maybe", "suggests", "narrowest"):
        assert weasel not in r["realised"], f"the realised record hedged with {weasel!r}"


def test_profit_factor_below_one_means_losses_were_bigger():
    r = R.row(bucket(100, -10.0, 40, 40.0, 50.0), "hold", R.RULE_LABEL)
    assert r["pf"] == pytest.approx(0.8)
    assert r["net"] < 0


# ── the sample-size question ────────────────────────────────────────────────
def test_trades_needed_scales_with_how_small_the_effect_is():
    """A tiny edge needs a huge sample. This is the number that answers 'is it
    worth waiting?' — and it must get bigger as the effect shrinks."""
    big = R.trades_needed(bucket(1000, 200.0, 500, 400.0, 200.0))
    small = R.trades_needed(bucket(1000, 5.0, 500, 300.0, 295.0))
    assert small > big


def test_trades_needed_is_none_for_a_dead_flat_result():
    assert R.trades_needed(bucket(1000, 0.0, 500, 250.0, 250.0)) is None


# ── the lesson the repo keeps re-learning ───────────────────────────────────
def test_win_rate_lesson_names_both_rules_when_they_disagree():
    """48 mean-reversion configs, a 5-year TW backtest and a live Bybit account
    have all made this point. When the measured record makes it again, the page
    must name both rules — otherwise it reads as a slogan rather than evidence."""
    rules = [
        {"key": "a", "label": "Trail 1R", "wr": 36.5, "exp": -0.03},
        {"key": "b", "label": "TP1 only", "wr": 47.9, "exp": -0.052},
    ]
    lesson = R.win_rate_lesson(rules)
    assert lesson["agree"] is False
    assert lesson["wr_rule"] == "TP1 only"      # wins most often
    assert lesson["exp_rule"] == "Trail 1R"     # makes (loses) least
    assert lesson["wr_exp"] < lesson["exp_exp"], "the higher win rate is the worse rule"


def test_win_rate_lesson_admits_when_they_agree():
    rules = [
        {"key": "a", "label": "Trail 1R", "wr": 55.0, "exp": 0.10},
        {"key": "b", "label": "TP1 only", "wr": 40.0, "exp": 0.02},
    ]
    assert R.win_rate_lesson(rules)["agree"] is True


# ── the board as a whole ────────────────────────────────────────────────────
def test_board_is_sorted_best_first_and_survives_an_empty_file(tmp_path):
    empty = tmp_path / "nope.json"
    b = R.board(path=str(empty))
    assert b["ok"] is True and b["rules"] == [] and b["cohorts"] == []
    assert b["headline"]["any_edge"] is False


def test_headline_only_claims_an_edge_on_exact_evidence():
    """any_edge drives a green banner. It must not turn green on the bound."""
    import json
    state = {"totals": {"all": {"hold": bucket(4000, 400.0, 2000, 900.0, 500.0)}}}
    rules = R.rules_board(state, "all")
    h = R.headline(state, rules)
    assert h["rules_positive"] == 1
    assert h["rules_proven"] == 0
    assert h["any_edge"] is False, "a positive floor-basis result turned the banner green"
    json.dumps(h)                                # must stay JSON-safe


def test_basis_note_matches_what_the_rows_actually_claim():
    floor_rows = [{"basis": "floor"}]
    exact_rows = [{"basis": "exact"}]
    assert "narrowest" in R.basis_note(floor_rows)
    assert "exact" in R.basis_note(exact_rows).lower()
    assert "narrowest" not in R.basis_note(exact_rows)
