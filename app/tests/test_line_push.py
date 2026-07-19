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
