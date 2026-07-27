"""Real TW company fundamentals (TWSE OpenAPI) for dad's /tw page.

No API key, no consensus-estimate feed exists for TW stocks — 'beat/miss'
here is honestly YoY growth, not vs-analyst-estimate. These tests never hit
the network: requests.get is monkeypatched everywhere.
"""
from datetime import date

import tw_financials as F


def _fake_get(revenue_rows, eps_rows):
    def get(url, headers=None, timeout=None):
        class R:
            def json(self):
                return revenue_rows if url == F.REVENUE_URL else eps_rows
        return R()
    return get


def _reset_cache(monkeypatch):
    monkeypatch.setattr(F, "_cache", {"ts": 0.0, "revenue": {}, "eps": {}})


# ── next_deadline_iso ────────────────────────────────────────────────────────
def test_next_deadline_picks_the_nearest_upcoming_one():
    """Chronological order within a calendar year is annual(3/31) -> Q1(5/15)
    -> H1(8/14) -> Q3(11/14) -> next year's annual — the annual deadline for
    the PRIOR fiscal year lands earliest in the year, before Q1's own."""
    assert F.next_deadline_iso(date(2026, 1, 10)) == "2026-03-31"
    assert F.next_deadline_iso(date(2026, 3, 31)) == "2026-03-31"   # on the day itself
    assert F.next_deadline_iso(date(2026, 4, 1)) == "2026-05-15"
    assert F.next_deadline_iso(date(2026, 5, 15)) == "2026-05-15"
    assert F.next_deadline_iso(date(2026, 5, 16)) == "2026-08-14"
    assert F.next_deadline_iso(date(2026, 8, 14)) == "2026-08-14"
    assert F.next_deadline_iso(date(2026, 8, 15)) == "2026-11-14"
    assert F.next_deadline_iso(date(2026, 11, 15)) == "2027-03-31"
    assert F.next_deadline_iso(date(2026, 12, 31)) == "2027-03-31"


# ── summary() ────────────────────────────────────────────────────────────────
def test_summary_parses_revenue_yoy_and_month(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(F.requests, "get", _fake_get(
        revenue_rows=[{"公司代號": "2330", "資料年月": "11506",
                       "營業收入-去年同月增減(%)": "32.398"}],
        eps_rows=[],
    ))
    out = F.summary("2330")
    assert out["rev_yoy"] == 32.4
    assert out["rev_month"] == "2026-06"
    assert out["eps_yoy"] is None                   # no EPS rows for this code


def test_summary_computes_eps_yoy_from_same_season_prior_year(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(F.requests, "get", _fake_get(
        revenue_rows=[],
        eps_rows=[
            {"公司代號": "2330", "年度": "114", "季別": "1", "基本每股盈餘(元)": "8.00"},
            {"公司代號": "2330", "年度": "115", "季別": "1", "基本每股盈餘(元)": "10.00"},
            {"公司代號": "2330", "年度": "115", "季別": "2", "基本每股盈餘(元)": "11.00"},  # not the latest season match
        ],
    ))
    out = F.summary("2330")
    assert out["eps_cur"] == 11.0
    assert out["eps_season"] == "2026 Q2"
    assert out["eps_yoy"] is None                    # no 114 Q2 to compare against


def test_summary_finds_matching_prior_season(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(F.requests, "get", _fake_get(
        revenue_rows=[],
        eps_rows=[
            {"公司代號": "2330", "年度": "114", "季別": "2", "基本每股盈餘(元)": "8.00"},
            {"公司代號": "2330", "年度": "115", "季別": "2", "基本每股盈餘(元)": "10.00"},
        ],
    ))
    out = F.summary("2330")
    assert out["eps_cur"] == 10.0
    assert out["eps_yoy"] == 25.0                    # (10-8)/8


def test_summary_missing_code_is_all_none_but_not_an_error(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(F.requests, "get", _fake_get(revenue_rows=[], eps_rows=[]))
    out = F.summary("9999")
    assert out["rev_yoy"] is None and out["eps_yoy"] is None
    assert out["next_deadline"]                      # date math still runs


def test_summary_survives_network_failure(monkeypatch):
    _reset_cache(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("TWSE down")
    monkeypatch.setattr(F.requests, "get", boom)
    out = F.summary("2330")                          # must not raise
    assert out["rev_yoy"] is None and out["eps_yoy"] is None


def test_fetch_all_is_cached_between_calls(monkeypatch):
    """An empty response must NOT be cached (looks like a transient TWSE
    hiccup, not 'zero companies exist') — use real-shaped rows so the cache
    actually has something to hold onto, then prove the second call reuses it."""
    _reset_cache(monkeypatch)
    calls = {"n": 0}
    row = {"公司代號": "2330", "資料年月": "11506", "營業收入-去年同月增減(%)": "1.0"}

    def get(url, headers=None, timeout=None):
        calls["n"] += 1

        class R:
            def json(self):
                return [row]
        return R()
    monkeypatch.setattr(F.requests, "get", get)
    F.summary("2330")
    F.summary("2330")
    assert calls["n"] == 2                            # one revenue + one eps fetch, ONCE total
