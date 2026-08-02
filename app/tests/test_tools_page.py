"""🧰 Tools page — the calculators live at /tools, NOT on the dashboard.

The two original widgets (price alerts, position size) were moved off the home
page on 2026-08-02 so the live signal feed isn't sharing space with input
forms. These tests pin that split so a future edit can't quietly drag them
back onto the dashboard, and cover the one server-side calculator input
(/api/funding), whose interval is DERIVED from history rather than assumed.
"""
import flask_login.utils
import pytest

import app as APP


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


# ── the page itself ──────────────────────────────────────────────────────────
def test_tools_page_carries_every_calculator(as_user):
    """All seven tools render. Anchors are what the jump-nav links to, so a
    renamed id silently breaks navigation — pin them."""
    with APP.app.test_request_context("/tools"):
        html = APP.tools_page()
    for anchor in ("t-alerts", "t-size", "t-edge", "t-ruin",
                   "t-funding", "t-dca", "t-dd"):
        assert f'id="{anchor}"' in html, f"missing tool section {anchor}"


def test_tools_page_is_login_gated():
    """No-auth access must redirect to the login page, never render."""
    client = APP.app.test_client()
    resp = client.get("/tools")
    assert resp.status_code in (301, 302, 401)
    assert "/login" in resp.headers.get("Location", "")


def _template(name):
    import os
    with open(os.path.join(APP.app.root_path, "templates", name),
              encoding="utf-8") as f:
        return f.read()


def test_calculators_are_not_on_the_dashboard(as_user):
    """The whole point of the move: the dashboard must NOT carry them."""
    with APP.app.test_request_context("/tools"):
        html = APP.tools_page()
    assert "psc-out" in html and "pa-form" in html     # they live on /tools ...
    index_html = _template("index.html")               # ... and not on home
    assert "psc-panel" not in index_html
    assert "palerts-panel" not in index_html
    assert 'href="/tools"' in _template("_nav.html")   # reachable from the nav


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
