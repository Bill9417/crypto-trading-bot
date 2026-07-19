"""📱 LINE push — delivery mechanics + the plain-Chinese digest for family.

The autouse guard already stubs line_push._post; these tests layer their own
recorder on top to inspect what WOULD be sent. Nothing reaches LINE.
"""
from datetime import datetime

import config
import line_push
import tw_stocks


def _cap(monkeypatch, token="tok", to=None, codes=None):
    """Enable LINE with a recorder; returns the list of (path, payload) calls."""
    calls = []

    def _fake(path, payload):
        calls.append((path, payload))
        return (codes or [200]).pop(0) if codes else 200, "ok"

    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", token)
    monkeypatch.setattr(config, "LINE_TO", to or [])
    monkeypatch.setattr(line_push, "_post", _fake)
    return calls


# ── delivery ─────────────────────────────────────────────────────────────────
def test_disabled_without_token(monkeypatch):
    calls = _cap(monkeypatch, token="")
    assert line_push.send("hello") is False
    assert not calls
    assert line_push.enabled() is False


def test_broadcast_when_no_recipients(monkeypatch):
    calls = _cap(monkeypatch)
    assert line_push.send("台股訊號") is True
    assert len(calls) == 1
    path, payload = calls[0]
    assert path == "broadcast"
    assert payload == {"messages": [{"type": "text", "text": "台股訊號"}]}


def test_push_per_recipient(monkeypatch):
    calls = _cap(monkeypatch, to=["Uaaa", "Ubbb"])
    assert line_push.send("hi") is True
    assert [(p, pl["to"]) for p, pl in calls] == [("push", "Uaaa"), ("push", "Ubbb")]


def test_chunks_split_on_newlines():
    text = "\n".join(f"line {i} " + "x" * 400 for i in range(30))
    chunks = line_push._chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= line_push.MAX_LEN for c in chunks)
    assert "\n".join(chunks) == text            # nothing lost, split at newlines


def test_retry_on_429(monkeypatch):
    monkeypatch.setattr(line_push.time, "sleep", lambda s: None)
    calls = _cap(monkeypatch, codes=[429, 200])
    assert line_push.send("retry me") is True
    assert len(calls) == 2


def test_failure_returns_false_never_raises(monkeypatch):
    calls = _cap(monkeypatch, codes=[400])
    assert line_push.send("quota gone") is False
    assert len(calls) == 1


# ── self-minted stateless tokens ─────────────────────────────────────────────
class _TokResp:
    status_code = 200
    text = "ok"

    @staticmethod
    def json():
        return {"access_token": "minted-tok", "expires_in": 900}


def test_enabled_via_channel_id_and_secret(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_ID", "2010")
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "sec")
    assert line_push.enabled() is True


def test_token_minted_and_cached(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_ID", "2010")
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setattr(line_push, "_tok_cache", {"token": "", "exp": 0.0})
    mints = []
    monkeypatch.setattr(line_push.requests, "post",
                        lambda url, **kw: mints.append(url) or _TokResp())
    assert line_push._token() == "minted-tok"
    assert line_push._token() == "minted-tok"       # second call hits the cache
    assert len(mints) == 1 and "oauth2/v3/token" in mints[0]


def test_console_token_overrides_minting(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "console-tok")
    monkeypatch.setattr(line_push.requests, "post",
                        lambda url, **kw: (_ for _ in ()).throw(AssertionError("minted")))
    assert line_push._token() == "console-tok"


# ── webhook: LINE-group auto-subscribe ───────────────────────────────────────
def _join_event(gid="Cgroup12345", token="rt-1"):
    return {"events": [{"type": "join", "replyToken": token,
                        "source": {"type": "group", "groupId": gid}}]}


def test_group_join_subscribes_replies_and_notifies(monkeypatch):
    calls = _cap(monkeypatch)
    notes = []
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda text, **kw: notes.append(text) or True)
    assert line_push.handle_webhook(_join_event()) == 1
    assert "Cgroup12345" in line_push.targets()
    assert [(p, pl.get("replyToken")) for p, pl in calls] == [("reply", "rt-1")]
    assert calls[0][1]["messages"][0]["text"] == line_push.WELCOME_GROUP
    assert notes and "LINE 群組已連接" in notes[0]
    # …and send() now pushes to the group, not broadcast
    calls.clear()
    assert line_push.send("hi") is True
    assert [(p, pl.get("to")) for p, pl in calls] == [("push", "Cgroup12345")]


def test_group_join_is_idempotent(monkeypatch):
    calls = _cap(monkeypatch)
    line_push.handle_webhook(_join_event())
    calls.clear()
    assert line_push.handle_webhook(_join_event(token="rt-2")) == 0
    assert not calls                            # no second welcome reply
    assert line_push.targets().count("Cgroup12345") == 1


def test_group_leave_unsubscribes(monkeypatch):
    _cap(monkeypatch)
    line_push.handle_webhook(_join_event())
    leave = {"events": [{"type": "leave",
                         "source": {"type": "group", "groupId": "Cgroup12345"}}]}
    assert line_push.handle_webhook(leave) == 1
    assert "Cgroup12345" not in line_push.targets()


def test_signature_check(monkeypatch):
    import base64
    import hashlib
    import hmac as hmac_mod
    body = b'{"events":[]}'
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "")
    assert line_push.sig_ok(body, "") is True              # capture mode
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "secret")
    good = base64.b64encode(
        hmac_mod.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert line_push.sig_ok(body, good) is True
    assert line_push.sig_ok(body, "forged") is False
    assert line_push.sig_ok(body, "") is False


def test_webhook_route(monkeypatch):
    import base64
    import hashlib
    import hmac as hmac_mod

    import app as APP
    _cap(monkeypatch)
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "s3cret")
    import json as _json
    body = _json.dumps(_join_event("Croute999")).encode()
    sig = base64.b64encode(
        hmac_mod.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    with APP.app.test_client() as c:
        bad = c.post("/line/webhook", data=body,
                     content_type="application/json",
                     headers={"X-Line-Signature": "wrong"})
        assert bad.status_code == 403
        okr = c.post("/line/webhook", data=body,
                     content_type="application/json",
                     headers={"X-Line-Signature": sig})
        assert okr.status_code == 200
    assert "Croute999" in line_push.targets()


# ── text commands (free reply messages) ──────────────────────────────────────
def _text_event(text, gid="Cgroup12345", token="rt-9"):
    return {"events": [{"type": "message", "replyToken": token,
                        "message": {"type": "text", "text": text},
                        "source": {"type": "group", "groupId": gid}}]}


def _subscribed(monkeypatch):
    """Recorder with the group already subscribed (welcome consumed)."""
    calls = _cap(monkeypatch)
    line_push.handle_webhook(_join_event())
    calls.clear()
    return calls


def test_command_help(monkeypatch):
    calls = _subscribed(monkeypatch)
    line_push.handle_webhook(_text_event("說明"))
    assert [(p, pl["messages"][0]["text"]) for p, pl in calls] == \
        [("reply", line_push.HELP_MSG)]


def test_command_digest_from_state(monkeypatch):
    calls = _subscribed(monkeypatch)
    import tw_stocks
    monkeypatch.setattr(tw_stocks, "_load_state",
                        lambda: {"last_digest_plain": "🇹🇼 今日掃描內容"})
    line_push.handle_webhook(_text_event("訊號"))
    assert calls[0][1]["messages"][0]["text"] == "🇹🇼 今日掃描內容"


def test_command_digest_before_first_scan(monkeypatch):
    calls = _subscribed(monkeypatch)
    import tw_stocks
    monkeypatch.setattr(tw_stocks, "_load_state", lambda: {})
    line_push.handle_webhook(_text_event("台股"))
    assert "還沒有台股掃描" in calls[0][1]["messages"][0]["text"]


def test_command_snapshot(monkeypatch):
    calls = _subscribed(monkeypatch)
    import tw_intraday
    monkeypatch.setattr(tw_intraday, "snapshot_plain", lambda: "即時快照")
    line_push.handle_webhook(_text_event("現況"))
    assert calls[0][1]["messages"][0]["text"] == "即時快照"


def test_ordinary_chatter_stays_silent(monkeypatch):
    calls = _subscribed(monkeypatch)
    line_push.handle_webhook(_text_event("爸 晚餐吃什麼"))
    assert not calls                        # bots must not answer normal chat


def test_pause_and_resume(monkeypatch):
    calls = _subscribed(monkeypatch)
    line_push.handle_webhook(_text_event("暫停"))
    assert "已暫停" in calls[0][1]["messages"][0]["text"]
    assert "Cgroup12345" not in line_push.targets()
    # chatter while paused must NOT silently re-subscribe (nor re-welcome)
    calls.clear()
    line_push.handle_webhook(_text_event("隨便聊天"))
    assert not calls
    assert "Cgroup12345" not in line_push.targets()
    # …but 「開啟」 resumes
    line_push.handle_webhook(_text_event("開啟"))
    assert "已恢復" in calls[0][1]["messages"][0]["text"]
    assert "Cgroup12345" in line_push.targets()


def test_reinvite_after_pause_reactivates(monkeypatch):
    calls = _subscribed(monkeypatch)
    line_push.handle_webhook(_text_event("暫停"))
    calls.clear()
    line_push.handle_webhook(_join_event(token="rt-again"))   # kicked+re-invited
    assert "Cgroup12345" in line_push.targets()
    assert calls[0][1]["messages"][0]["text"] == line_push.WELCOME_GROUP


def test_clear_command_explains_limitation(monkeypatch):
    calls = _subscribed(monkeypatch)
    line_push.handle_webhook(_text_event("清除"))
    txt = calls[0][1]["messages"][0]["text"]
    assert "無法刪除" in txt and "暫停" in txt


def test_first_message_gets_welcome_not_command(monkeypatch):
    # One replyToken per event: if the group's first-ever event is a command,
    # the welcome wins and the command is NOT double-replied.
    calls = _cap(monkeypatch)
    line_push.handle_webhook(_text_event("說明", token="rt-first"))
    texts = [pl["messages"][0]["text"] for _p, pl in calls]
    assert texts == [line_push.WELCOME_GROUP]


def test_snapshot_plain_has_no_html(monkeypatch):
    import time as _time

    import tw_intraday
    import tw_stocks
    rows = [{"code": "2330", "name": "台積電", "price": 1050.0, "change_pct": 2.1},
            {"code": "2317", "name": "鴻海", "price": 210.0, "change_pct": -1.2}]
    monkeypatch.setattr(tw_intraday, "_quotes", lambda: (rows, _time.time()))
    monkeypatch.setattr(tw_intraday, "fetch_taiex",
                        lambda: {"price": 23000.0, "prev": 22800.0, "pct": 0.88})
    monkeypatch.setattr(tw_stocks, "_load_state",
                        lambda: {"active_setups": [{"code": "2330", "name": "台積電",
                                                    "date": "2026-07-18", "ref": 1040.0,
                                                    "sl": 990.0, "tp": 1150.0}]})
    snap = tw_intraday.snapshot_plain()
    assert "加權指數 23,000" in snap
    assert "2330 台積電 現價 1,050" in snap
    assert "進 1,040／損 990.0／標 1,150" in snap
    assert "<" not in snap                  # plain text, no <pre> tables


# ── the plain-Chinese digest ─────────────────────────────────────────────────
_NOW = datetime(2026, 7, 17, 14, 0)             # a Friday

_SETUP = {"ref": 1050.0, "sl": 990.0, "tp": 1150.0, "atr": 20.0,
          "turnover": 1e9}
_REG_BULL = {"ok": True, "close": 23000.0, "sma100": 22000.0, "mom20": 0.03}
_REG_BEAR = {"ok": False, "close": 21000.0, "sma100": 22000.0, "mom20": -0.02}


def test_plain_digest_bull_lists_levels():
    msg = tw_stocks.build_digest_plain(_NOW, _REG_BULL, [("2330", "台積電", _SETUP)])
    assert "2330 台積電" in msg
    assert "進場參考 1,050" in msg
    assert "停損 990" in msg and "−5.7%" in msg
    assert "目標 1,150" in msg and "+9.5%" in msg
    assert "40 個交易日" in msg
    assert "<" not in msg                        # plain text — no HTML/<pre>


def test_plain_digest_bear_says_stand_aside():
    msg = tw_stocks.build_digest_plain(_NOW, _REG_BEAR, [])
    assert "觀望" in msg
    assert "100日均線之下" in msg
    assert "停損" not in msg                     # no setups listed in a bear regime


def test_plain_digest_always_carries_risk_note():
    for reg, setups in ((_REG_BULL, []), (_REG_BEAR, [])):
        assert "過去績效不代表未來" in tw_stocks.build_digest_plain(_NOW, reg, setups)
