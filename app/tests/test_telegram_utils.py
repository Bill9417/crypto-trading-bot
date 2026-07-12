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
