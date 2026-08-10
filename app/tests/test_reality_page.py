"""🔍 Reality Check — the page that is allowed to say "nothing is working".

Replaced the seven-calculator Toolkit on 2026-08-10. The calculators were
generic (expectancy, DCA, drawdown, risk-of-ruin are on every trading site) and
none of them knew anything about this account, while signal_outcomes had
quietly scored ~19,000 real outcomes against six exit rules that could only be
read as a Telegram text table.

The tests that matter here are honesty tests. This page exists to talk someone
out of a bad idea, so the failure that would hurt is not a 500 — it is the page
claiming an edge it has not measured.
"""
import flask_login.utils
import pytest

import app as APP
import reality as R


class _User:
    is_active = True
    is_anonymous = False
    is_authenticated = True
    is_admin = True

    def get_id(self):
        return "1"


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: _User())


def _template(name):
    import os
    with open(os.path.join(APP.app.root_path, "templates", name),
              encoding="utf-8") as f:
        return f.read()


# ── the page ────────────────────────────────────────────────────────────────
def test_reality_page_renders(as_user):
    with APP.app.test_request_context("/reality"):
        html = APP.reality_page()
    for hook in ("rc-verdict", "rc-rules", "rc-cohortboard", "rc-lesson"):
        assert f'id="{hook}"' in html, f"missing section {hook}"


def test_price_alerts_survived_the_rebuild(as_user):
    """The one widget on the old page that did something an exchange does not,
    and it is wired to the scanner — losing it in a redesign would silently
    stop real Telegram alerts the user still expects."""
    with APP.app.test_request_context("/reality"):
        html = APP.reality_page()
    assert 'id="pa-form"' in html and 'id="pa-list"' in html
    assert "/api/price_alerts" in html


def test_reality_page_is_login_gated():
    resp = APP.app.test_client().get("/reality")
    assert resp.status_code in (301, 302, 401)
    assert "/login" in resp.headers.get("Location", "")


def test_tools_url_still_resolves():
    """/tools was the nav target for a week and is in old messages and
    bookmarks. It must redirect, not 404."""
    rules = {str(r) for r in APP.app.url_map.iter_rules()}
    assert "/tools" in rules, "the old URL was deleted outright — bookmarks 404"


def test_nav_points_at_the_new_page():
    nav = _template("_nav.html")
    assert 'href="/reality"' in nav
    assert 'href="/tools"' not in nav, "nav should link the canonical URL, not the redirect"


def test_the_dashboard_did_not_reabsorb_the_widgets():
    """Carried over from the old suite: the split that moved these off the
    home page must hold."""
    index_html = _template("index.html")
    assert "psc-panel" not in index_html
    assert "palerts-panel" not in index_html


# ── the API ─────────────────────────────────────────────────────────────────
def test_api_reality_rejects_unknown_slices(as_user):
    """cohort/rule come straight off the query string and index into dicts —
    an unknown value must fall back, never leak a KeyError or a 500."""
    client = APP.app.test_client()
    d = client.get("/api/reality?cohort=../etc&rule=DROP").get_json()
    assert d["ok"] is True
    assert d["cohort"] == "all" and d["rule"] == "hold"


def test_api_reality_never_500s_when_the_tally_is_unreadable(as_user, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(R, "board", _boom)
    resp = APP.app.test_client().get("/api/reality")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


# ── /api/funding (the one calculator that needs the server) ──────────────────
def test_funding_rejects_junk_symbols(as_user):
    for bad in ("", "  ", "ETH; DROP", "../etc"):
        with APP.app.test_request_context(f"/api/funding?symbol={bad}"):
            resp = APP.api_funding()
            body, code = (resp if isinstance(resp, tuple) else (resp, 200))
            assert code == 400, f"{bad!r} should be rejected"
            assert body.get_json()["ok"] is False


def test_funding_derives_interval_from_history(as_user, monkeypatch):
    """ccxt leaves `interval` None on Binance, so the tool derives it from the
    gaps between funding prints. An assumed 8h would silently misprice the
    4h-interval alts, so this is the behaviour worth pinning."""
    h = 3600_000
    monkeypatch.setattr(APP.rest_client, "call", lambda name, *a, **k: (
        {"fundingRate": 0.0001, "markPrice": 100.0, "fundingTimestamp": 40 * h}
        if name == "fetch_funding_rate" else
        # four prints, 4h apart => interval must come out as 4, not 8
        [{"timestamp": t * h, "fundingRate": 0.0002}
         for t in (28, 32, 36, 40)]
    ))
    APP._funding_cache.clear()
    with APP.app.test_request_context("/api/funding?symbol=DOGE"):
        data = APP.api_funding().get_json()
    assert data["ok"] is True
    assert data["interval_hours"] == 4
    assert data["avg_rate"] == pytest.approx(0.0002)
    assert data["avg_samples"] == 4


def test_funding_survives_history_failure(as_user, monkeypatch):
    """History is best-effort — the tool must still answer on the live rate
    alone rather than 500 when the second call fails."""
    def _call(name, *a, **k):
        if name == "fetch_funding_rate":
            return {"fundingRate": 0.0001, "markPrice": 100.0}
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(APP.rest_client, "call", _call)
    APP._funding_cache.clear()
    with APP.app.test_request_context("/api/funding?symbol=ETH"):
        data = APP.api_funding().get_json()
    assert data["ok"] is True
    assert data["interval_hours"] is None      # unknown, not guessed
    assert data["funding_rate"] == 0.0001


def test_funding_is_cached(as_user, monkeypatch):
    """A tool people retype into must not hammer Binance — memory of the
    10006 rate-limit spam says uncached polling is what caused it."""
    calls = []

    def _call(name, *a, **k):
        calls.append(name)
        if name == "fetch_funding_rate":
            return {"fundingRate": 0.0001, "markPrice": 100.0}
        return []

    monkeypatch.setattr(APP.rest_client, "call", _call)
    APP._funding_cache.clear()
    for _ in range(3):
        with APP.app.test_request_context("/api/funding?symbol=SOL"):
            APP.api_funding()
    assert calls.count("fetch_funding_rate") == 1, "second call should hit cache"
