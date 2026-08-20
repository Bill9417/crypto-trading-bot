"""🧩 板塊 — the sector board.

The map is curated and hand-maintained, which is the honest trade against a
third-party category API that rate-limits and reshapes itself without warning.
These tests hold the properties that make a hand-map safe: it must not report
a sector from a pair of tickers, must not silently drop what it cannot
classify, and must not let one runaway coin speak for a whole sector.
"""
import sectors as S


def _row(base, chg, vol=1e9):
    return {"base": base, "change_pct": chg, "volume_usdt": vol}


def test_median_not_mean_so_one_runaway_cannot_speak_for_a_sector():
    rows = [_row("BTC", 1.0), _row("ETH", 1.0), _row("SOL", 1.0),
            _row("ADA", 1.0), _row("AVAX", 80.0)]
    b = S.board(rows, exclude=set())
    sec = next(s for s in b["sectors"] if s["sector"] == "L1 公鏈")
    assert sec["median"] == 1.0, "one coin at +80% moved the sector reading"
    assert sec["mean"] > 15, "the mean is kept for comparison and should differ"


def test_a_sector_needs_more_than_a_pair_of_tickers():
    b = S.board([_row("XMR", 5.0), _row("ZEC", 6.0)], exclude=set())
    assert not [s for s in b["sectors"] if s["sector"] == "隱私"]
    assert "隱私" in b["thin"], "the thin sector was dropped without being named"


def test_unclassified_coins_are_counted_not_swallowed():
    """'迷因 is strongest' means something different when a third of the board
    was never classified."""
    b = S.board([_row("BTC", 1), _row("ETH", 1), _row("SOL", 1),
                 _row("NEVERHEARDOFIT", 50)], exclude=set())
    assert b["unmapped"] == 1
    assert "NEVERHEARDOFIT" in b["unmapped_sample"]


def test_contract_multiplier_prefixes_resolve_to_the_real_coin():
    """1000PEPE is PEPE with a different lot size. Seven of the first twelve
    unclassified names were these, which silently shrank 迷因 by a third."""
    assert S.normalise("1000PEPE") == "PEPE"
    assert S.normalise("1000000MOG") == "MOG"
    assert S.normalise("BTC") == "BTC"
    b = S.board([_row("1000PEPE", 5), _row("1000SHIB", 5), _row("DOGE", 5)],
                exclude=set())
    assert b["sectors"] and b["sectors"][0]["n"] == 3


def test_stock_perps_and_stablecoins_are_not_crypto_sectors():
    """This venue lists AAPL and SPY beside the coins, and a stablecoin's 24h
    change is noise around zero that drags a median to nothing."""
    skip = S.tradfi_bases()
    for t in ("SPY", "QQQ", "TQQQ", "SOXL"):
        assert t in skip, f"{t} would be classified as a crypto sector"
    for t in ("USDC", "USDT", "PAXG", "XAUT"):
        assert t in skip, f"{t} would drag a momentum median"


def test_a_missing_segment_cache_classifies_nothing_away(monkeypatch, tmp_path):
    """No cache must mean "exclude only what the static list knows", never
    "exclude everything" or "trust a guess"."""
    monkeypatch.chdir(tmp_path)
    import os
    real = os.path.dirname(os.path.abspath(S.__file__))
    monkeypatch.setattr(S, "__file__", str(tmp_path / "sectors.py"))
    skip = S.tradfi_bases()
    assert skip == S.NON_CRYPTO | S.NON_MOMENTUM
    monkeypatch.setattr(S, "__file__", os.path.join(real, "sectors.py"))


def test_every_mapped_base_has_exactly_one_sector():
    assert len(S.SECTOR_MAP) > 150
    assert len(set(S.SECTOR_MAP.values())) >= 8
    for base, sec in S.SECTOR_MAP.items():
        assert base == base.upper() and sec
