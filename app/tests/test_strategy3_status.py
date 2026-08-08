"""
The 2026-08-08 question, frozen as tests: XAUT printed 97.5/100, Vegas green,
MSB bull, ALL-LONG multi-timeframe bias — and the bot did not go long. The
reason (a spent flag, plus a manual position that had burnt it four days
earlier) was recoverable only by hand. These tests pin the explanation.
"""
import pytest

import strategy3_scanner as S3
import strategy3_status as ST

FLAGFLIP = {"engine": "flagflip", "timeframe": "30m", "margin": 30.0,
            "leverage": 50, "sl_pct": 0.015, "feed": "bybit"}
OCC = {"engine": "occ", "timeframe": "30m", "res_mult": 3, "ma_len": 8,
       "margin": 30.0, "leverage": 10, "sl_pct": 0.03, "feed": "bybit"}

NOW = 1786155637.0          # 2026-08-08 10:20 Taipei — the moment of the screenshot


def live_xaut(**over):
    """The state file exactly as it read when the owner asked."""
    st = {"last_flag": "long", "consumed": True, "pos_dir": None,
          "last_score": 97.5, "last_vegas": 1, "last_msb": "bull",
          "last_price": 4323.0, "last_seen": NOW - 1200, "open_attempts": 0,
          "engine": "flagflip", "flag_ts": NOW - 93 * 3600}
    st.update(over)
    return st


# ── the actual question ──────────────────────────────────────────────────────

def test_a_perfect_looking_chart_with_a_spent_flag_explains_itself():
    info = ST.explain("XAUT", live_xaut(), FLAGFLIP, now=NOW)
    assert info["state"] == "spent"
    text = ST.card(info)
    # the three things the owner had to reconstruct by hand
    assert "3.9 天前" in text                     # WHEN the flag fired
    assert "做多 LONG" in text                    # WHICH direction it was
    assert "輪流" in text                         # WHY 97.5/100 changes nothing
    assert "97.5" in text and "綠" in text        # matches the chart, not rounded


def test_the_reason_an_entry_was_skipped_survives_in_the_card():
    """The 08-04 long was skipped because a manual position still existed.
    That fact used to live only in an untimestamped log line."""
    st = live_xaut(skip_reason="Bybit 上已經有這個標的的倉位（手動？），不去動它")
    info = ST.explain("XAUT", st, FLAGFLIP, now=NOW)
    assert info["state"] == "spent"
    assert "手動" in ST.card(info)


def test_score_and_vegas_are_labelled_as_not_a_trigger():
    """The whole misunderstanding in one line — say it out loud."""
    assert "不是新的進場訊號" in ST.card(ST.explain("XAUT", live_xaut(), FLAGFLIP, now=NOW))


# ── every other branch, mirrored against decide() ────────────────────────────

def test_holding_says_the_exit_is_the_opposite_flag():
    info = ST.explain("XAUT", live_xaut(pos_dir="long"), FLAGFLIP, now=NOW)
    assert info["state"] == "holding"
    assert "反向旗標" in info["next_step"]


def test_a_live_flag_waiting_on_vegas_is_not_reported_as_spent():
    st = live_xaut(consumed=False, last_vegas=-1)
    info = ST.explain("XAUT", st, FLAGFLIP, now=NOW)
    assert info["state"] == "waiting_vegas"


def test_a_live_flag_with_vegas_agreeing_is_arming():
    info = ST.explain("XAUT", live_xaut(consumed=False), FLAGFLIP, now=NOW)
    assert info["state"] == "arming"


def test_the_occ_engine_has_no_vegas_gate():
    """OCC crosses ARE the whole signal — reporting 'waiting for Vegas' there
    would be a lie about a gate that does not exist on that engine."""
    st = live_xaut(consumed=False, last_vegas=None, engine="occ")
    info = ST.explain("SOL", st, OCC, now=NOW)
    assert info["state"] == "arming"
    assert "交叉" in ST.card(ST.explain("SOL", live_xaut(engine="occ"), OCC, now=NOW))


def test_a_blocked_engine_says_so_before_talking_about_flags():
    info = ST.explain("XAUT", live_xaut(consumed=False), FLAGFLIP,
                      blocked="STRATEGY3_LIVE=false (alert-only)", now=NOW)
    assert info["state"] == "blocked"
    assert "alert-only" in info["headline"]


def test_no_scan_yet_is_distinguished_from_no_signal():
    assert ST.explain("XAUT", {}, FLAGFLIP, now=NOW)["state"] == "no_data"
    assert ST.explain("XAUT", {"last_seen": NOW}, FLAGFLIP,
                      now=NOW)["state"] == "no_signal_yet"


def test_a_missing_flag_timestamp_never_invents_one():
    """Pre-upgrade state files have no flag_ts. Saying '0 分鐘前' would be a
    fabricated zero — the same trap as nz() on a missing reading."""
    info = ST.explain("XAUT", live_xaut(flag_ts=None), FLAGFLIP, now=NOW)
    assert info["flag_ago"] == "時間未記錄"


def test_every_state_renders_without_crashing():
    seen = set()
    for st in (({}, FLAGFLIP), ({"last_seen": NOW}, FLAGFLIP),
               (live_xaut(), FLAGFLIP), (live_xaut(pos_dir="short"), FLAGFLIP),
               (live_xaut(consumed=False), FLAGFLIP),
               (live_xaut(consumed=False, last_vegas=-1), FLAGFLIP)):
        info = ST.explain("XAUT", st[0], st[1], now=NOW)
        seen.add(info["state"])
        assert ST.card(info)
    info = ST.explain("XAUT", live_xaut(consumed=False), FLAGFLIP,
                      blocked="halted", now=NOW)
    seen.add(info["state"])
    assert seen == set(ST.STATES)          # every branch reachable and rendered


# ── the card is going to a PUBLIC topic ──────────────────────────────────────

def test_the_card_carries_no_account_figures():
    """channel="s1signals" is a public group topic. Size, leverage and margin
    are the owner's business — see test_protect_suite's group-channel guard."""
    text = ST.card(ST.explain("XAUT", live_xaut(pos_dir="long"), FLAGFLIP, now=NOW))
    for leak in ("USDT", "50x", "保證金", "餘額", "淨值", "30.0"):
        assert leak not in text


# ── the scanner side: reasons are recorded, and only changes are announced ───

def test_open_flip_outcomes_still_compare_as_plain_strings():
    """Outcome subclasses str on purpose so every existing `== "skip"` keeps
    working — a tuple return would have broken three call sites silently."""
    o = S3.Outcome("skip", "because")
    assert o == "skip" and o.reason == "because"
    assert S3.Outcome("opened").reason == ""


def test_a_skip_records_why_and_a_fresh_flag_clears_it(monkeypatch):
    monkeypatch.setattr(S3.config, "STRATEGY3_LIVE", False, raising=False)
    out = S3.open_flip("XAUT/USDT:USDT", "long", 4047.8, 88, 30.0, 50)
    assert out == "skip" and "alert-only" in out.reason


def test_the_card_is_announced_only_when_the_answer_changes(monkeypatch):
    sent = []
    monkeypatch.setattr(S3, "_tg_feed", lambda m: sent.append(m))
    monkeypatch.setattr(S3, "live_blocked", lambda: "")
    st = live_xaut()

    assert S3.announce_state("XAUT", st, FLAGFLIP) is False   # first: seed, silent
    assert sent == []
    assert S3.announce_state("XAUT", st, FLAGFLIP) is False   # unchanged: quiet
    assert sent == []

    st["pos_dir"] = "long"                                    # changed: one card
    assert S3.announce_state("XAUT", st, FLAGFLIP) is True
    assert len(sent) == 1 and "S3 · XAUT" in sent[0]
    assert S3.announce_state("XAUT", st, FLAGFLIP) is False
    assert len(sent) == 1


def test_a_new_skip_reason_counts_as_a_change(monkeypatch):
    """Same state ('spent'), different reason — the owner needs to hear it."""
    sent = []
    monkeypatch.setattr(S3, "_tg_feed", lambda m: sent.append(m))
    monkeypatch.setattr(S3, "live_blocked", lambda: "")
    st = live_xaut(skip_reason="A")
    S3.announce_state("XAUT", st, FLAGFLIP)
    st["skip_reason"] = "B"
    assert S3.announce_state("XAUT", st, FLAGFLIP) is True


def test_a_broken_status_card_never_stops_the_trading_loop(monkeypatch):
    monkeypatch.setattr(ST, "explain",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert S3.announce_state("XAUT", live_xaut(), FLAGFLIP) is False


# ── the lifecycle now lands in the S1 topic, ops noise does not ──────────────

def test_lifecycle_goes_to_the_s1_topic_and_errors_stay_on_alerts():
    import re
    src = open(S3.__file__, encoding="utf-8").read()
    feed = re.findall(r"_tg_feed\(f?\"([^\"]{0,40})", src)
    ops = re.findall(r"(?<!_)_tg\(f?\"([^\"]{0,40})", src)
    assert any("FLIP" in m for m in feed) and any("EXIT" in m for m in feed)
    assert any("FLAG" in m for m in feed)
    # the naked-position alarm must NOT be diluted into the public feed
    assert any("GUARDIAN" in m for m in ops)
    assert not any("GUARDIAN" in m for m in feed)


def test_the_s1_topic_send_is_the_only_channel_override():
    src = open(S3.__file__, encoding="utf-8").read()
    assert 'channel="s1signals"' in src
    assert src.count("telegram_utils.send_message(") == 2   # _tg + _tg_feed


if __name__ == "__main__":       # pragma: no cover
    pytest.main([__file__, "-q"])
