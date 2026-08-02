"""🔗 site_link — tunnel-URL announcements to TG + LINE, and the link commands."""
import config
import line_push
import site_link
import telegram_utils

URL = "https://post-catherine-arabic-variables.trycloudflare.com"


def _wire(monkeypatch, url=URL, tg_ok=True, line_ok=True, line_on=True):
    """Fake tunnel + recorders for both channels; returns (tg_calls, line_calls)."""
    tg_calls, line_calls = [], []
    monkeypatch.setattr(line_push, "current_tunnel_url", lambda: url)
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **kw: tg_calls.append(msg) or tg_ok)
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "tok" if line_on else "")
    monkeypatch.setattr(line_push, "send",
                        lambda text: line_calls.append(text) or line_ok)
    return tg_calls, line_calls


def test_tick_announces_new_url_to_both_channels(monkeypatch):
    tg, ln = _wire(monkeypatch)
    assert site_link.tick() is True
    assert len(tg) == 1 and URL in tg[0]
    assert len(ln) == 1 and URL in ln[0]
    # same URL again → silent on both
    assert site_link.tick() is False
    assert len(tg) == 1 and len(ln) == 1


def test_tick_reannounces_on_url_change(monkeypatch):
    tg, ln = _wire(monkeypatch)
    site_link.tick()
    monkeypatch.setattr(line_push, "current_tunnel_url",
                        lambda: "https://new-name.trycloudflare.com")
    assert site_link.tick() is True
    assert "new-name" in tg[1] and "new-name" in ln[1]


def test_failed_channel_retries_without_respamming_the_other(monkeypatch):
    tg, ln = _wire(monkeypatch, line_ok=False)
    site_link.tick()
    assert len(tg) == 1 and len(ln) == 1        # LINE tried and failed
    # next sweep: LINE retries, Telegram already recorded → no duplicate
    monkeypatch.setattr(line_push, "send", lambda text: ln.append(text) or True)
    assert site_link.tick() is True
    assert len(tg) == 1 and len(ln) == 2


def test_tick_silent_without_tunnel(monkeypatch):
    tg, ln = _wire(monkeypatch, url="")
    assert site_link.tick() is False
    assert not tg and not ln


def test_line_disabled_only_tracks_telegram(monkeypatch):
    tg, ln = _wire(monkeypatch, line_on=False)
    assert site_link.tick() is True
    assert len(tg) == 1 and not ln


def test_link_reply_and_commands(monkeypatch):
    _wire(monkeypatch)
    assert URL in site_link.link_reply()
    # LINE 網址 command
    assert URL in line_push._command_reply("網址")
    # TG /link command
    import tg_commands
    assert URL in tg_commands.handle("link")


def test_link_reply_without_tunnel(monkeypatch):
    _wire(monkeypatch, url="")
    assert "偵測不到" in site_link.link_reply()


# ── stable link (Tailscale Funnel) ───────────────────────────────────────────
# A permanent URL changes what the copy is allowed to promise: the old text
# told the family "the link will change on reboot and we'll send the new one",
# which becomes a lie people plan around once the URL is fixed.
STABLE = "https://shihbochuns-mac-mini.tail902e9c.ts.net"


def test_public_base_url_wins_over_the_tunnel(monkeypatch):
    monkeypatch.setattr(line_push, "current_tunnel_url", lambda: URL)
    monkeypatch.setenv("PUBLIC_BASE_URL", STABLE)
    assert site_link.current_url() == STABLE
    assert site_link.is_stable() is True


def test_trailing_slash_is_trimmed(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", STABLE + "/")
    assert site_link.current_url() == STABLE


def test_line_webhook_base_still_works(monkeypatch):
    """Back-compat: it did this job before there was a general name for it."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("LINE_WEBHOOK_BASE", STABLE)
    assert site_link.current_url() == STABLE
    assert site_link.is_stable() is True


def test_falls_back_to_the_tunnel_when_unset(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LINE_WEBHOOK_BASE", raising=False)
    monkeypatch.setattr(line_push, "current_tunnel_url", lambda: URL)
    assert site_link.current_url() == URL
    assert site_link.is_stable() is False


def test_stable_copy_does_not_promise_a_new_link(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", STABLE)
    for text in (site_link.announce_text(STABLE), site_link.link_reply()):
        assert STABLE in text
        assert "固定網址" in text
        assert "會更換" not in text, "stable link must not promise it will change"


def test_ephemeral_copy_still_warns(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LINE_WEBHOOK_BASE", raising=False)
    monkeypatch.setattr(line_push, "current_tunnel_url", lambda: URL)
    assert "會更換" in site_link.link_reply()
