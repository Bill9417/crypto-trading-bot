"""watchdog + S3 circuit breaker + state backup — the protection batch."""
import os
import time
import zipfile

import backup_state
import strategy3_risk as R
import watchdog as W

# ── watchdog ─────────────────────────────────────────────────────────────────
PS = """COMMAND
/sbin/launchd
/Users/x/miniforge3/bin/python -u app.py
/Users/x/miniforge3/bin/python -u strategy2_scanner.py
/Users/x/miniforge3/bin/python -u bot.py
grep something strategy3_scanner.py
/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000
"""


def test_parse_running_matches_only_python_processes():
    running = W.parse_running(PS)
    assert running == {"app.py", "strategy2_scanner.py", "bot.py"}
    # the grep line mentions strategy3_scanner.py but is NOT a python process


def test_missing_lists_dead_processes():
    assert W.missing({"app.py", "bot.py"}) == \
        ["strategy2_scanner.py", "strategy3_scanner.py"]
    assert W.missing(set(W.EXPECTED)) == []


def test_watchdog_alerts_and_recovers(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "_ps", lambda: PS)          # strategy3 missing
    sent = []
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **kw: sent.append(msg) or True)
    alerted = W.tick("strategy2_scanner.py")
    assert alerted == ["strategy3_scanner.py"]
    assert "🚨" in sent[0] and "strategy3_scanner.py" in sent[0]
    # process comes back → recovery note, no repeat alarm
    full_ps = PS + "/Users/x/miniforge3/bin/python -u strategy3_scanner.py\n"
    monkeypatch.setattr(W, "_ps", lambda: full_ps)
    state = W._load_state()
    state["last_check"] = 0                            # bypass the check gate
    W._save_state(state)
    assert W.tick("strategy2_scanner.py") == []
    assert any("✅" in m for m in sent)


# ── circuit breaker ──────────────────────────────────────────────────────────
NOW_MS = 1_783_900_000_000


def _trade(pnl, hours_ago, symbol="XAUTUSDT", now_ms=NOW_MS):
    return {"pnl": pnl, "time": now_ms - int(hours_ago * 3600 * 1000),
            "symbol": symbol}


def test_losses_in_window_counts_and_nets():
    trades = [_trade(-15.0, 2), _trade(+8.0, 5), _trade(-20.0, 23),
              _trade(-99.0, 30)]                       # 30h ago — outside
    n, net = R.losses_in_window(trades, NOW_MS)
    assert n == 2
    assert abs(net - (-27.0)) < 1e-9


def test_breach_on_stop_count_and_loss_depth():
    assert "虧損平倉" in R.breach(2, -10.0, max_stops=2, max_loss=45)
    assert "淨虧損" in R.breach(1, -50.0, max_stops=2, max_loss=45)
    assert R.breach(1, -10.0, max_stops=2, max_loss=45) == ""
    assert R.breach(99, -999.0, max_stops=0, max_loss=0) == ""   # disabled


def test_losses_ignore_symbols_s3_does_not_trade():
    """Regression 2026-07-14: two tiny MANUAL ETH losses (−1.8 USDT total, on
    a +24 USDT day) halted the gold engine — the breaker must judge S3's own
    symbols only. Manual trades on the SAME symbol still count (no author on
    Bybit's closed-P&L rows)."""
    trades = [_trade(-0.71, 1, symbol="ETHUSDT"),
              _trade(-1.07, 2, symbol="ETHUSDT"),
              _trade(+2.41, 3, symbol="SKHYNIXUSDT"),
              _trade(-22.0, 4, symbol="XAUTUSDT")]
    n, net = R.losses_in_window(trades, NOW_MS, bases={"XAUT"})
    assert n == 1                       # only the XAUT stop-out counts
    assert abs(net - (-22.0)) < 1e-9
    n_all, _ = R.losses_in_window(trades, NOW_MS)     # bases=None → everything
    assert n_all == 3


def test_halt_file_cycle(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "HALT_FILE", str(tmp_path / "halt.json"))
    assert R.halted() is None
    R.set_halt("test reason")
    assert R.halted()["reason"] == "test reason"
    assert R.clear_halt() is True
    assert R.halted() is None
    assert R.clear_halt() is False                     # idempotent


def test_entry_blocked_trips_and_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "HALT_FILE", str(tmp_path / "halt.json"))
    monkeypatch.setattr(R, "MAX_DAILY_STOPS", 2)
    monkeypatch.setattr(R, "MAX_DAILY_LOSS_USDT", 45.0)
    monkeypatch.setattr(R, "S3_BASES", {"XAUT"})
    # entry_blocked() windows against the REAL clock — the fixture trades must
    # be stamped relative to it, not the fixed NOW_MS (a NOW_MS-stamped trade
    # aged out of the 24h window a day after this test was written and the
    # suite went red overnight).
    real_now = int(time.time() * 1000)
    import strategy3_exec
    monkeypatch.setattr(strategy3_exec, "closed_pnl_history",
                        lambda limit=30: {"ok": True, "trades":
                                          [_trade(-30.0, 1, now_ms=real_now),
                                           _trade(-31.0, 3, now_ms=real_now)]})
    sent = []
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **kw: sent.append(msg) or True)
    reason = R.entry_blocked()
    assert reason and "虧損" in reason
    assert sent and "🛑" in sent[0]
    # halt persists without re-querying the API
    monkeypatch.setattr(strategy3_exec, "closed_pnl_history",
                        lambda limit=30: (_ for _ in ()).throw(RuntimeError("no api")))
    assert R.entry_blocked() == reason


def test_entry_blocked_ignores_manual_trades_on_other_symbols(monkeypatch, tmp_path):
    """The 2026-07-14 live false-trip, end to end: manual ETH losses on the
    same sub-account must NOT halt the gold engine."""
    monkeypatch.setattr(R, "HALT_FILE", str(tmp_path / "halt.json"))
    monkeypatch.setattr(R, "S3_BASES", {"XAUT"})
    real_now = int(time.time() * 1000)
    import strategy3_exec
    monkeypatch.setattr(strategy3_exec, "closed_pnl_history",
                        lambda limit=30: {"ok": True, "trades":
                                          [_trade(-0.71, 1, symbol="ETHUSDT", now_ms=real_now),
                                           _trade(-1.07, 2, symbol="ETHUSDT", now_ms=real_now)]})
    assert R.entry_blocked() == ""
    assert R.halted() is None                          # and no halt file written


def test_entry_blocked_fails_open_on_api_error(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "HALT_FILE", str(tmp_path / "halt.json"))
    import strategy3_exec
    monkeypatch.setattr(strategy3_exec, "closed_pnl_history",
                        lambda limit=30: {"ok": False, "error": "boom"})
    assert R.entry_blocked() == ""


# ── backup ───────────────────────────────────────────────────────────────────
def test_backup_writes_zip_and_prunes(tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"state{i}.json"
        p.write_text("{}")
        files.append(str(p))
    dest = str(tmp_path / "backups")
    for day in range(1, 17):                            # 16 days → prune to 14
        path = backup_state.make_backup(dest, files, f"2026-07-{day:02d}")
        assert os.path.exists(path)
    zips = sorted(os.listdir(dest))
    assert len(zips) == backup_state.KEEP
    assert zips[0] == "state-2026-07-03.zip"            # oldest two pruned
    with zipfile.ZipFile(os.path.join(dest, zips[-1])) as z:
        assert sorted(z.namelist()) == ["state0.json", "state1.json", "state2.json"]


def test_offsite_copy_mirrors_and_prunes(tmp_path):
    src = tmp_path / "local"
    f = tmp_path / "a.json"
    f.write_text("{}")
    offsite = tmp_path / "cloud" / "WolfScannerBackups"
    (tmp_path / "cloud").mkdir()                        # the "iCloud" parent
    for day in range(1, 17):
        z = backup_state.make_backup(str(src), [str(f)], f"2026-07-{day:02d}")
        assert backup_state.offsite_copy(z, str(offsite)) is True
    zips = sorted(os.listdir(offsite))
    assert len(zips) == backup_state.KEEP               # pruned offsite too
    assert zips[-1] == "state-2026-07-16.zip"


def test_offsite_copy_refuses_missing_parent(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{}")
    z = backup_state.make_backup(str(tmp_path / "l"), [str(f)], "2026-07-13")
    ghost = str(tmp_path / "no-icloud-here" / "sub")    # parent doesn't exist
    assert backup_state.offsite_copy(z, ghost) is False
    assert not os.path.exists(ghost)                    # never fabricated


def test_backup_is_idempotent_per_day(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{}")
    dest = str(tmp_path / "b")
    p1 = backup_state.make_backup(dest, [str(f)], "2026-07-13")
    mtime = os.path.getmtime(p1)
    p2 = backup_state.make_backup(dest, [str(f)], "2026-07-13")
    assert p1 == p2 and os.path.getmtime(p2) == mtime   # untouched
