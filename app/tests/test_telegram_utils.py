"""telegram_utils — pacing, 429 retry, long-message chunking.

2026-07-12 hardening after live message loss: an 80-signal digest hit the
4096-char limit (400, whole message dropped) and bursts of HC alerts hit the
~20 msg/min group limit (429, dropped with no retry)."""
import telegram_utils as T


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.text = str(self._body)
        self.ok = status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)


def _wire(monkeypatch, responses, calls, sleeps):
    """Route send_message at a fake group chat and a scripted requests.post."""
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setitem(T._TOPIC_THREAD, "alerts", "3")
    monkeypatch.setattr(T, "_last_send_ts", 0.0)

    def fake_post(url, data=None, timeout=None):
        calls.append(data)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(T.requests, "post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda s: sleeps.append(s))


# ── chunking ─────────────────────────────────────────────────────────────────
def test_chunks_short_message_passthrough():
    assert T._chunks_of("hello") == ["hello"]


def test_chunks_split_on_line_boundaries_lossless():
    msg = "\n".join(f"row {i} " + "x" * 90 for i in range(60))   # ~6k chars
    parts = T._chunks_of(msg)
    assert len(parts) >= 2
    assert all(len(p) <= T.CHUNK_LEN for p in parts)
    assert "\n".join(parts) == msg                    # nothing lost or reordered


def test_chunks_hard_split_monster_line():
    msg = "y" * 9000                                  # no newlines at all
    parts = T._chunks_of(msg)
    assert all(len(p) <= T.CHUNK_LEN for p in parts)
    assert "".join(parts) == msg


# ── 429 handling ─────────────────────────────────────────────────────────────
def test_429_retries_and_succeeds(monkeypatch):
    calls, sleeps = [], []
    _wire(monkeypatch,
          [FakeResp(429, {"ok": False, "parameters": {"retry_after": 7}}),
           FakeResp(200)], calls, sleeps)
    assert T.send_message("hi", force=True) is True
    assert len(calls) == 2
    assert any(s >= 7 for s in sleeps)                # honoured retry_after


def test_429_gives_up_after_retries(monkeypatch):
    calls, sleeps = [], []
    _wire(monkeypatch,
          [FakeResp(429, {"ok": False, "parameters": {"retry_after": 1}})],
          calls, sleeps)
    assert T.send_message("hi", force=True, retries=2) is False
    assert len(calls) == 3                            # initial + 2 retries


def test_400_is_final_no_retry(monkeypatch):
    calls, sleeps = [], []
    _wire(monkeypatch, [FakeResp(400, {"ok": False, "description": "too long"})],
          calls, sleeps)
    assert T.send_message("hi", force=True) is False
    assert len(calls) == 1


# ── chunked sends ────────────────────────────────────────────────────────────
def test_long_message_sent_in_parts(monkeypatch):
    calls, sleeps = [], []
    _wire(monkeypatch, [FakeResp(200)], calls, sleeps)
    msg = "\n".join(f"sig {i} " + "z" * 80 for i in range(120))  # ~10k chars
    assert T.send_message(msg, force=True) is True
    assert len(calls) == len(T._chunks_of(msg)) >= 3
    assert all(len(c["text"]) <= T.MAX_LEN for c in calls)


# ── pacing ───────────────────────────────────────────────────────────────────
def test_consecutive_sends_are_paced(monkeypatch):
    calls, sleeps = [], []
    _wire(monkeypatch, [FakeResp(200)], calls, sleeps)
    clock = [1000.0]
    monkeypatch.setattr(T.time, "time", lambda: clock[0])
    assert T.send_message("one", force=True) is True
    assert not sleeps                                 # first send: no wait
    assert T.send_message("two", force=True) is True  # same instant → must wait
    assert sleeps and abs(sleeps[0] - T.MIN_GAP_SEC) < 0.1


# ── sent-message ledger + /clean ─────────────────────────────────────────────
def test_send_message_records_message_id(monkeypatch, tmp_path):
    calls, sleeps = [], []
    _wire(monkeypatch,
          [FakeResp(200, {"ok": True, "result": {"message_id": 55}})],
          calls, sleeps)
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "ledger.json"))
    assert T.send_message("hi", force=True) is True
    log = T._load_sent()
    assert len(log) == 1
    assert log[0]["mid"] == 55 and log[0]["chat"] == "-100123"


NOW = 1_783_800_000.0     # 2026-07 — realistic epoch


def _seed_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(T.time, "time", lambda: NOW)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    T._save_sent([
        {"ts": NOW - 1 * 3600, "chat": 1, "mid": 10, "bot": "main"},    # keep
        {"ts": NOW - 30 * 3600, "chat": 1, "mid": 11, "bot": "main"},   # delete
        {"ts": NOW - 60 * 3600, "chat": 1, "mid": 12, "bot": "main"},   # >48h
    ])


def test_clean_deletes_between_24h_and_48h_only(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path)
    deletes = []

    def fake_post(url, data=None, timeout=None):
        deletes.append((url, data))
        return FakeResp(200, {"ok": True, "result": True})

    monkeypatch.setattr(T.requests, "post", fake_post)
    summary = T.clean_old_messages(24)
    assert summary == {"deleted": 1, "too_old": 1, "kept": 1, "failed": 0}
    assert len(deletes) == 1
    assert deletes[0][1]["message_id"] == 11
    assert "deleteMessage" in deletes[0][0]
    assert [e["mid"] for e in T._load_sent()] == [10]   # only the fresh one kept


def test_deep_clean_sweeps_ids_below_first_ledger_entry(monkeypatch):
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    tried = []

    def fake_post(url, data=None, timeout=None):
        tried.append(data["message_id"])
        # mid 2 is "too old / already gone" → 400; the rest delete fine
        return FakeResp(400 if data["message_id"] == 2 else 200,
                        {"ok": data["message_id"] != 2})

    monkeypatch.setattr(T.requests, "post", fake_post)
    s = T.deep_clean("-100123", upto_mid=5, limit=3)
    assert tried == [4, 3, 2]                 # newest-first, capped, below upto
    assert s == {"deleted": 2, "skipped": 1, "tried": 3}


def test_clean_treats_400_as_gone_but_retries_5xx(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(T.requests, "post",
                        lambda *a, **k: FakeResp(400, {"ok": False}))
    assert T.clean_old_messages(24)["deleted"] == 1     # gone is gone — dropped

    _seed_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(T.requests, "post",
                        lambda *a, **k: FakeResp(500, {"ok": False}))
    summary = T.clean_old_messages(24)
    assert summary["failed"] == 1 and summary["deleted"] == 0
    assert 11 in [e["mid"] for e in T._load_sent()]     # kept for next retry


def test_clean_preserves_messages_appended_during_the_sweep(monkeypatch, tmp_path):
    """A message the bot sends WHILE the ~90s delete sweep runs must survive —
    the final save re-merges instead of overwriting with a stale snapshot."""
    _seed_ledger(monkeypatch, tmp_path)

    def fake_post(url, data=None, timeout=None):
        # simulate a concurrent send landing in the ledger mid-sweep, right
        # when mid 11's delete goes out
        if data.get("message_id") == 11:
            T._record_sent(2, 99, bot="main", ts=NOW)
        return FakeResp(200, {"ok": True, "result": True})

    monkeypatch.setattr(T.requests, "post", fake_post)
    summary = T.clean_old_messages(24)
    mids = sorted(e["mid"] for e in T._load_sent())
    assert summary["deleted"] == 1
    assert mids == [10, 99]          # 11 deleted, 12 too-old dropped, 10 + the new 99 kept


# ── auto-clean: async, once per day ──────────────────────────────────────────
def _autoclean_wired(monkeypatch, tmp_path, ran):
    monkeypatch.setattr(T, "AUTO_CLEAN", True)
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setattr(T, "_AC_STATE", str(tmp_path / "ac.json"))
    # run "async" synchronously in the test so we can assert on it
    monkeypatch.setattr(T.threading, "Thread",
                        lambda target, args=(), **kw: type("T", (), {
                            "start": lambda self: ran.append(args) or target(*args)})())


def test_auto_clean_tick_runs_once_then_gated(monkeypatch, tmp_path):
    from datetime import datetime
    ran = []
    _autoclean_wired(monkeypatch, tmp_path, ran)
    cleans = []
    monkeypatch.setattr(T, "clean_old_messages",
                        lambda h: cleans.append(h) or {"deleted": 0, "too_old": 0,
                                                       "kept": 0, "failed": 0})
    at_5am = datetime(2026, 7, 23, 5, 0)
    assert T.auto_clean_tick(at_5am) is True
    assert cleans == [24]                                  # kicked off the clean
    assert T.auto_clean_tick(at_5am) is False              # same day → no re-run
    assert cleans == [24]


def test_auto_clean_tick_waits_for_the_hour(monkeypatch, tmp_path):
    from datetime import datetime
    ran = []
    _autoclean_wired(monkeypatch, tmp_path, ran)
    monkeypatch.setattr(T, "AUTO_CLEAN_HOUR", 4)
    assert T.auto_clean_tick(datetime(2026, 7, 23, 3, 0)) is False   # before 04:00
    assert ran == []


# ── private channel (owner DM) ───────────────────────────────────────────────
def test_private_channel_goes_to_owner_dm_not_group(monkeypatch, tmp_path):
    calls, sleeps = [], []
    _wire(monkeypatch, [FakeResp(200, {"ok": True, "result": {"message_id": 1}})],
          calls, sleeps)
    monkeypatch.setattr(T, "CHAT_ID", "777")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    assert T.send_message("secret report", channel="private")
    assert calls[0]["chat_id"] == "777"
    assert "message_thread_id" not in calls[0]


def test_report_channel_still_goes_to_group_topic(monkeypatch, tmp_path):
    calls, sleeps = [], []
    _wire(monkeypatch, [FakeResp(200, {"ok": True, "result": {"message_id": 2}})],
          calls, sleeps)
    monkeypatch.setitem(T._TOPIC_THREAD, "report", "214")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    assert T.send_message("group stuff", channel="report")
    assert calls[0]["chat_id"] == "-100123"
    assert calls[0]["message_thread_id"] == "214"


# ── pin / unpin ──────────────────────────────────────────────────────────────
def test_pin_message_success(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or FakeResp(200))
    assert T.pin_message("-100123", 42) is True
    url, kw = calls[0]
    assert url.endswith("/pinChatMessage")
    assert kw["data"] == {"chat_id": "-100123", "message_id": 42,
                          "disable_notification": True}


def test_pin_message_no_admin_rights_returns_false(monkeypatch):
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T.requests, "post",
                        lambda url, **kw: FakeResp(400, {"description": "not enough rights"}))
    assert T.pin_message("-100123", 42) is False


def test_pin_message_network_error_returns_false(monkeypatch):
    import requests
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")

    def boom(url, **kw):
        raise requests.RequestException("timeout")
    monkeypatch.setattr(T.requests, "post", boom)
    assert T.pin_message("-100123", 42) is False


def test_unpin_message_success(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or FakeResp(200))
    assert T.unpin_message("-100123", 41) is True
    assert calls[0][0].endswith("/unpinChatMessage")
    assert calls[0][1]["data"] == {"chat_id": "-100123", "message_id": 41}


# ── send + pin (uses the SAME routed chat/token as send_message) ─────────────
def test_send_message_and_pin_uses_routed_chat_id(monkeypatch, tmp_path):
    posts = []
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setitem(T._TOPIC_THREAD, "report", "214")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    monkeypatch.setattr(T, "_last_send_ts", 0.0)

    def fake_post(url, data=None, timeout=None):
        posts.append((url, data))
        if url.endswith("/sendMessage"):
            return FakeResp(200, {"ok": True, "result": {"message_id": 55}})
        return FakeResp(200)                    # pin/unpin
    monkeypatch.setattr(T.requests, "post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)

    mid = T.send_message_and_pin("today's brief", force=True, channel="report")
    assert mid == 55
    send_call = next(u for u, d in posts if u.endswith("/sendMessage"))
    pin_call = next((u, d) for u, d in posts if u.endswith("/pinChatMessage"))
    assert send_call.endswith("/sendMessage")
    assert pin_call[1]["chat_id"] == "-100123" and pin_call[1]["message_id"] == 55


def test_send_message_and_pin_unpins_previous(monkeypatch, tmp_path):
    posts = []
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setitem(T._TOPIC_THREAD, "report", "214")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    monkeypatch.setattr(T, "_last_send_ts", 0.0)

    def fake_post(url, data=None, timeout=None):
        posts.append((url, data))
        if url.endswith("/sendMessage"):
            return FakeResp(200, {"ok": True, "result": {"message_id": 66}})
        return FakeResp(200)
    monkeypatch.setattr(T.requests, "post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)

    mid = T.send_message_and_pin("tomorrow's brief", force=True, channel="report",
                                 unpin_previous=55)
    assert mid == 66
    unpin_call = next(d for u, d in posts if u.endswith("/unpinChatMessage"))
    assert unpin_call == {"chat_id": "-100123", "message_id": 55}


def test_send_message_and_pin_returns_none_on_send_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setitem(T._TOPIC_THREAD, "report", "214")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    monkeypatch.setattr(T, "_last_send_ts", 0.0)
    monkeypatch.setattr(T.requests, "post", lambda url, **kw: FakeResp(400))
    monkeypatch.setattr(T.time, "sleep", lambda s: None)

    assert T.send_message_and_pin("x", force=True, channel="report") is None


def test_send_message_and_pin_still_returns_mid_when_pin_fails(monkeypatch, tmp_path):
    """The message got through — that's what callers care about for their
    'was this sent' state. A pin failure (no admin rights yet) is logged,
    not fatal."""
    monkeypatch.setattr(T, "BOT_TOKEN", "tok")
    monkeypatch.setattr(T, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setitem(T._TOPIC_THREAD, "report", "214")
    monkeypatch.setattr(T, "SENT_LOG", str(tmp_path / "sent.json"))
    monkeypatch.setattr(T, "_last_send_ts", 0.0)

    def fake_post(url, data=None, timeout=None):
        if url.endswith("/sendMessage"):
            return FakeResp(200, {"ok": True, "result": {"message_id": 77}})
        return FakeResp(403, {"description": "not enough rights"})   # pin fails
    monkeypatch.setattr(T.requests, "post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)

    assert T.send_message_and_pin("x", force=True, channel="report") == 77


def test_run_clean_async_skips_when_already_running(monkeypatch):
    """The non-blocking lock means a second overlapping clean is a no-op —
    belt-and-suspenders beside the daily state gate."""
    calls = []
    monkeypatch.setattr(T, "clean_old_messages",
                        lambda h: calls.append(h) or {"deleted": 0, "too_old": 0,
                                                      "kept": 0, "failed": 0})
    assert T._ac_thread_lock.acquire(blocking=False)      # simulate a clean in flight
    try:
        T._run_clean_async(24)
        assert calls == []                                # skipped, didn't run
    finally:
        T._ac_thread_lock.release()
    T._run_clean_async(24)                                # lock free now → runs
    assert calls == [24]
