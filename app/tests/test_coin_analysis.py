"""🔍 One-coin analysis — a page whose main risk is looking like a verdict.

The panel this was modelled on sums weighted points (+24 / −7). Those weights
ARE the claim: they assert one factor is six times more informative than
another, and nothing has measured that. So the tests here are less about the
arithmetic than about what the page is allowed to say.
"""
import pytest

import coin_analysis as A


# ── the composite claims only what it can support ───────────────────────────
def test_the_composite_is_a_count_not_a_weighted_score():
    """A weighted score reads as an expected return. A count reads as 'six of
    eight readings lean this way', which is exactly what is known. This repo
    measured 48 high-win-rate setups and found 46 losing; a fabricated weight
    is how that happens."""
    # Checked on the CODE with docstrings removed, and on the output contract.
    # The first version grepped the whole module and failed on its own
    # docstring, which explains the "+24 分" panel it is refusing to copy — a
    # test that its own prose can trip is testing prose.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(A))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            node.value.value = ""            # blank every docstring
    code = ast.unparse(tree)
    for invented in ("WEIGHTS", "weight *", "points", "score +="):
        assert invented not in code, f"a weighted score crept in: {invented}"

    # the output contract: factors carry a DIRECTION, never points
    f = A._factor("k", "l", "v", {"read": A.LONG, "note": "n"})
    assert set(f) == {"key", "label", "value", "read", "note"}
    assert f["read"] in (A.LONG, A.SHORT, A.NEUTRAL)


def test_the_disclaimer_says_it_is_conditions_not_a_prediction():
    assert "不是預測" in A.DISCLAIMER
    assert "46" in A.DISCLAIMER          # the 48/46 finding is cited


# ── the readings ────────────────────────────────────────────────────────────
def test_extreme_positive_funding_is_read_as_crowded_longs_not_bullish():
    """The sign inversion that is easy to get backwards. Longs PAY shorts when
    funding is positive, so an extreme reading is a crowding warning. Read as
    bullish it would confirm the trade at its most expensive point."""
    hist = [0.00001] * 20
    out = A.read_funding(0.0010, hist)
    assert out["read"] == A.SHORT
    assert "擁擠" in out["note"]


def test_extreme_negative_funding_is_read_as_crowded_shorts():
    out = A.read_funding(-0.0010, [0.00001] * 20)
    assert out["read"] == A.LONG


def test_ordinary_funding_takes_no_side():
    """Judged against the symbol's OWN median: 0.01% is ordinary on one perp
    and extreme on another, so an absolute threshold is wrong per symbol."""
    out = A.read_funding(0.00005, [0.00005] * 20)
    assert out["read"] == A.NEUTRAL
    assert "常態" in out["note"]


def test_funding_with_no_history_does_not_guess():
    assert A.read_funding(0.01, [])["read"] == A.NEUTRAL


def test_taker_pressure_names_the_aggressor_because_it_actually_can():
    """Open interest cannot name the aggressive side — every contract has a
    long and a short. Taker volume can: it records who crossed the spread."""
    assert A.read_taker(120, 80)["read"] == A.LONG
    assert A.read_taker(80, 120)["read"] == A.SHORT
    assert A.read_taker(100, 100)["read"] == A.NEUTRAL
    assert A.read_taker(0, 0)["read"] == A.NEUTRAL


def test_retail_long_short_is_read_contrarian_only_at_extremes():
    assert A.read_ls_ratio(2.5)["read"] == A.SHORT
    assert A.read_ls_ratio(0.4)["read"] == A.LONG
    assert A.read_ls_ratio(1.02)["read"] == A.NEUTRAL
    assert A.read_ls_ratio(None)["read"] == A.NEUTRAL


def test_relative_strength_is_measured_against_the_same_window():
    """A coin lagging BTC through the same hours has its own selling pressure.
    Comparing different windows would measure the calendar, not the coin."""
    assert A.read_rs(-16.0, 0.0)["read"] == A.SHORT
    assert A.read_rs(10.0, 1.0)["read"] == A.LONG
    assert A.read_rs(1.0, 0.5)["read"] == A.NEUTRAL


def test_the_oi_read_is_the_same_one_the_radar_uses():
    """Two pages disagreeing about the same symbol's open interest would make
    both untrustworthy, so the four-state read has exactly one definition."""
    import crowd_radar as C
    assert A.read_oi(5.0, 1.0)["state"] == C.LONGS_OPENING
    assert A.read_oi(5.0, -1.0)["state"] == C.SHORTS_OPENING
    assert A.read_oi(-5.0, 1.0)["state"] == C.SHORTS_CLOSING
    assert A.read_oi(-5.0, -1.0)["state"] == C.LONGS_CLOSING
    # shorts covering is bullish for PRICE even though OI fell
    assert A.read_oi(-5.0, 1.0)["read"] == A.LONG


def test_momentum_needs_enough_bars():
    assert A.read_momentum([1, 2], 24) == (None, A.NEUTRAL)


# ── the search box ──────────────────────────────────────────────────────────
UNIV = ["BEATUSDT", "BEUSDT", "BELUSDT", "BERAUSDT", "ADBEUSDT", "SOLUSDT"]


def test_an_exact_ticker_ranks_first():
    """Typing 'BE' and getting ADBE before BE would make the box feel broken."""
    assert A.search("BE", UNIV)[0] == "BE"


def test_prefix_matches_come_before_substring_matches():
    got = A.search("BE", UNIV)
    assert got.index("BEAT") < got.index("ADBE")


def test_search_is_case_insensitive():
    assert A.search("sol", UNIV) == ["SOL"]


def test_an_empty_query_returns_nothing_rather_than_everything():
    assert A.search("", UNIV) == []
    assert A.search("   ", UNIV) == []


# ── input handling ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("q,want", [
    ("beat", "BEATUSDT"), ("BEAT", "BEATUSDT"), ("BEATUSDT", "BEATUSDT"),
    ("BEAT/USDT:USDT", "BEATUSDT"), ("  sol  ", "SOLUSDT"),
])
def test_tickers_are_resolved_from_whatever_the_user_types(q, want):
    assert A.resolve(q) == want


@pytest.mark.parametrize("q", ["", "   ", "../etc", "A B/C", "'; DROP"])
def test_junk_is_rejected_rather_than_sent_to_the_exchange(q):
    """resolve() is what stands between the search box and a URL parameter."""
    assert A.resolve(q) == ""


def test_an_unknown_symbol_reports_it_instead_of_500ing(monkeypatch):
    import requests

    class _Resp:
        status_code = 400

    def boom(*a, **k):
        raise requests.HTTPError(response=_Resp())

    monkeypatch.setattr(A, "_get", boom)
    A._cache.clear()
    out = A.analyse("NOTACOIN")
    assert out["ok"] is False and "找不到" in out["error"]


def test_a_network_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(A, "_get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    A._cache.clear()
    out = A.analyse("SOL")
    assert out["ok"] is False and out["symbol"] == "SOLUSDT"


def test_a_bad_query_never_reaches_the_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("junk query hit the exchange")

    monkeypatch.setattr(A, "_get", boom)
    A._cache.clear()
    assert A.analyse("../../etc/passwd")["ok"] is False


# ── honesty about the venue ─────────────────────────────────────────────────
def test_open_interest_is_labelled_as_single_venue():
    """The modelled panel shows a CoinGlass AGGREGATE. Ours is Binance only, so
    its OI/市值 of 20.2% and our 3.6% are different measurements, not a
    discrepancy — presenting one under the other's name is the comfortable lie.
    """
    import inspect
    src = inspect.getsource(A._build)
    assert "oi_venue" in src
    assert "Binance" in src
