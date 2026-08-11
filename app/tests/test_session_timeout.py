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
    # login() rotates this to enforce one-active-login-per-account, so the
    # double has to model it or the view blows up on an attribute the real
    # User has. It is also what proves rotation happens on every login.
    session_token = None
    last_login_at = None
    last_login_ip = None

    def new_session_token(self):
        import secrets
        self.session_token = secrets.token_urlsafe(32)
        return self.session_token

    def get_id(self):
        return f"{self.id}|{self.session_token or ''}"


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


# ── ONE ACCOUNT, ONE ACTIVE LOGIN (2026-08-11) ──────────────────────────────
# The dashboard is a paid/private surface; one login being passed around
# between people defeats that. A fresh login rotates the account's
# session_token, and the token is baked into the cookie by get_id() — so every
# cookie minted earlier resolves to None and those devices are signed out.
#
# Enforcement is tested through load_user(), which is the single choke point
# every authenticated request passes through. No real users.db is touched.
class _DBUser:
    """Mirrors the real User's token behaviour without SQLAlchemy."""
    def __init__(self, uid=1, token="tok-A"):
        self.id = uid
        self.session_token = token

    def get_id(self):
        return f"{self.id}|{self.session_token or ''}"


def _loader_with(monkeypatch, user):
    monkeypatch.setattr(APP.db.session, "get", lambda _model, _id: user)
    return APP.load_user


def test_the_current_cookie_is_accepted(monkeypatch):
    u = _DBUser(token="tok-A")
    assert _loader_with(monkeypatch, u)("1|tok-A") is u


def test_a_superseded_cookie_is_rejected(monkeypatch):
    """The eviction itself: device 1 holds tok-A, device 2 logs in and the row
    moves to tok-B. Device 1's next request must not resolve to a user."""
    u = _DBUser(token="tok-B")            # someone else just logged in
    assert _loader_with(monkeypatch, u)("1|tok-A") is None


def test_an_empty_or_forged_token_is_rejected(monkeypatch):
    u = _DBUser(token="tok-A")
    load = _loader_with(monkeypatch, u)
    assert load("1|") is None
    assert load("1|guessed") is None


def test_a_legacy_cookie_still_works_once(monkeypatch):
    """Cookies issued BEFORE this feature carry no '|'. Rejecting them would
    log every existing user out on deploy for no security gain — the next
    login mints a versioned one."""
    u = _DBUser(token="tok-A")
    assert _loader_with(monkeypatch, u)("1") is u


def test_an_account_that_never_logged_in_since_upgrade_is_allowed(monkeypatch):
    """session_token is NULL on existing rows until their first login."""
    u = _DBUser(token=None)
    assert _loader_with(monkeypatch, u)("1|anything") is u


def test_a_missing_user_resolves_to_nothing(monkeypatch):
    assert _loader_with(monkeypatch, None)("1|tok-A") is None


def test_a_junk_cookie_never_raises(monkeypatch):
    load = _loader_with(monkeypatch, _DBUser())
    for junk in ("", "abc", "|", "abc|def", "1|2|3"):
        load(junk)          # must not raise — a bad cookie is not a 500


def test_login_rotates_the_token_so_other_devices_lose_access(monkeypatch):
    """End of the loop: logging in must CHANGE the token, or nothing is
    evicted and the whole feature is decorative."""
    monkeypatch.setattr(APP, "User", _FakeUserModel)
    monkeypatch.setattr(APP, "check_password_hash", lambda *_a: True)
    monkeypatch.setattr(APP.db.session, "commit", lambda: None)
    # _FakeQuery.first() mints a NEW _FakeUser per call, so pin ONE instance —
    # otherwise login() rotates a different object than this test inspects and
    # the assertion is vacuous.
    user = _FakeUser()
    user.session_token = "old-token"
    monkeypatch.setattr(_FakeQuery, "first", lambda _self: user)
    monkeypatch.setattr(APP, "login_user", lambda u, **kw: True)
    APP._login_fails.clear()                      # not rate-limited by neighbours
    client = APP.app.test_client()
    with client.session_transaction() as sess:    # same CSRF idiom as above
        sess["_csrf_token"] = "tok"
    resp = client.post("/login", data={"username": "u", "password": "p",
                                       "csrf_token": "tok"})
    assert resp.status_code == 302, f"login did not succeed: {resp.status_code}"
    assert user.session_token not in (None, "", "old-token"), \
        "login did not rotate the session token — no device would be evicted"
