"""Restart control: which files make a process stale, when the nudge fires,
and what the outcome message is allowed to claim.

The two things worth guarding here are both honesty properties. A restart that
aborts on red tests leaves the OLD stack running and untouched, so its message
must not read like an outage. And the staleness list is DERIVED from imports
precisely because the hand-written one it replaced was wrong in both
directions for months without anybody noticing.
"""
from datetime import datetime, timedelta

import pytest

import restart_ctl as R


# ── the import closure (what "stale" actually means) ─────────────────────────
def test_closure_reaches_lazily_imported_modules(tmp_path):
    """Function-local imports count. This codebase imports inside functions
    everywhere (tg_commands.handle() alone has a dozen), so a top-level-only
    scan would call a process 'current' while the module it loads on demand
    has changed underneath it."""
    (tmp_path / "entry.py").write_text(
        "import os\n"
        "def go():\n"
        "    import deep\n"
        "    from deeper import thing\n"
        "    return deep, thing\n")
    (tmp_path / "deep.py").write_text("x = 1\n")
    (tmp_path / "deeper.py").write_text("thing = 2\n")
    monkeypatched = R.APP
    try:
        R.APP = str(tmp_path)
        R._closure_cache["ts"] = 0
        got = set(R.sources("entry.py"))
    finally:
        R.APP = monkeypatched
        R._closure_cache["ts"] = 0
    assert got == {"entry.py", "deep.py", "deeper.py", ".env"}
    assert "os.py" not in got          # stdlib is not ours to watch


def test_closure_is_transitive_and_terminates_on_cycles(tmp_path):
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("import c\n")
    (tmp_path / "c.py").write_text("import a\n")     # cycle
    old = R.APP
    try:
        R.APP = str(tmp_path)
        R._closure_cache["ts"] = 0
        got = set(R.sources("a.py"))
    finally:
        R.APP = old
        R._closure_cache["ts"] = 0
    assert got == {"a.py", "b.py", "c.py", ".env"}


def test_real_entry_points_cover_their_own_dependencies():
    """The regression the hand list actually had: strategy3_scanner imports
    strategy3_risk (the circuit breaker) and strategy_ledger, and neither was
    listed — so editing the breaker left /health saying S3 was up to date."""
    s3 = set(R.sources("strategy3_scanner.py"))
    for must in ("strategy3_scanner.py", "strategy3_exec.py", "strategy3_risk.py",
                 "strategy_ledger.py", "config.py", "telegram_utils.py", ".env"):
        assert must in s3, f"{must} missing from the S3 source set"


def test_env_is_always_watched():
    for _key, _pattern, entry in R.PROCS:
        assert ".env" in R.sources(entry)


# ── mtime → stale ────────────────────────────────────────────────────────────
def test_changed_since_ignores_files_older_than_the_process(tmp_path):
    (tmp_path / "entry.py").write_text("x = 1\n")
    old = R.APP
    try:
        R.APP = str(tmp_path)
        R._closure_cache["ts"] = 0
        future = (tmp_path / "entry.py").stat().st_mtime + 3600
        assert R.changed_since("entry.py", future, str(tmp_path)) == []
        past = (tmp_path / "entry.py").stat().st_mtime - 3600
        assert R.changed_since("entry.py", past, str(tmp_path)) == ["entry.py"]
    finally:
        R.APP = old
        R._closure_cache["ts"] = 0


def test_a_process_that_is_not_running_is_never_stale(monkeypatch):
    """Down is the watchdog's louder, separate problem. Reporting a dead
    process as 'running old code' would send the operator to restart something
    the alert already told them to restart."""
    monkeypatch.setattr(R, "ps_snapshot", lambda: [])
    assert R.stale() == []


def test_stale_reports_the_running_process_and_its_changed_files(monkeypatch, tmp_path):
    started = datetime.now() - timedelta(hours=5)
    monkeypatch.setattr(R, "ps_snapshot", lambda: [
        {"pid": 42, "started": started, "rss_kb": 1,
         "cmd": "/usr/bin/python -u strategy3_scanner.py"}])
    monkeypatch.setattr(R, "changed_since", lambda *a, **k: ["strategy3_risk.py"])
    got = R.stale()
    assert got == [{"key": "s3", "label": "S3 flip", "pid": 42,
                    "changed": ["strategy3_risk.py"]}]


# ── in-flight guard ──────────────────────────────────────────────────────────
def test_in_flight_expires_and_clears_on_finish():
    now = 1_000_000.0
    assert R.in_flight({"started_at": now - 10}, now) is True
    assert R.in_flight({"started_at": now - R.INFLIGHT_SEC - 1}, now) is False
    assert R.in_flight({"started_at": now - 10, "finished_at": now - 1}, now) is False
    assert R.in_flight({}, now) is False


def test_a_second_request_while_one_is_running_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(R, "LOG_FILE", str(tmp_path / "restart.log"))
    monkeypatch.setattr(R, "RUNNER", str(tmp_path / "restart.sh"))
    (tmp_path / "restart.sh").write_text("#!/bin/bash\n")
    calls = []

    ok, _ = R.request("first", spawn=lambda *a, **k: calls.append(a))
    assert ok is True and len(calls) == 1

    ok2, note = R.request("second", spawn=lambda *a, **k: calls.append(a))
    assert ok2 is False
    assert len(calls) == 1, "a double-tap must not launch two run_all.sh"
    assert "重啟中" in note


def test_request_spawns_detached_because_the_caller_is_about_to_die(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(R, "LOG_FILE", str(tmp_path / "restart.log"))
    monkeypatch.setattr(R, "RUNNER", str(tmp_path / "restart.sh"))
    (tmp_path / "restart.sh").write_text("#!/bin/bash\n")
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw

    assert R.request("why", spawn=fake)[0] is True
    # run_all.sh pkills the process that asked. Inheriting its session would
    # take the runner down with it and nothing would ever restart.
    assert seen["kw"]["start_new_session"] is True
    assert seen["argv"][1] == str(tmp_path / "restart.sh")
    assert "why" in seen["argv"]


def test_a_failed_spawn_does_not_leave_a_permanent_in_flight_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(R, "LOG_FILE", str(tmp_path / "restart.log"))
    monkeypatch.setattr(R, "RUNNER", str(tmp_path / "restart.sh"))
    (tmp_path / "restart.sh").write_text("#!/bin/bash\n")

    def boom(*a, **k):
        raise OSError("nope")

    ok, note = R.request("x", spawn=boom)
    assert ok is False and "失敗" in note
    assert R.in_flight() is False, "a spawn that never started must not block the next try"


def test_missing_runner_is_reported_not_silently_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(R, "RUNNER", str(tmp_path / "gone.sh"))
    ok, note = R.request("x", spawn=lambda *a, **k: None)
    assert ok is False and "gone.sh" in note


# ── the outcome message ──────────────────────────────────────────────────────
def test_a_failed_preflight_must_say_the_old_stack_is_still_running():
    """run_all.sh runs lint+tests BEFORE it kills anything, so rc != 0 means
    nothing was touched. A bare '重啟失敗' would read as an outage and send the
    owner running for a terminal at 3am for a system that is fine."""
    text = R.finish_text(1, [], "E   assert 1 == 2")
    assert "舊的程式還在跑" in text
    assert "沒有停機" in text


def test_success_names_what_came_back_up():
    text = R.finish_text(0, ["web", "bot", "s2", "s3"])
    assert text.startswith("✅")
    for key in ("web", "bot", "s2", "s3"):
        assert R.LABELS[key] in text


def test_success_with_nothing_running_does_not_claim_processes():
    assert "（沒有偵測到）" in R.finish_text(0, [])


def test_failure_tail_is_redacted(monkeypatch):
    """The tail is raw log output, and this project has already leaked a live
    bot token into a log once (requests puts the auth URL in exception text)."""
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "redact", lambda s: str(s).replace("SECRET", "***"))
    assert "SECRET" not in R.finish_text(2, [], "token=SECRET failed")


# ── the watchdog nudge ───────────────────────────────────────────────────────
ENTRY = [{"key": "s2", "label": "S2 scanner", "pid": 1, "changed": ["x.py"]}]


def test_nudge_stays_quiet_while_the_code_is_still_being_edited():
    import watchdog as W
    now = 1_000_000.0
    assert W.stale_due(ENTRY, now - 5, False, 0, now) is False
    assert W.stale_due(ENTRY, now - W.STALE_QUIET_SEC - 1, False, 0, now) is True


def test_nudge_stays_quiet_while_the_tree_is_dirty():
    """Uncommitted work is a half-finished batch, not a release."""
    import watchdog as W
    now = 1_000_000.0
    settled = now - W.STALE_QUIET_SEC - 1
    assert W.stale_due(ENTRY, settled, True, 0, now) is False
    assert W.stale_due(ENTRY, settled, False, 0, now) is True


def test_nudge_respects_its_cooldown_and_never_fires_on_nothing():
    import watchdog as W
    now = 1_000_000.0
    settled = now - W.STALE_QUIET_SEC - 1
    assert W.stale_due([], settled, False, 0, now) is False
    assert W.stale_due(ENTRY, settled, False, now - 60, now) is False
    assert W.stale_due(ENTRY, settled, False, now - W.STALE_COOLDOWN_SEC, now) is True


def test_nudge_goes_to_the_owner_dm_with_a_button(monkeypatch):
    """The message names which processes are behind and, with RESTART_AUTO off,
    changes nothing by itself — so it must carry the button that does."""
    import telegram_utils
    import watchdog as W
    sent = {}
    monkeypatch.setattr(W, "RESTART_AUTO", False)
    monkeypatch.setattr(R, "stale", lambda: ENTRY)
    monkeypatch.setattr(R, "in_flight", lambda **k: False)
    monkeypatch.setattr(R, "newest_change_ts", lambda e: 0)
    monkeypatch.setattr(R, "tree_dirty", lambda: False)
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda text, **kw: sent.update(text=text, **kw) or True)

    state = {}
    assert W.stale_tick(state, 9_000_000.0)
    assert sent["channel"] == "private", "the group must never be told to restart"
    assert "S2 scanner" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "x:restart"
    assert state["stale_notice"] == 9_000_000.0


def test_auto_mode_restarts_instead_of_asking(monkeypatch):
    import telegram_utils
    import watchdog as W
    sent, asked = {}, []
    monkeypatch.setattr(W, "RESTART_AUTO", True)
    monkeypatch.setattr(R, "stale", lambda: ENTRY)
    monkeypatch.setattr(R, "in_flight", lambda **k: False)
    monkeypatch.setattr(R, "newest_change_ts", lambda e: 0)
    monkeypatch.setattr(R, "tree_dirty", lambda: False)
    monkeypatch.setattr(R, "request", lambda reason: (asked.append(reason), (True, "ok"))[1])
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda text, **kw: sent.update(text=text, **kw) or True)
    assert W.stale_tick({}, 9_000_000.0)
    assert len(asked) == 1
    assert sent["channel"] == "private"


def test_nudge_is_skipped_while_a_restart_is_already_running(monkeypatch):
    import watchdog as W
    monkeypatch.setattr(R, "in_flight", lambda **k: True)
    monkeypatch.setattr(R, "stale", lambda: (_ for _ in ()).throw(
        AssertionError("must not even look")))
    assert W.stale_tick({}, 9_000_000.0) == ""


# ── the Telegram surface ─────────────────────────────────────────────────────
def test_restart_is_owner_only_not_merely_admin(monkeypatch):
    """A group admin is trusted to delete messages, not to bounce the live
    trading engine mid-position."""
    import tg_commands as T
    assert "restart" in T.OWNER_ONLY_COMMANDS
    monkeypatch.setattr(T, "OWNER_IDS", {"111"})
    monkeypatch.setattr(T, "_group_admin_ids", lambda: {"222"})
    assert T.authorized("restart", "111") is True
    assert T.authorized("restart", "222") is False
    assert T.authorized("restart", "333") is False


def test_restart_reply_goes_to_the_owners_dm():
    import tg_commands as T
    assert "restart" in T.PRIVATE_REPLY_COMMANDS


def test_action_callbacks_are_a_separate_protocol_from_refresh():
    """Refresh is idempotent; restart is not. Sharing the r: prefix would put a
    re-tappable button on the result of a restart."""
    import tg_commands as T
    assert T.parse_action_callback("x:restart") == "restart"
    assert T.parse_action_callback("x:rm -rf") is None      # unknown action
    assert T.parse_action_callback("r:positions:") is None
    assert T.parse_refresh_callback("x:restart") is None
    assert T.action_keyboard("restart")["inline_keyboard"][0][0]["callback_data"] == "x:restart"


def test_restart_check_reports_without_restarting(monkeypatch):
    import tg_commands as T
    monkeypatch.setattr(R, "stale", lambda: ENTRY)
    monkeypatch.setattr(R, "in_flight", lambda: False)
    monkeypatch.setattr(R, "request", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("'/restart check' must not restart anything")))
    out = T.handle("restart", "check")
    assert "S2 scanner" in out and "x.py" in out


def test_restart_command_asks_restart_ctl(monkeypatch):
    import tg_commands as T
    monkeypatch.setattr(R, "request", lambda reason: (True, f"go:{reason}"))
    assert T.handle("restart", "") == "go:telegram /restart"


def test_fmt_stale_says_all_clear_when_nothing_changed():
    import tg_commands as T
    assert T.fmt_stale([]).startswith("✅")
    assert "⏳" in T.fmt_stale(ENTRY, in_flight=True)


def test_the_action_button_is_removed_before_the_restart_runs(monkeypatch):
    """The button fires a process kill a few seconds later, so anything left
    until 'after' never happens — and a still-tappable button sitting in the
    chat history would restart the stack again tomorrow."""
    import tg_commands as T
    order = []
    monkeypatch.setattr(T, "allowed", lambda c: True)
    monkeypatch.setattr(T, "OWNER_IDS", {"111"})
    monkeypatch.setattr(T, "_edit_message",
                        lambda c, m, text, mode, markup: order.append(("edit", markup)))
    monkeypatch.setattr(T, "_answer_callback", lambda *a, **k: order.append(("answer",)))
    monkeypatch.setattr(T, "telegram_utils", type("_T", (), {
        "send_message": staticmethod(lambda *a, **k: order.append(("send",)))})())
    monkeypatch.setattr(R, "request", lambda reason: (order.append(("restart",)), (True, "ok"))[1])

    cb = {"id": "1", "data": "x:restart", "from": {"id": "111"},
          "message": {"chat": {"id": 5}, "message_id": 9, "text": "有新程式碼"}}
    T._handle_callback(cb)
    assert [o[0] for o in order] == ["edit", "answer", "restart", "send"]
    assert order[0][1] is None, "the keyboard must be dropped, not re-attached"


def test_a_non_owner_tap_is_refused(monkeypatch):
    import tg_commands as T
    answers = []
    monkeypatch.setattr(T, "allowed", lambda c: True)
    monkeypatch.setattr(T, "OWNER_IDS", {"111"})
    monkeypatch.setattr(T, "_answer_callback", lambda cid, text="": answers.append(text))
    monkeypatch.setattr(R, "request", lambda reason: (_ for _ in ()).throw(
        AssertionError("a stranger tapped the button and it ran")))
    cb = {"id": "1", "data": "x:restart", "from": {"id": "999"},
          "message": {"chat": {"id": 5}, "message_id": 9, "text": "x"}}
    T._handle_callback(cb)
    assert "⛔" in answers[0]


# ── the web button ───────────────────────────────────────────────────────────
def test_web_restart_rejects_a_post_with_no_csrf_token():
    import app as A
    r = A.app.test_client().post("/api/restart")
    assert r.status_code == 400          # protect_post_requests(), before the view


def test_web_restart_needs_a_logged_in_admin_even_with_a_valid_csrf_token(monkeypatch):
    """Clearing the CSRF hurdle must not be enough — a valid token is
    obtainable by anyone who can load /login."""
    import re as _re

    import app as A
    monkeypatch.setattr(R, "request", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an anonymous request restarted the live stack")))
    client = A.app.test_client()
    page = client.get("/login").get_data(as_text=True)
    token = _re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = client.post("/api/restart", data={"csrf_token": token})
    assert r.status_code in (302, 401, 403), "anonymous POST reached the restart"


@pytest.mark.parametrize("entry", [e for _k, _p, e in R.PROCS])
def test_every_watched_entry_module_exists(entry):
    import os
    assert os.path.isfile(os.path.join(R.APP, entry))
