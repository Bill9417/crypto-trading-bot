"""
Market Pulse — the composite behind the redesigned dashboard.

The tests that matter here are the honesty ones. A composite score built from
five feeds is only trustworthy if a dead feed is visibly ABSENT rather than
quietly counted as zero (which would drag the reading down and look identical
to a genuinely weak tape) or as 50 (which would look identical to a genuinely
neutral one). Everything else in this file is arithmetic.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pulse  # noqa: E402


def _tickers(rows):
    return {"rows": rows, "errors": []}


def _row(base, pct, vol=50_000_000.0, price=10.0):
    return {"symbol": f"{base}/USDT:USDT", "base": base, "price": price,
            "pct": pct, "volume": vol}


def _wire(monkeypatch, *, rows=None, funding=None, candles=None, fng=None,
          regime="BULL"):
    monkeypatch.setattr(pulse, "perp_tickers",
                        lambda *a, **k: _tickers(rows if rows is not None else []))
    monkeypatch.setattr(pulse, "funding_all",
                        lambda *a, **k: {"rows": funding or {}, "errors": []})
    monkeypatch.setattr(pulse, "btc_daily",
                        lambda *a, **k: {"candles": candles or [], "errors": []})
    monkeypatch.setattr(pulse.market_intel, "fear_greed",
                        lambda *a, **k: {"value": fng, "label": "Fear", "errors": []})
    monkeypatch.setattr(pulse, "_btc_regime", lambda: regime)


# ── the honesty rules ───────────────────────────────────────────────────────
def test_a_dead_feed_is_excluded_not_scored_zero(monkeypatch):
    """Funding and sentiment unavailable → they must not appear in the average.

    If they were counted as 0 the composite would collapse; if counted as 50 it
    would look like a real neutral reading. Neither is a thing we measured.
    """
    rows = [_row("A", 5.0), _row("B", 5.0), _row("C", 5.0)]
    _wire(monkeypatch, rows=rows, funding={}, fng=None, regime="BULL")
    d = pulse.compute()

    live = {c["key"]: c for c in d["components"] if c["ok"]}
    assert set(live) == {"trend", "breadth", "momentum"}
    # denominator = the live weights only (25 + 25 + 20), not 100
    w = pulse.WEIGHTS
    expected = (85.0 * w["trend"] + 100.0 * w["breadth"] + live["momentum"]["score"] * w["momentum"]) \
        / (w["trend"] + w["breadth"] + w["momentum"])
    assert d["score"] == round(expected, 1)

    # ...and the drop-out is reported rather than hidden
    assert d["confidence"]["ok"] == 3
    assert d["confidence"]["total"] == 5
    assert d["confidence"]["pct"] == 70.0
    assert set(d["confidence"]["missing"]) == {"Funding rate", "Fear & Greed"}


def test_missing_components_carry_none_not_a_number(monkeypatch):
    _wire(monkeypatch, rows=[], funding={}, fng=None, regime="")
    d = pulse.compute()
    assert d["score"] is None                    # nothing was readable at all
    assert d["confidence"]["pct"] == 0.0
    for c in d["components"]:
        assert c["ok"] is False
        assert c["score"] is None
        assert c["value"] is None


def test_full_read_reports_full_confidence(monkeypatch):
    rows = [_row("A", 1.0), _row("B", -1.0), _row("C", 2.0)]
    _wire(monkeypatch, rows=rows,
          funding={r["symbol"]: 0.0001 for r in rows}, fng=50, regime="NEUTRAL")
    d = pulse.compute()
    assert d["confidence"] == {"pct": 100.0, "ok": 5, "total": 5, "missing": []}
    assert d["score"] is not None


# ── components ──────────────────────────────────────────────────────────────
def test_breadth_is_the_share_of_the_universe_that_is_green(monkeypatch):
    rows = [_row("A", 1.0), _row("B", 1.0), _row("C", 1.0), _row("D", -1.0)]
    _wire(monkeypatch, rows=rows)
    d = pulse.compute()
    assert d["breadth"] == {"up": 3, "down": 1, "flat": 0, "total": 4, "ratio": 3.0}
    assert next(c for c in d["components"] if c["key"] == "breadth")["value"] == 75.0


def test_illiquid_listings_do_not_get_a_vote(monkeypatch):
    """A 200k-a-day ghost printing +40% must not move breadth or momentum."""
    rows = [_row("REAL", -1.0), _row("GHOST", 40.0, vol=200_000.0)]
    _wire(monkeypatch, rows=rows)
    d = pulse.compute()
    assert d["breadth"]["total"] == 1
    assert d["breadth"]["up"] == 0


def test_momentum_uses_the_median_so_one_pump_cannot_own_it(monkeypatch):
    rows = [_row("A", 0.0), _row("B", 0.0), _row("C", 0.0), _row("D", 400.0)]
    _wire(monkeypatch, rows=rows)
    d = pulse.compute()
    mom = next(c for c in d["components"] if c["key"] == "momentum")
    assert mom["value"] == 0.0          # the mean would have been +100
    assert mom["score"] == 50.0


def test_trend_prefers_the_scanner_regime_so_the_page_cannot_disagree(monkeypatch):
    _wire(monkeypatch, rows=[_row("A", 1.0)], regime="BEAR")
    trend = next(c for c in pulse.compute()["components"] if c["key"] == "trend")
    assert trend["value"] == "BEAR"
    assert trend["score"] == 15.0


def test_trend_falls_back_to_the_daily_ema_structure(monkeypatch):
    # 220 rising closes → price above both EMAs, EMA50 above EMA200.
    candles = [[i * 86_400_000, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i]
               for i in range(220)]
    _wire(monkeypatch, rows=[_row("A", 1.0)], regime="", candles=candles)
    trend = next(c for c in pulse.compute()["components"] if c["key"] == "trend")
    assert trend["value"] == "BULL"
    assert trend["score"] == 90.0


# ── panels ──────────────────────────────────────────────────────────────────
def test_a_sector_needs_enough_members_to_be_averaged():
    rows = [_row("BTC", 1.0), _row("ETH", 3.0), _row("SOL", 2.0),   # L1 ×3
            _row("XMR", 5.0), _row("ZEC", 5.0)]                     # Privacy ×2
    names = {s["name"] for s in pulse.sectors(rows)}
    assert "L1 Chains" in names
    assert "Privacy" not in names          # 2 members is not an average


def test_sector_row_reports_its_median_and_its_extremes():
    rows = [_row("BTC", 1.0), _row("ETH", 3.0), _row("SOL", 10.0)]
    s = pulse.sectors(rows)[0]
    assert s["median"] == 3.0 and s["n"] == 3 and s["up"] == 3
    assert (s["leader"], s["laggard"]) == ("SOL", "BTC")


def test_unmapped_tickers_are_not_guessed_into_a_sector():
    assert pulse.sector_of("BTC") == "L1 Chains"
    assert pulse.sector_of("SOMETHINGNEW") == "Other"
    assert all(s["name"] != "Other" for s in
               pulse.sectors([_row("Q1", 1.0), _row("Q2", 2.0), _row("Q3", 3.0)]))


def test_movers_are_ranked_from_both_ends():
    rows = [_row(b, p) for b, p in (("A", 5.0), ("B", -9.0), ("C", 1.0), ("D", 12.0))]
    m = pulse.movers(rows, n=2)
    assert [r["base"] for r in m["up"]] == ["D", "A"]
    assert [r["base"] for r in m["down"]] == ["B", "C"]


def test_btc_block_summarises_its_own_window():
    candles = [[1, 10.0, 15.0, 8.0, 12.0], [2, 12.0, 20.0, 11.0, 18.0]]
    b = pulse.btc_block(candles, days=30)
    assert b["ok"] and b["days"] == 2
    assert (b["open"], b["close"], b["high"], b["low"]) == (10.0, 18.0, 20.0, 8.0)
    assert b["change_pct"] == 80.0


def test_btc_block_says_so_when_there_are_no_candles():
    assert pulse.btc_block([]) == {"series": [], "ok": False}


def test_label_bands_are_ordered():
    assert pulse.label_for(90)[1] == "bull"
    assert pulse.label_for(50)[1] == "neutral"
    assert pulse.label_for(10)[1] == "bear"
