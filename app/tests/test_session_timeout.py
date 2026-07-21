"""🔒 Session idle timeout — the dashboard shows real account balances, so a
browser tab left open indefinitely shouldn't stay authenticated forever.

Login touches the real User model/DB, so these tests mock check_password_hash
and User.query instead of hitting users.db (same spirit as test_admin_gating's
flask_login double — never write to the real login database from a test).
"""
from datetime import timedelta

import app as APP


class _FakeUser:
    id = 999999
    password = "hashed"
    username = "faketestuser"


class _FakeQuery:
    def filter_by(self, **_kw):
        return self

    def first(self):
        return _FakeUser()


class _FakeUserModel:
    """Stands in for the real User model — login() only ever does
    `User.query.filter_by(...).first()`, so this never touches SQLAlchemy
    or the app context (unlike patching attributes ONTO the real User
    class, whose flask_sqlalchemy `query` descriptor lives on the
    metaclass and ignores instance/subclass __dict__ overrides)."""
    query = _FakeQuery()


def test_permanent_session_lifetime_is_configured():
    assert isinstance(APP.app.config["PERMANENT_SESSION_LIFETIME"], timedelta)
    assert APP.app.config["PERMANENT_SESSION_LIFETIME"] > timedelta(0)


def test_login_issues_a_persistent_expiring_cookie(monkeypatch):
    """session.permanent=True on login → the Set-Cookie carries Max-Age/Expires
    instead of being a browser-session-only cookie with no idle timeout."""
    monkeypatch.setattr(APP, "User", _FakeUserModel)
    monkeypatch.setattr(APP, "check_password_hash", lambda h, p: True)
    monkeypatch.setattr(APP, "login_user", lambda user, **kw: True)

    client = APP.app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "tok"
    resp = client.post("/login", data={"username": "x", "password": "y",
                                       "csrf_token": "tok"})
    assert resp.status_code == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "Max-Age" in set_cookie or "Expires" in set_cookie


def test_failed_login_does_not_mark_session_permanent(monkeypatch):
    """A wrong password must not upgrade the session's lifetime either."""
    monkeypatch.setattr(APP, "User", _FakeUserModel)
    monkeypatch.setattr(APP, "check_password_hash", lambda h, p: False)

    client = APP.app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "tok"
    resp = client.post("/login", data={"username": "x", "password": "wrong",
                                       "csrf_token": "tok"})
    assert resp.status_code == 200                 # re-renders the form, no redirect
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "Max-Age" not in set_cookie and "Expires" not in set_cookie
