"""🐺 Public /welcome landing page — the shareable promo front door.

Two properties are non-negotiable:
  1. It renders WITHOUT auth (it's the only public page besides /login).
  2. It is account-free by construction — aggregated signal stats only,
     never balances / positions / per-account P&L.
"""
import app as APP
import config


def _get(path="/welcome"):
    client = APP.app.test_client()
    return client.get(path)


def test_welcome_renders_without_login(monkeypatch):
    monkeypatch.setitem(APP._public_stats_cache, "data", None)
    r = _get()
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "WOLF" in html and "誠實原則" in html
    assert "公開輸單" in html                       # the differentiator, above the fold
    assert "會員登入" in html


def test_welcome_has_og_tags_for_link_unfurl():
    html = _get().get_data(as_text=True)
    assert 'property="og:title"' in html
    assert 'property="og:image"' in html
    assert 'name="twitter:card"' in html


def test_welcome_never_contains_account_words():
    html = _get().get_data(as_text=True)
    for banned in ("餘額", "可用保證金", "未實現", "帳戶淨值"):
        assert banned not in html


def test_welcome_join_button_follows_the_live_invite(monkeypatch):
    """The seam moved 2026-08-11: the button used to read
    config.TELEGRAM_INVITE_URL straight from .env, and that value had gone
    stale — it was a REVOKED link, so every visitor who tapped Join was told
    the invite was invalid. It now comes from telegram_utils.group_invite_link(),
    which asks Telegram for the group's current primary link."""
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "group_invite_link",
                        lambda *a, **k: "https://t.me/+abc123")
    html = _get().get_data(as_text=True)
    assert 'href="https://t.me/+abc123"' in html and "加入 Telegram" in html
    monkeypatch.setattr(telegram_utils, "group_invite_link", lambda *a, **k: "")
    html = _get().get_data(as_text=True)
    assert "加入 Telegram" not in html               # no dead button when unset
    assert "會員登入" in html                        # members can still get in


def test_join_button_survives_telegram_being_unreachable(monkeypatch):
    """A join button must never 500 a public page, and must never vanish just
    because the API blipped — it falls back to the configured value."""
    import telegram_utils

    def _boom(*a, **k):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(telegram_utils, "group_invite_link", _boom)
    monkeypatch.setattr(config, "TELEGRAM_INVITE_URL", "https://t.me/+fallback")
    html = _get().get_data(as_text=True)
    assert 'href="https://t.me/+fallback"' in html


def test_public_stats_failsafe_and_cached(monkeypatch):
    import morning_brief

    calls = {"n": 0}

    def boom(_now):
        calls["n"] += 1
        raise RuntimeError("state file missing")

    monkeypatch.setattr(morning_brief, "_outcome_stat", boom)
    monkeypatch.setattr(morning_brief, "_signal_tally", boom)
    monkeypatch.setitem(APP._public_stats_cache, "data", None)
    monkeypatch.setitem(APP._public_stats_cache, "ts", 0.0)

    st = APP._public_stats()
    assert st == {"outcomes": {}, "signals": {}}     # degraded, not crashed
    APP._public_stats()                              # second call hits the cache
    assert calls["n"] == 2                           # both fetchers ran ONCE


def test_welcome_hides_tiny_outcome_sample(monkeypatch):
    # n<5 outcome stats are braggy noise — page must hide them, same floor as
    # the morning brief.
    monkeypatch.setitem(APP._public_stats_cache, "ts", 9e12)
    monkeypatch.setitem(APP._public_stats_cache, "data",
                        {"outcomes": {"n": 3, "hit_pct": 100.0}, "signals": {}})
    html = _get().get_data(as_text=True)
    assert "已結算訊號" not in html and "先到目標比例" not in html


# ── /join — the link that survives an in-app browser (2026-08-11) ───────────
# A private invite is t.me/+HASH, and Threads' in-app browser STRIPS the `+`.
# t.me then reads the rest as a username, finds nothing, and bounces to
# telegram.org's homepage — so the reader sees "a new era of messaging" instead
# of a Join button and assumes the group is dead. Measured that day:
#     t.me/+EXAMPLEINVITEHASH  → 200 Join Group Chat
#     t.me/EXAMPLEINVITEHASH   → 302 telegram.org      (same link, no +)
# Both invites were valid the whole time; the `+` was the bug.
def test_join_redirects_to_the_live_invite(monkeypatch):
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "group_invite_link",
                        lambda *a, **k: "https://t.me/+liveHash")
    r = APP.app.test_client().get("/join")
    assert r.status_code == 302
    assert r.headers["Location"] == "https://t.me/+liveHash"


def test_join_url_has_nothing_a_webview_can_mangle(monkeypatch):
    """The whole point: no '+', no query string, no reserved characters."""
    import threads_post as T
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.delenv("THREADS_LINK", raising=False)
    link = T.post_link()
    assert link.endswith("/join")
    assert "+" not in link and "?" not in link


def test_join_is_a_302_not_a_301():
    """A 301 is cached by the browser forever, which would pin every reader to
    whichever invite was live the first time they tapped — the exact staleness
    this route exists to remove."""
    r = APP.app.test_client().get("/join")
    assert r.status_code == 302


def test_join_never_dead_ends_when_no_invite_is_configured(monkeypatch):
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "group_invite_link", lambda *a, **k: "")
    monkeypatch.setattr(config, "TELEGRAM_INVITE_URL", "")
    r = APP.app.test_client().get("/join")
    assert r.status_code == 302
    assert "/welcome" in r.headers["Location"]      # a page, never a 404
