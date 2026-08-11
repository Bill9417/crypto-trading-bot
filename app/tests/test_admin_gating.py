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
    # Anchor to the app package, not the cwd — a bare relative path only
    # resolves when pytest is started from inside app/.
    from pathlib import Path
    tpl = Path(APP.__file__).resolve().parent / "templates" / "performance.html"
    src = tpl.read_text(encoding="utf-8")
    start = src.index("Equity curve")
    guard = src.rindex("{% if user.is_admin %}", 0, start)
    end = src.index("{% endif %}", start)
    block = src[guard:end]
    assert "equityChart" in block and "/api/balance_history" in block


# ── the owner's trade history is owner-only (2026-08-11 security review) ────
# /performance and its two APIs were @login_required, not @admin_required: any
# member account — and copy-trading gives real people accounts here — could
# read the owner's full trade history and per-trade P&L. Not balances, but more
# than a follower needs. Pinned by reading the decorators actually applied, so
# a future edit that relaxes one of them fails here instead of in production.
def _decorators_for(route: str) -> str:
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read().split("\n")
    for i, line in enumerate(src):
        if re.match(rf'@app\.route\("{re.escape(route)}"\)?', line.strip()):
            return "\n".join(src[i:i + 5])
    raise AssertionError(f"route not found: {route}")


def test_performance_pages_are_admin_only():
    for route in ("/performance", "/api/performance_stats", "/api/performance_live"):
        block = _decorators_for(route)
        assert "@admin_required" in block, f"{route} is not admin-gated"


def test_performance_nav_link_is_hidden_from_members():
    """A nav item that always 403s is worse than one that isn't rendered."""
    import os
    nav = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "templates", "_nav.html")
    with open(nav, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for i, l in enumerate(lines):
        if "url_for('performance')" in l:
            preceding = "\n".join(lines[max(0, i - 6):i])
            assert "is_admin" in preceding, "Performance nav link is not admin-gated"
            break
    else:
        raise AssertionError("Performance nav link not found")
