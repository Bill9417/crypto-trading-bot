"""
Real TW company fundamentals for dad's /tw page — TWSE OpenAPI, no API key.

Two free, official datasets:
  · t187ap05_L — monthly revenue (營收), TWSE already computes the YoY% itself.
  · t187ap14_L — quarterly basic EPS (基本每股盈餘); YoY is computed here
    (same season, one year apart) since TWSE doesn't precompute that one.

There is no free, public analyst-consensus-estimate feed for TW stocks (unlike
a US quoteSummary's epsEstimate) — a paid vendor would be needed for that. So
"beat/miss" here honestly means "grew vs the same period last year", not
"beat Wall Street's number" — do not relabel it as an estimate comparison.

Next report date is NOT company-specific: TW's quarterly filing deadlines are
statutory and identical for every listed company (Q1 → May 15, H1/Q2 →
Aug 14, Q3 → Nov 14, Annual → the following Mar 31). This returns whichever
of those hasn't passed yet — a real company may well file earlier.

Fail-soft throughout: this is decoration on dad's page, never required. Any
network hiccup returns None/empty rather than raising.
"""
import threading
import time
from datetime import date

import requests

_UA = {"User-Agent": "Mozilla/5.0"}
_CACHE_TTL = 6 * 3600           # revenue is monthly, EPS is quarterly — no need to refetch often
_lock = threading.Lock()
_cache = {"ts": 0.0, "revenue": {}, "eps": {}}

REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
EPS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"

_DEADLINE_MD = [(5, 15), (8, 14), (11, 14), (3, 31)]   # Q1, H1/Q2, Q3, Annual(next yr)


def next_deadline_iso(today: date = None) -> str:
    """Nearest upcoming statutory quarterly-report deadline, as an ISO date."""
    d = today or date.today()
    candidates = []
    for month, day in _DEADLINE_MD:
        for year in (d.year, d.year + 1):
            try:
                dl = date(year, month, day)
            except ValueError:
                continue
            if dl >= d:
                candidates.append(dl)
    return min(candidates).isoformat() if candidates else ""


def _fetch_all():
    with _lock:
        if _cache["revenue"] and time.time() - _cache["ts"] < _CACHE_TTL:
            return _cache["revenue"], _cache["eps"]
    revenue, eps = {}, {}
    try:
        for r in requests.get(REVENUE_URL, headers=_UA, timeout=15).json():
            code = r.get("公司代號")
            if code:
                revenue.setdefault(code, []).append(r)
    except Exception:  # noqa: BLE001 — fundamentals are decoration, never required
        pass
    try:
        for r in requests.get(EPS_URL, headers=_UA, timeout=15).json():
            code = r.get("公司代號")
            if code:
                eps.setdefault(code, []).append(r)
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        if revenue or eps:
            _cache.update(ts=time.time(), revenue=revenue or _cache["revenue"],
                          eps=eps or _cache["eps"])
        return _cache["revenue"], _cache["eps"]


def summary(code: str) -> dict:
    """{'rev_yoy': float|None, 'rev_month': 'YYYY-MM'|None, 'eps_cur': float|None,
        'eps_yoy': float|None, 'eps_season': 'YYYY Qn'|None, 'next_deadline': iso}.
    Every field degrades to None independently — TWSE sometimes has one
    dataset current and the other lagging."""
    revenue, eps = _fetch_all()
    out = {"rev_yoy": None, "rev_month": None, "eps_cur": None,
           "eps_yoy": None, "eps_season": None,
           "next_deadline": next_deadline_iso()}

    rrows = revenue.get(code) or []
    if rrows:
        latest = max(rrows, key=lambda r: str(r.get("資料年月") or ""))
        try:
            out["rev_yoy"] = round(float(latest["營業收入-去年同月增減(%)"]), 1)
            ym = str(latest["資料年月"])                  # ROC yyymm, e.g. '11506'
            out["rev_month"] = f"{int(ym[:-2]) + 1911}-{ym[-2:]}"
        except (KeyError, ValueError, TypeError):
            pass

    erows = eps.get(code) or []
    if erows:
        def season_key(r):
            try:
                return (int(r.get("年度") or 0), int(r.get("季別") or 0))
            except (TypeError, ValueError):
                return (0, 0)
        erows = sorted(erows, key=season_key)
        latest = erows[-1]
        ly, ls = season_key(latest)
        prior = next((r for r in erows if season_key(r) == (ly - 1, ls)), None)
        try:
            out["eps_cur"] = float(latest["基本每股盈餘(元)"])
            out["eps_season"] = f"{ly + 1911} Q{ls}" if ls else f"{ly + 1911} 全年"
            if prior:
                prior_eps = float(prior["基本每股盈餘(元)"])
                if prior_eps:
                    out["eps_yoy"] = round((out["eps_cur"] - prior_eps) / abs(prior_eps) * 100, 1)
        except (KeyError, ValueError, TypeError):
            pass

    return out
