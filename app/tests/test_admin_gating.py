"""🔒 Admin gating — owner-only data stays owner-only.

With the group being promoted, ordinary members will hold real accounts on
this site. They may browse signals/market pages, but the owner's REAL money
(balances, positions, equity curve, ops) is admin-only. These tests pin the
two layers: the decorator's behaviour and the equity-curve API's guard.
"""
import flask_login.utils

import app as APP


class _User:
    """Minimal flask-login user double."""
    is_active = True
    is_anonymous = False

    def __init__(self, admin):
        self.is_admin = admin
        self.is_authenticated = True

    def get_id(self):
        return "1"


def _as(monkeypatch, user):
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: user)


def test_admin_api_returns_403_json_for_member(monkeypatch):
    with APP.app.test_request_context("/api/balance_history"):
        _as(monkeypatch, _User(admin=False))
        resp, code = APP.api_balance_history()
        assert code == 403
        assert resp.get_json() == {"error": "admin only"}


def test_admin_api_serves_admin(monkeypatch):
    with APP.app.test_request_context("/api/balance_history"):
        _as(monkeypatch, _User(admin=True))
        resp = APP.api_balance_history()
        assert "history" in resp.get_json()          # real payload, not a 403


def test_admin_page_redirects_member_not_json(monkeypatch):
    # Non-API admin pages keep the friendly flash+redirect (not a bare 403).
    @APP.admin_required
    def dummy():
        return "secret"

    with APP.app.test_request_context("/bybit"):
        _as(monkeypatch, _User(admin=False))
        resp = APP.app.make_response(dummy())
        assert resp.status_code == 302               # → back to the dashboard
        assert "secret" not in resp.get_data(as_text=True)


def test_equity_panel_hidden_from_members():
    # The /performance template must not even render the equity-curve panel
    # (canvas + fetch) for non-admins — defence in depth beside the API 403.
    src = open("templates/performance.html", encoding="utf-8").read()
    start = src.index("Equity curve")
    guard = src.rindex("{% if user.is_admin %}", 0, start)
    end = src.index("{% endif %}", start)
    block = src[guard:end]
    assert "equityChart" in block and "/api/balance_history" in block
