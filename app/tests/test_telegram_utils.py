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
