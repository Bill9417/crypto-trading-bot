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


# ── partial closes are ONE position, not N losses ────────────────────────────
# 2026-08-03: a single 0.371 XAUT short (entry 4041.8, −3.68 USDT all-in) was
# closed in four chunks. Bybit emits one closed-P&L row per closing FILL, not
# per position, so it arrived as four rows and tripped a limit of 2 — halting
# the engine on what was one small losing trade. The −45 USDT net limit, the
# one that measures real damage, was nowhere near.
def _chunk(pnl, hours_ago, entry="4041.8", side="Buy", symbol="XAUTUSDT"):
    return {"pnl": pnl, "time": NOW_MS - int(hours_ago * 3600 * 1000),
            "symbol": symbol, "entry": entry, "side": side}


def test_chunk_closed_trade_counts_once():
    rows = [_chunk(-0.97, 1), _chunk(-1.04, 2), _chunk(-0.74, 12), _chunk(-0.92, 15)]
    n, net = R.losses_in_window(rows, NOW_MS)
    assert n == 1                                    # one position, not four
    assert abs(net - (-3.67)) < 1e-9                 # net is still every row


def test_distinct_positions_still_count_separately():
    rows = [_chunk(-5.0, 1, entry="4041.8"), _chunk(-6.0, 2, entry="4090.2")]
    assert R.losses_in_window(rows, NOW_MS)[0] == 2


def test_opposite_sides_at_one_price_are_two_positions():
    """A short and a long can share an average entry; they are not one trade."""
    rows = [_chunk(-5.0, 1, side="Buy"), _chunk(-6.0, 2, side="Sell")]
    assert R.losses_in_window(rows, NOW_MS)[0] == 2


def test_scaling_out_of_a_winner_through_a_red_chunk_is_not_a_loss():
    rows = [_chunk(+9.0, 1), _chunk(-1.0, 2)]
    n, net = R.losses_in_window(rows, NOW_MS)
    assert n == 0 and abs(net - 8.0) < 1e-9


def test_rows_without_an_entry_keep_one_row_per_trade():
    """The documented pure-math shape carries no entry price — unchanged."""
    assert R.losses_in_window([_trade(-1.0, 1), _trade(-1.0, 2)], NOW_MS)[0] == 2


def test_chunks_outside_the_window_do_not_join_the_group():
    rows = [_chunk(-1.0, 1), _chunk(-50.0, 25)]
    n, net = R.losses_in_window(rows, NOW_MS)
    assert n == 1 and abs(net - (-1.0)) < 1e-9


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


# ── nothing that reads the real account may reach a group channel ────────────
# The Telegram group is joinable by anyone holding the invite link, so "which
# channel" is an access-control decision, not a formatting one. 2026-08-03:
# daily_risk broadcast the account's realised daily P&L to the public topic
# because send_message() defaults to channel="alerts".
GROUP_CHANNELS = {"alerts", "events", "liq", "report", "s1signals", "signals",
                  "tech", "twstocks"}


def test_group_channels_never_carry_account_numbers():
    import os
    import re
    money = re.compile(r"(帳戶今日|已實現 \{|未實現 \{|餘額 \{|淨值 \{|可用保證金（\{"
                       r"|avail:\.|balance:\.|equity:\.)")
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for name in sorted(os.listdir(app_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(app_dir, name), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        for i, line in enumerate(lines):
            if "send_message(" not in line or line.strip().startswith("def "):
                continue
            block = "\n".join(lines[i:i + 8])
            ch = re.search(r'channel=["\'](\w+)["\']', block)
            # no channel= at all means the "alerts" default, i.e. the group
            in_group = (ch.group(1) in GROUP_CHANNELS) if ch else True
            if in_group and money.search(block):
                offenders.append(f"{name}:{i + 1}")
    assert not offenders, f"account figures routed to a group channel: {offenders}"


# ── watchdog: no false alarms, and not in front of the members ───────────────
def test_the_tunnel_check_is_opt_in(monkeypatch, tmp_path):
    """The public URL moved to a Tailscale Funnel, which is a system service,
    not a cloudflared process — so this check reported the retired quick-tunnel
    "down" and alerted every hour."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    assert W.tick("strategy2_scanner.py") == []          # no cloudflared alarm


def test_the_tunnel_check_still_works_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", True)
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message", lambda *a, **k: True)
    assert W.TUNNEL_KEY in W.tick("strategy2_scanner.py")


def test_watchdog_alerts_go_to_the_owner_not_the_group(monkeypatch, tmp_path):
    """'restart with ./run_all.sh bg' is an instruction only the owner can act
    on, and send_message() defaults to a PUBLIC group topic."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "_ps", lambda: "python -u strategy2_scanner.py")
    import telegram_utils
    seen = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: seen.append(k.get("channel")) or True)
    W.tick("strategy2_scanner.py")
    assert seen and all(ch == "private" for ch in seen), seen


def test_disabling_a_check_is_not_announced_as_recovery(monkeypatch, tmp_path):
    """2026-08-03: turning the retired cloudflared check off dropped its name
    from `down`, which is indistinguishable from the tunnel coming back — so
    the owner got "✅ cloudflared 已恢復運行" for something that no longer
    exists. Unwatched names must leave the state silently."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    W._save_state({"down": [W.TUNNEL_KEY], "last_alert": {W.TUNNEL_KEY: 1.0}})
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    import telegram_utils
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.append(msg) or True)
    assert W.tick("strategy2_scanner.py") == []
    assert not sent, f"announced a retired check: {sent}"
    st = W._load_state()
    assert W.TUNNEL_KEY not in (st.get("down") or [])
    assert W.TUNNEL_KEY not in (st.get("last_alert") or {})


def test_a_watched_process_still_announces_recovery(monkeypatch, tmp_path):
    """The silencing above must not swallow a real comeback."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    dead = sorted(W.EXPECTED)[0]
    W._save_state({"down": [dead], "last_alert": {}})
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    import telegram_utils
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.append((msg, k.get("channel"))) or True)
    W.tick("strategy2_scanner.py")
    assert sent and "已恢復運行" in sent[0][0] and sent[0][1] == "private"


# ── the public URL: checked from outside, or not at all ─────────────────────
# 2026-08-05: the Tailscale Funnel was registered, the ACL granted funnel, the
# cert was valid and `tailscale funnel status` said "Funnel on" — and the public
# relays served nothing. LINE could not reach its webhook, so every 指令 did
# nothing and the Quick Reply buttons never returned; /tw, /us and /welcome were
# dead for everyone off the tailnet. The process watchdog saw four healthy
# processes and said nothing, because a reachable site is not a process.
def _pub(monkeypatch, tmp_path, codes):
    """codes: HTTP code per relay IP, in order. Keyed BY IP rather than by call
    order, because a failing relay is retried — an ordered queue would hand the
    retry the next relay's code and turn a dead relay into a live one."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "WATCH_PUBLIC", True)
    monkeypatch.setattr(W, "public_host", lambda: "host.test")
    ips = ["1.1.1.1", "2.2.2.2"][:len(codes)]
    monkeypatch.setattr(W, "public_ips", lambda h: ips)
    by_ip = dict(zip(ips, codes, strict=False))

    class _R:
        def __init__(self, out):
            self.stdout = out

    def _run(argv, *a, **k):
        resolve = next((x for x in argv if x.count(":") == 2), "")
        return _R(by_ip.get(resolve.rsplit(":", 1)[-1], "000"))

    monkeypatch.setattr(W.subprocess, "run", _run)


def test_a_dead_public_url_is_an_alert(monkeypatch, tmp_path):
    _pub(monkeypatch, tmp_path, ["000", "000"])
    assert W.public_reachable() is False


def test_one_dead_relay_out_of_two_is_an_outage(monkeypatch, tmp_path):
    """This assertion used to be the opposite, on the reasoning that Tailscale
    publishes several relays so one dead is a spare tyre. It isn't. Public DNS
    hands the browser EVERY A record and it picks whichever it likes, so a dead
    ingress fails roughly half of real visits — which is exactly what happened
    on 2026-08-05: a phone off the tailnet got "cannot establish a secure
    connection" intermittently while this check reported healthy."""
    _pub(monkeypatch, tmp_path, ["000", "200"])
    assert W.public_reachable() is False
    st = W.public_status()
    assert st["bad"] == ["1.1.1.1"] and st["ok"] == ["2.2.2.2"]


def test_a_relay_is_retried_before_being_called_dead(monkeypatch, tmp_path):
    """One timeout is a blip. Alerting on the first miss would page the owner
    every time a single curl lost a race."""
    _pub(monkeypatch, tmp_path, ["200", "200"])
    calls = []
    real = W.subprocess.run
    monkeypatch.setattr(W.subprocess, "run",
                        lambda argv, *a, **k: (calls.append(1), real(argv, *a, **k))[1])
    assert W.public_reachable() is True
    assert len(calls) == 2, "a healthy relay must not be probed twice"


def test_no_public_url_configured_is_not_an_outage(monkeypatch):
    monkeypatch.setattr(W, "public_host", lambda: "")
    assert W.public_reachable() is True


def test_dns_failure_is_not_an_outage(monkeypatch):
    """A watchdog that alerts because it could not MEASURE is worse than none."""
    monkeypatch.setattr(W, "public_host", lambda: "host.test")
    monkeypatch.setattr(W, "public_ips", lambda h: [])
    assert W.public_reachable() is True


def test_the_public_alert_carries_the_right_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "WATCH_PUBLIC", True)
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    monkeypatch.setattr(W, "public_host", lambda: "host.test")
    monkeypatch.setattr(W, "public_ips", lambda h: ["1.1.1.1", "2.2.2.2"])
    monkeypatch.setattr(W, "public_status",
                        lambda h=None, i=None: {"ok": [], "bad": ["1.1.1.1", "2.2.2.2"]})
    import telegram_utils
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.append((msg, k.get("channel"))) or True)
    alerted = W.tick("strategy2_scanner.py")
    assert W.PUBLIC_KEY in alerted
    assert "tailscale.sh" in sent[0][0]          # not ./run_all.sh bg
    assert sent[0][1] == "private"


def test_a_half_dead_public_url_is_not_described_as_stopped(monkeypatch, tmp_path):
    """The owner opens the link, the working relay serves them, and they
    conclude the alert was noise. The message has to say WHY it looks fine."""
    partial = W.public_alert_text({"ok": ["2.2.2.2"], "bad": ["1.1.1.1"]}, "./tailscale.sh rearm")
    assert "已停止運行" not in partial and "完全連不上" not in partial
    assert "50%" in partial and "1.1.1.1" in partial
    assert "./tailscale.sh rearm" in partial

    total = W.public_alert_text({"ok": [], "bad": ["1.1.1.1", "2.2.2.2"]}, "./tailscale.sh rearm")
    assert "完全連不上" in total


def test_a_measurable_outage_still_beats_an_unmeasurable_one(monkeypatch, tmp_path):
    """No DNS answer means we could not measure — that must stay quiet."""
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "WATCH_PUBLIC", True)
    monkeypatch.setattr(W, "WATCH_STALE", False)
    monkeypatch.setattr(W, "_ps", lambda: "\n".join(
        f"python -u {s}" for s in W.EXPECTED))
    monkeypatch.setattr(W, "public_host", lambda: "host.test")
    monkeypatch.setattr(W, "public_ips", lambda h: [])
    monkeypatch.setattr(W, "public_status", lambda h=None, i=None: (_ for _ in ()).throw(
        AssertionError("must not probe when DNS gave nothing to probe")))
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message", lambda msg, **k: True)
    assert W.PUBLIC_KEY not in W.tick("strategy2_scanner.py")
