"""🌐 Market Intel aggregation — concurrent fetches, fail-soft per feed.

/market pulls from eight unrelated providers. Serially that cost ~8.9s on a
cold cache; only two of the eight actually depend on another (the positioning
and OI panels need the futures rows first). `_gather` runs each independent
group concurrently, which measured ~4.5s.

The risk of concurrency here is that one dead feed takes the whole page with
it — previously impossible, because each producer caught its own errors. These
tests pin that a job which *raises* (not just returns errors) still leaves
every other panel intact.

conftest's autouse guard kills this module's three network seams, so anything
here that doesn't install its own fake is exercising the genuine offline path.
"""
import time

import market_intel as M


def test_gather_returns_every_job_result():
    out = M._gather(
        {"a": lambda: {"v": 1}, "b": lambda: {"v": 2}, "c": lambda: {"v": 3}},
        {"a": {}, "b": {}, "c": {}},
    )
    assert out == {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}}


def test_one_raising_job_does_not_lose_the_others():
    def boom():
        raise RuntimeError("feed is down")

    out = M._gather(
        {"good": lambda: {"rows": [1, 2]}, "bad": boom},
        {"good": {"rows": [], "errors": []}, "bad": {"rows": [], "errors": []}},
    )
    assert out["good"] == {"rows": [1, 2]}          # survivor untouched
    assert out["bad"]["rows"] == []                 # fell back
    assert "feed is down" in out["bad"]["errors"][0]


def test_fallback_is_copied_not_shared():
    """A raising job must not hand back the caller's fallback dict itself —
    mutating one panel's errors would otherwise corrupt the template."""
    fallback = {"x": {"rows": [], "errors": []}}

    def boom():
        raise RuntimeError("nope")

    out = M._gather({"x": boom}, fallback)
    assert out["x"] is not fallback["x"]
    assert fallback["x"]["errors"] == []            # original untouched


def test_jobs_actually_run_concurrently():
    """Three 150ms sleeps must finish in well under their 450ms serial sum."""
    def slow():
        time.sleep(0.15)
        return {"ok": True}

    t0 = time.perf_counter()
    out = M._gather({"a": slow, "b": slow, "c": slow}, {"a": {}, "b": {}, "c": {}})
    elapsed = time.perf_counter() - t0
    assert all(r == {"ok": True} for r in out.values())
    assert elapsed < 0.35, f"ran serially ({elapsed:.2f}s)"


def test_market_intel_degrades_to_empty_panels_when_every_feed_is_dead(tmp_path,
                                                                       monkeypatch):
    """conftest kills the network seams, so this exercises the real aggregator
    with all eight providers failing — the page must still render a payload.

    The calendar now keeps a DISK cache and serves the last good copy when the
    fetch fails (that is the whole point — a 429 used to empty it silently), so
    point that cache at an empty tmp dir or this test reads the real one."""
    monkeypatch.setattr(M, "_DISK_CACHE_DIR", str(tmp_path))
    out = M.market_intel(top_n=5)
    assert out["futures"] == []
    assert out["positioning"] == []
    assert out["news"] == []
    assert out["calendar"] == []
    assert isinstance(out["generated_at"], int) and out["generated_at"] > 0
    assert out["errors"], "a totally dead upstream should be reported, not hidden"


def test_market_intel_merges_oi_change_onto_futures_rows(monkeypatch):
    """The OI delta is fetched in stage 2 and merged back onto stage 1's rows.
    Getting that wiring wrong silently blanks a column, so pin it."""
    monkeypatch.setattr(M, "binance_futures", lambda top_n=15: {
        "rows": [{"symbol": "BTC/USDT:USDT", "base": "BTC", "price": 1.0},
                 {"symbol": "ETH/USDT:USDT", "base": "ETH", "price": 2.0}],
        "errors": [],
    })
    monkeypatch.setattr(M, "oi_change", lambda syms: {
        "rows": {"BTC/USDT:USDT": 5.5}, "errors": [],
    })
    monkeypatch.setattr(M, "long_short", lambda syms: {"rows": [{"s": 1}], "errors": []})
    for name in ("defillama", "news", "global_market", "fear_greed", "stocks", "econ_calendar"):
        monkeypatch.setattr(M, name, lambda: {"rows": [], "items": [], "events": [], "errors": []})

    out = M.market_intel(top_n=2)
    by_sym = {r["symbol"]: r for r in out["futures"]}
    assert by_sym["BTC/USDT:USDT"]["oi_change_24h_pct"] == 5.5
    assert by_sym["ETH/USDT:USDT"]["oi_change_24h_pct"] is None   # absent, not crashed
    assert out["positioning"] == [{"s": 1}]
