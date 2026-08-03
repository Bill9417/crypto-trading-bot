"""Account-wide daily loss limit.

S3's own breaker counts only STRATEGY3_SYMBOLS, so it cannot see a bad day
made by the S1 mirror or the copy engine — all three settle into the same Bybit
sub-account. This is the shared brake.

Two properties matter more than the arithmetic:
  · it gates ENTRIES only — it must never close or cancel anything;
  · it FAILS OPEN — it sits in front of every engine, so an exchange blip must
    not silently halt all trading.
"""
from datetime import datetime, timedelta

import daily_risk as R

NOW = datetime.now(R.TZ)


def _t(pnl, when=None):
    when = when or NOW
    return {"pnl": pnl, "time": int(when.timestamp() * 1000)}


def _armed(monkeypatch, limit=10.0):
    monkeypatch.setattr(R, "MAX_DAILY_LOSS_USDT", limit)
    monkeypatch.setattr(R, "_cache", {"ts": 0.0, "pnl": None, "day": ""})


# ── the window ───────────────────────────────────────────────────────────────
def test_only_todays_trades_count():
    yesterday = NOW - timedelta(days=1)
    assert R.realised_today([_t(-5.0), _t(-99.0, yesterday)]) == -5.0


def test_realised_today_sums_wins_and_losses():
    assert R.realised_today([_t(3.0), _t(-5.0), _t(1.0)]) == -1.0


def test_bad_rows_are_skipped_not_fatal():
    assert R.realised_today([{"pnl": "x", "time": int(NOW.timestamp() * 1000)},
                             {"time": None}, _t(-2.0)]) == -2.0


def test_the_day_boundary_is_taipei_midnight():
    start_ms, end_ms = R.day_bounds_ms(NOW)
    start = datetime.fromtimestamp(start_ms / 1000, R.TZ)
    assert (start.hour, start.minute) == (0, 0)
    assert start.date() == NOW.date()
    assert end_ms >= start_ms


# ── the threshold ────────────────────────────────────────────────────────────
def test_breach_needs_the_limit_to_be_set():
    assert R.breached(-50.0, limit=0) is False        # 0 disables entirely
    assert R.breached(-50.0, limit=10.0) is True
    assert R.breached(-9.99, limit=10.0) is False
    assert R.breached(-10.0, limit=10.0) is True      # exactly at the limit
    assert R.breached(5.0, limit=10.0) is False


def test_a_negative_limit_is_read_as_a_magnitude():
    assert R.breached(-11.0, limit=-10.0) is True


# ── the gate ─────────────────────────────────────────────────────────────────
def test_disabled_by_default():
    assert R.enabled() is False
    assert R.entry_blocked() == ""


def test_blocks_entries_once_breached(monkeypatch):
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "_todays_pnl", lambda: -12.0)
    monkeypatch.setattr(R, "_announce_once", lambda day, pnl: None)
    msg = R.entry_blocked()
    assert msg and "不再開新倉" in msg


def test_allows_entries_inside_the_limit(monkeypatch):
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "_todays_pnl", lambda: -3.0)
    assert R.entry_blocked() == ""


def test_the_gate_fails_OPEN_when_the_exchange_is_down(monkeypatch):
    """It sits in front of three engines — an API blip must not halt trading."""
    _armed(monkeypatch, 10.0)
    import strategy3_exec as X

    def _boom(**k):
        raise RuntimeError("bybit down")
    monkeypatch.setattr(X, "closed_pnl_summary", _boom)
    assert R._todays_pnl() == 0.0
    assert R.entry_blocked() == ""


def test_announces_once_per_day_not_once_per_entry(monkeypatch, tmp_path):
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "d.json"))
    monkeypatch.setattr(R, "_todays_pnl", lambda: -12.0)
    sent = []
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda m, **k: sent.append(m) or True)
    for _ in range(5):
        assert R.entry_blocked() != ""
    assert len(sent) == 1


def test_a_new_day_announces_again(monkeypatch, tmp_path):
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "STATE_FILE", str(tmp_path / "d.json"))
    R._save({"announced_day": "1999-01-01"})
    monkeypatch.setattr(R, "_todays_pnl", lambda: -12.0)
    sent = []
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda m, **k: sent.append(m) or True)
    R.entry_blocked()
    assert len(sent) == 1
    assert R._load()["announced_day"] == R.today_str()


def test_pnl_read_is_cached(monkeypatch):
    _armed(monkeypatch, 10.0)
    calls = []
    import strategy3_exec as X
    monkeypatch.setattr(X, "closed_pnl_summary",
                        lambda **k: calls.append(1) or {"ok": True, "trades": []})
    R._todays_pnl()
    R._todays_pnl()
    assert len(calls) == 1


def test_status_is_safe_for_a_health_page(monkeypatch):
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "_todays_pnl", lambda: -4.0)
    st = R.status()
    assert st["enabled"] is True and st["limit"] == 10.0
    assert st["pnl_today"] == -4.0 and st["blocked"] is False


# ── engine wiring ────────────────────────────────────────────────────────────
def test_the_mirror_refuses_to_open_while_blocked(monkeypatch, tmp_path):
    import config
    import s1_bybit_mirror as M
    import strategy3_exec as X
    monkeypatch.setattr(M, "STATE_FILE", str(tmp_path / "pos.json"))
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "is_live", lambda: False)
    notes, owner_notes = [], []
    monkeypatch.setattr(M, "_tg", lambda msg: notes.append(msg))
    monkeypatch.setattr(M, "_tg_owner", lambda msg: owner_notes.append(msg))
    _armed(monkeypatch, 10.0)
    monkeypatch.setattr(R, "_todays_pnl", lambda: -12.0)
    monkeypatch.setattr(R, "_announce_once", lambda day, pnl: None)

    assert M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0) is False
    assert M._load() == {}                       # nothing tracked
    # The public topic gets the REASON; the numbers go to the owner's DM.
    # daily_risk's block text embeds the account's realised P&L in USDT, and
    # the S1 topic is in a group anyone with the invite link can join.
    assert any("今日風控上限已觸發" in n for n in notes)
    assert not any("USDT" in n for n in notes)
    assert any("不再開新倉" in n for n in owner_notes)


def test_the_daily_loss_notice_goes_to_the_owner_not_the_group(monkeypatch):
    """send_message() defaults to channel='alerts', which routes to the PUBLIC
    group topic — so this announcement broadcast the account's realised daily
    P&L to every member on the worst day of the month."""
    import daily_risk as D
    import telegram_utils
    sent = {}
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.update(msg=msg, ch=k.get("channel")) or True)
    monkeypatch.setattr(D, "_load", lambda: {})
    monkeypatch.setattr(D, "_save", lambda st: None)
    D._announce_once("2026-08-03", -12.34)
    assert sent["ch"] == "private", "daily P&L must never reach a group channel"
    assert "-12.34" in sent["msg"]
