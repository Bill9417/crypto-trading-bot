"""🎯 立即可進場 — the Top 5 for 台股 and 美股.

The interesting property is not the ordering. It is that an empty list must
mean what it says: a pullback strategy waits, so "nothing at its price" is the
normal answer, and "we cannot see any prices" is a completely different one
that produces an identical empty list.
"""
import stock_picks as S


def _row(**kw):
    base = {"code": "2330", "name": "台積電", "status": "tracking",
            "price": 100.0, "ref": 100.0, "sl": 90.0, "tp": 120.0,
            "dist_pct": 0.0, "buy_zone": True}
    return {**base, **kw}


def test_only_setups_still_at_their_price_are_called_ready():
    """The band means "entering HERE still matches the setup that was
    measured", not "the stock is above its stop"."""
    ready, watch = S._rank([
        _row(code="A", buy_zone=True, dist_pct=0.2),
        _row(code="B", buy_zone=False, dist_pct=2.0),
        _row(code="C", buy_zone=False, dist_pct=25.0),
    ])
    assert [r["code"] for r in ready] == ["A"]
    assert [r["code"] for r in watch] == ["B"], "a 25% gap is not a near-miss"


def test_closest_to_entry_ranks_first():
    ready, _ = S._rank([_row(code="FAR", dist_pct=1.9),
                        _row(code="NEAR", dist_pct=0.1)])
    assert [r["code"] for r in ready] == ["NEAR", "FAR"]


def test_closed_setups_never_appear():
    ready, watch = S._rank([_row(status="tp"), _row(status="sl"),
                            _row(status="timeout")])
    assert not ready and not watch


def test_an_empty_list_is_a_real_answer_not_a_padded_one():
    """Padding to five with setups 8% away would turn "top 5 opportunities"
    into "five stocks", which is the one thing a page called 好進場點 must
    not do."""
    ready, watch = S._rank([_row(buy_zone=False, dist_pct=9.0) for _ in range(20)])
    assert ready == [] and watch == []


def test_no_prices_is_reported_differently_from_nothing_near_entry(monkeypatch):
    """THE distinction. Overnight every dist_pct is None, so without this the
    page would report no opportunities every night for the wrong reason — the
    same shape as a dead feed reading as a quiet market."""
    class _Mod:
        @staticmethod
        def web_view():
            return {"setups": [_row(price=None, dist_pct=None, buy_zone=None)
                               for _ in range(25)]}
    monkeypatch.setattr(S, "__import__", lambda n, *a: _Mod(), raising=False)
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: _Mod() if n == "tw_stocks" else real(n, *a, **k))
    m = S._market("tw_stocks", "tw", "🇹🇼 台股")
    assert m["blind"] is True, "a market with no quotes was reported as 'nothing near entry'"
    assert m["priced"] == 0 and m["open_count"] == 25


def test_a_market_with_prices_but_none_in_band_is_not_blind(monkeypatch):
    import builtins
    real = builtins.__import__

    class _Mod:
        @staticmethod
        def web_view():
            return {"setups": [_row(price=120.0, dist_pct=20.0, buy_zone=False)]}
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: _Mod() if n == "tw_stocks" else real(n, *a, **k))
    m = S._market("tw_stocks", "tw", "🇹🇼 台股")
    assert m["blind"] is False and m["ready"] == []


def test_one_market_failing_does_not_take_the_other_down():
    """/tw is opened from a LINE link by someone who cannot debug it."""
    m = S._market("no_such_module_at_all", "tw", "台股")
    assert m["ready"] == [] and m["error"]


def test_each_market_carries_its_own_measured_edge():
    """The 台股 rules were ported to US and MEASURED — they did not transfer.
    Sharing one edge string between the two would state a number for a market
    it was never measured on."""
    p = S.picks()
    assert p["tw"]["market"] == "tw" and p["us"]["market"] == "us"
    if p["tw"].get("edge") and p["us"].get("edge"):
        assert p["tw"]["edge"] != p["us"]["edge"]


def test_it_reads_no_network():
    """Both pages are public and open from a phone link; a quote fetch here
    would put a scan's rate budget behind a page load."""
    import inspect
    src = inspect.getsource(S)
    for banned in ("requests.", "fetch_ohlcv", "yfinance", "urlopen"):
        assert banned not in src, f"stock_picks reaches the network via {banned}"
