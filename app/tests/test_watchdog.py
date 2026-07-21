"""watchdog — process parsing + in-run log rotation (copy-truncate)."""

import watchdog

PS_ALL_UP = ("/usr/bin/something else\n"
            "/Users/x/miniforge3/bin/python -u app.py\n"
            "/Users/x/miniforge3/bin/python -u bot.py\n"
            "/Users/x/miniforge3/bin/python -u strategy2_scanner.py\n"
            "/Users/x/miniforge3/bin/python -u strategy3_scanner.py\n"
            "/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000\n")


def test_parse_running_and_missing():
    ps = ("/usr/bin/something else\n"
          "/Users/x/miniforge3/bin/python -u app.py\n"
          "/Users/x/miniforge3/bin/python -u strategy2_scanner.py\n")
    running = watchdog.parse_running(ps)
    assert running == {"app.py", "strategy2_scanner.py"}
    assert watchdog.missing(running) == ["bot.py", "strategy3_scanner.py"]


def test_tunnel_running_true_and_false():
    assert watchdog.tunnel_running(PS_ALL_UP) is True
    assert watchdog.tunnel_running("/usr/bin/something else\n") is False


def test_rotate_logs_copy_truncates_only_oversized(tmp_path):
    big = tmp_path / "app.log"
    small = tmp_path / "bot.log"
    other = tmp_path / "notes.txt"
    big.write_text("x" * 500)
    small.write_text("y" * 10)
    other.write_text("z" * 500)

    rotated = watchdog.rotate_logs(str(tmp_path), max_bytes=100)

    assert rotated == ["app.log"]
    assert (tmp_path / "app.log.1").read_text() == "x" * 500   # history kept
    assert big.read_text() == ""                               # truncated in place
    assert small.read_text() == "y" * 10                       # untouched
    assert not (tmp_path / "notes.txt.1").exists()             # only *.log


def test_rotate_logs_missing_dir_is_noop(tmp_path):
    assert watchdog.rotate_logs(str(tmp_path / "nope")) == []


# ── tick(): process + tunnel alerting ─────────────────────────────────────────
def _wire_tick(monkeypatch, ps_text, tmp_path):
    import telegram_utils
    monkeypatch.setattr(watchdog, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(watchdog, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(watchdog, "_ps", lambda: ps_text)
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **kw: sent.append(msg) or True)
    return sent


def test_tick_alerts_on_dead_tunnel_with_correct_fix_command(monkeypatch, tmp_path):
    ps_no_tunnel = PS_ALL_UP.replace(
        "/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000\n", "")
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path)
    alerted = watchdog.tick("app.py")
    assert "cloudflared" in alerted
    assert sent and "Cloudflare" in sent[0] and "./tunnel.sh start" in sent[0]
    assert "./run_all.sh bg" not in sent[0]   # tunnel gets its OWN fix, not the stack restart


def test_tick_recovers_when_tunnel_comes_back(monkeypatch, tmp_path):
    ps_no_tunnel = PS_ALL_UP.replace(
        "/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000\n", "")
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path)
    watchdog.tick("app.py")
    assert len(sent) == 1

    # force past the 5-min gate so the next call actually checks again
    state = watchdog._load_state()
    state["last_check"] = 0
    watchdog._save_state(state)
    monkeypatch.setattr(watchdog, "_ps", lambda: PS_ALL_UP)
    watchdog.tick("app.py")
    assert len(sent) == 2 and "已恢復運行" in sent[1] and "Cloudflare" in sent[1]


def test_tick_silent_when_everything_up(monkeypatch, tmp_path):
    sent = _wire_tick(monkeypatch, PS_ALL_UP, tmp_path)
    assert watchdog.tick("app.py") == []
    assert not sent


def test_tick_respects_check_interval_gate(monkeypatch, tmp_path):
    ps_no_tunnel = PS_ALL_UP.replace(
        "/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000\n", "")
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path)
    watchdog.tick("app.py")
    assert len(sent) == 1
    watchdog.tick("app.py")               # same 5-min window → no re-check at all
    assert len(sent) == 1
