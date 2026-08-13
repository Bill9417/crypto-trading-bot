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

# Captured BEFORE conftest's autouse guard stubs it out — see
# test_a_send_failure_cannot_log_the_token for why that distinction decides
# whether the test is real or vacuous.
_REAL_POST_ONE = T._post_one

# A FAKE token, and it must stay fake.
#
# 2026-08-09: this constant was the REAL live bot token. The test file written
# to prove that credentials never leak had one hardcoded in it, committed in
# b51a78b — the same commit that fixed the logging leak. It was never pushed
# (this repo is private and was 106 commits behind origin at the time), so the
# exposure stayed on one machine, but sync.sh pushes periodically and would
# have taken it to GitHub.
#
# The digits below are a keyboard walk and the secret half spells out what it
# is; nothing here authenticates anything. Redaction is a property of the token
# SHAPE (bot\d{6,}:[A-Za-z0-9_-]{20,}), so a fake of the right shape tests it
# exactly as well as a real one — there was never a reason to use the real one.
# test_the_fixture_token_is_not_a_real_credential enforces this.
TOKEN = "1234567890:AAFAKEfakeFAKE-not-a-real-token-xyz01"
POLL_ERR = ("[tgcmd] poll error: 429 Client Error: Too Many Requests for url: "
            f"https://api.telegram.org/bot{TOKEN}/getUpdates")


def test_the_fixture_token_is_not_a_real_credential():
    """The fixture must never again be a live secret. Checks against every
    long credential the running config actually holds, so this catches any
    future copy-paste of a real key into a test — not just the one that
    happened."""
    import config
    real = [getattr(config, n) for n in dir(config)
            if any(k in n for k in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))]
    real = [v for v in real if isinstance(v, str) and len(v) >= 12]
    for v in real:
        assert v not in TOKEN and v not in POLL_ERR, \
            "the test fixture contains a REAL credential from config"
    assert TOKEN.startswith("1234567890:"), "fixture token was changed to something unvetted"


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


# Having redact() is not the same as USING it. The poll-error emitter was fixed
# and tested; _post_one's send-failure emitter three hundred lines above it was
# not, and it wrote the live token to logs/strategy2.log 12 times — once per DNS
# failure and connect timeout. Found 2026-08-09 by grepping the real logs for
# the real token rather than by reading the code, which had looked fine.
#
# So this asserts the RULE, on every emitter at once: in a module whose every
# URL carries the credential, an exception may not be interpolated raw.
def test_no_emitter_in_telegram_utils_prints_a_raw_exception():
    import inspect
    import re
    src = inspect.getsource(T)
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        if "print(" not in line and not line.strip().startswith("f\""):
            continue
        for var in re.findall(r"\{(\w+)\}", line):
            if var in ("exc", "e", "err", "error") and f"redact({var})" not in line:
                # allow a continued f-string whose redact() sits on the next line
                bad.append((i, line.strip()[:88]))
    assert not bad, (
        "these interpolate an exception without redact(); a requests exception "
        f"carries the bot-token URL: {bad}")


def test_a_send_failure_cannot_log_the_token(monkeypatch, capsys):
    """The exact regression: a RequestException whose text holds the auth URL.

    _REAL_POST_ONE is captured at import time on purpose. conftest's autouse
    guard replaces telegram_utils._post_one with a stub for every test, so
    calling T._post_one here would exercise the stub and pass no matter how
    badly the real emitter leaked — which is the trap this test fell into first
    time round. The network seam inside it is patched instead, so nothing
    leaves the process either way.
    """
    import requests

    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
            f"exceeded with url: /bot{TOKEN}/sendMessage (Caused by NameResolution)")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(T, "_pace", lambda: None)

    ok, mid = _REAL_POST_ONE("https://api.telegram.org/botX/sendMessage",
                             {"chat_id": 1, "text": "hi"}, 0)
    assert ok is False
    out = capsys.readouterr().out
    assert "Telegram send failed" in out, "the emitter under test did not run"
    assert TOKEN not in out, f"send-failure emitter leaked the token: {out[:200]}"
    assert "bot***" in out, "redacted, but the error should still be readable"


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


# ── tg_format HTML must actually be rendered (2026-08-13) ───────────────────
# S4 shipped sending build_digest() — which is full of <pre> and <a href> from
# tg_format — with no parse_mode. Telegram delivered it happily, so every layer
# reported success while the owner received raw markup:
#     <pre>進場  0.06999 … </a>
# Nothing errors on this path. It has to be asserted.
import re as _re

import telegram_utils as _T


def _sends_html(path):
    """Does this module hand tg_format HTML to send_message?"""
    src = open(path, encoding="utf-8").read()
    builds = bool(_re.search(r"tg_format\.(pre_table|mono_plan|bybit_line)|"
                             r"\bF\.(pre_table|mono_plan|bybit_line)", src))
    return builds, src


def test_the_s4_digest_really_is_html_and_its_send_says_so():
    """The actual regression, checked at both ends rather than by grepping the
    whole repo for `send_message`.

    A repo-wide static rule was tried first and is not honest: it matched the
    words "send_message" inside comments, and it cannot tell a plain-text
    cascade alert from an HTML one without evaluating what was passed. Half the
    repo would have needed exemptions, and an exempt-list that long guards
    nothing. The runtime net below is the general protection; this pins the one
    that actually broke.
    """
    import inspect
    import strategy4 as S4

    sig = {"symbol": "DOGE/USDT:USDT", "base": "DOGE", "segment": "crypto",
           "side": "short", "price": 0.06999, "score": 26, "slope": -0.05,
           "quality": 50, "div_ago": 9, "div_sources": ["FISH", "KD"],
           "oi_state": 4, "bar_ts": 1,
           "support": {"level": 0.07079, "bars_ago": 14},
           "plan": {"entry": 0.06999, "sl": 0.070896, "tp": 0.068178,
                    "rr": 2, "stop_pct": 1.29, "tp_pct": 2.59, "side": "short"}}
    text = S4.format_signal(sig)
    assert "<pre>" in text, "the digest stopped being HTML — this test is stale"

    # AST, not a line grep. The first version of this scanned tick()'s source
    # lines for "parse_mode" + "HTML" and PASSED against the broken code — it
    # was matching the comment above the call that explains why parse_mode is
    # required. A test that a comment can satisfy tests nothing.
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(S4.tick)))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "send_message"]
    assert calls, "strategy4.tick no longer sends anything — this test is stale"
    for call in calls:
        kw = {k.arg: getattr(k.value, "value", None) for k in call.keywords}
        assert kw.get("parse_mode") == "HTML", (
            "strategy4 sends tg_format HTML without parse_mode='HTML' — "
            "Telegram prints <pre> and </a> as literal text")


def test_the_send_path_rescues_a_forgotten_parse_mode(monkeypatch):
    """The net. Sending <pre> as literal text is never what anyone wanted, so
    a caller that forgets gets upgraded rather than shipping broken markup."""
    seen = {}
    monkeypatch.setattr(_T, "_route",
                        lambda ch, force: ("tok", {"chat_id": "1"}, "bot"))
    monkeypatch.setattr(_T, "_post_one",
                        lambda url, body, retries: (seen.update(body), (True, 1))[1])
    _T.send_message("<pre>進場  0.069</pre>", channel="alerts")
    assert seen.get("parse_mode") == "HTML", "raw <pre> was sent as plain text"


def test_a_tappable_link_also_triggers_the_rescue(monkeypatch):
    seen = {}
    monkeypatch.setattr(_T, "_route",
                        lambda ch, force: ("tok", {"chat_id": "1"}, "bot"))
    monkeypatch.setattr(_T, "_post_one",
                        lambda url, body, retries: (seen.update(body), (True, 1))[1])
    _T.send_message('x · <a href="https://bybit.com">下單 ↗</a>', channel="alerts")
    assert seen.get("parse_mode") == "HTML"


def test_plain_text_is_left_alone(monkeypatch):
    """Upgrading a plain message would be a behaviour change, and an unescaped
    < or & in ordinary text would then 400 instead of sending."""
    seen = {}
    monkeypatch.setattr(_T, "_route",
                        lambda ch, force: ("tok", {"chat_id": "1"}, "bot"))
    monkeypatch.setattr(_T, "_post_one",
                        lambda url, body, retries: (seen.update(body), (True, 1))[1])
    _T.send_message("BTC 破 100k < 看多 & 續抱", channel="alerts")
    assert "parse_mode" not in seen
