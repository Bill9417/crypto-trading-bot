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


def test_welcome_join_button_follows_invite_config(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_INVITE_URL", "https://t.me/+abc123")
    html = _get().get_data(as_text=True)
    assert 'href="https://t.me/+abc123"' in html and "加入 Telegram" in html
    monkeypatch.setattr(config, "TELEGRAM_INVITE_URL", "")
    html = _get().get_data(as_text=True)
    assert "加入 Telegram" not in html               # no dead button when unset
    assert "會員登入" in html                        # members can still get in


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
