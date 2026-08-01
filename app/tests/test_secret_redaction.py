"""No credential may reach a log file or the /health page.

requests puts the FULL request URL into its exception messages, and every
Telegram API URL embeds the bot token. `[tgcmd] poll error: 429 ... for url:
https://api.telegram.org/bot<TOKEN>/getUpdates` therefore wrote a live
credential into app/logs/strategy2.log — which /health tails and renders as
`last_error` at 220 chars, comfortably enough for a whole token.

Two layers, tested separately: emitters redact at the source, and _scrub()
catches whatever is already on disk or written by a module that forgets.
"""
import app as A
import telegram_utils as T

TOKEN = "1234567890:AAFAKEfakeFAKE-not-a-real-token-xyz01"
POLL_ERR = ("[tgcmd] poll error: 429 Client Error: Too Many Requests for url: "
            f"https://api.telegram.org/bot{TOKEN}/getUpdates")


# ── layer 1: the emitter ─────────────────────────────────────────────────────
def test_redact_removes_a_bot_token_from_a_url():
    out = T.redact(POLL_ERR)
    assert TOKEN not in out
    assert "AAFqH4y" not in out                 # not even the secret half
    assert "bot***/getUpdates" in out           # still readable as an error


def test_redact_keeps_everything_that_is_not_a_secret():
    assert "429 Client Error" in T.redact(POLL_ERR)
    plain = "[strategy2] volume gate failed for ABCUSDT"
    assert T.redact(plain) == plain


def test_redact_accepts_an_exception_object():
    exc = RuntimeError(POLL_ERR)
    assert TOKEN not in T.redact(exc)


def test_redact_handles_several_tokens_in_one_line():
    out = T.redact(f"a bot{TOKEN}/x and bot{TOKEN}/y")
    assert TOKEN not in out and out.count("bot***") == 2


# ── layer 2: the /health scrubber ────────────────────────────────────────────
def test_scrub_strips_a_token_already_written_to_disk():
    out = A._scrub(POLL_ERR)
    assert TOKEN not in out and "bot***" in out


def test_scrub_strips_generic_credential_query_params():
    for line in (f"GET /x?api_key={TOKEN}", "POST secret=hunter2&a=1",
                 "url?token=abc123def456&next=1", "?password=letmein"):
        out = A._scrub(line)
        assert "hunter2" not in out and "abc123def456" not in out
        assert "letmein" not in out and TOKEN not in out


def test_scrub_leaves_ordinary_trading_output_alone():
    for line in ("[strategy2] volume gate failed for ABCUSDT",
                 "[s3-exec] moved stop on XAUT/USDT:USDT → 4000",
                 "Operation not permitted"):
        assert A._scrub(line) == line


def test_health_page_never_renders_a_token(tmp_path, monkeypatch):
    """End to end: a log containing a token must not reach the browser."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_text(POLL_ERR + "\n", encoding="utf-8")
    monkeypatch.setattr(A, "_HEALTH_LOGS", ("app.log",))
    entry = A._log_health(str(tmp_path))[0]
    assert entry["recent_errors"] == 1                 # still counted as an error
    assert TOKEN not in (entry["last_error"] or "")
    assert TOKEN not in (entry["last_line"] or "")
    assert "bot***" in entry["last_error"]
