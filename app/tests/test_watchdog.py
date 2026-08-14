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
def _wire_tick(monkeypatch, ps_text, tmp_path, watch_tunnel=False):
    """watch_tunnel: the cloudflared check is opt-in since the public URL moved
    to a Tailscale Funnel — a system service, not a process this can see — so
    it reported the retired quick-tunnel down and alerted every hour. The
    default-off behaviour is covered in test_protect_suite."""
    monkeypatch.setattr(watchdog, "WATCH_TUNNEL", watch_tunnel)
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
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path, watch_tunnel=True)
    alerted = watchdog.tick("app.py")
    assert "cloudflared" in alerted
    assert sent and "Cloudflare" in sent[0] and "./tunnel.sh start" in sent[0]
    assert "./run_all.sh bg" not in sent[0]   # tunnel gets its OWN fix, not the stack restart


def test_tick_recovers_when_tunnel_comes_back(monkeypatch, tmp_path):
    ps_no_tunnel = PS_ALL_UP.replace(
        "/opt/homebrew/bin/cloudflared tunnel --url http://localhost:4000\n", "")
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path, watch_tunnel=True)
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
    sent = _wire_tick(monkeypatch, ps_no_tunnel, tmp_path, watch_tunnel=True)
    watchdog.tick("app.py")
    assert len(sent) == 1
    watchdog.tick("app.py")               # same 5-min window → no re-check at all
    assert len(sent) == 1


# ── confirm before acting (2026-08-13) ──────────────────────────────────────
# The public-URL check used to alert AND re-arm on a single failed probe. The
# re-arm is not free: it runs `serve reset` and takes the site down for 30-60s
# while relays re-register. So one flaky probe caused a repair that caused a
# real outage that the owner got a 🚨 about — the watchdog manufacturing a
# share of the events it reported.
def _pub_tick(monkeypatch, tmp_path, results):
    """Drive watchdog.tick() through a scripted sequence of probe results."""
    import watchdog as W
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "wd.json"))
    monkeypatch.setattr(W, "CHECK_SEC", 0)
    monkeypatch.setattr(W, "WATCH_TUNNEL", False)
    monkeypatch.setattr(W, "WATCH_PUBLIC", True)
    monkeypatch.setattr(W, "public_host", lambda: "x.ts.net")
    monkeypatch.setattr(W, "public_ips", lambda h: ["1.1.1.1", "2.2.2.2"])
    monkeypatch.setattr(W, "_ps", lambda: "")
    monkeypatch.setattr(W, "parse_running", lambda t: set(W.EXPECTED))
    monkeypatch.setattr(W, "rotate_logs", lambda: None)

    fixes, sent = [], []
    monkeypatch.setattr(W, "run_autofix", lambda: (fixes.append(1), True)[1])
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda *a, **k: sent.append(a[0] if a else ""))

    seq = list(results)

    def status(host=None, ips=None):
        good = seq.pop(0) if seq else True
        return {"ok": ["1.1.1.1", "2.2.2.2"], "bad": []} if good \
            else {"ok": [], "bad": ["1.1.1.1", "2.2.2.2"]}

    monkeypatch.setattr(W, "public_status", status)
    for _ in range(len(results)):
        W.tick("strategy2_scanner.py")
    return fixes, sent


def test_one_flaky_probe_neither_alerts_nor_repairs(monkeypatch, tmp_path):
    """The blip case. A repair here would have CREATED a 30-60s outage."""
    fixes, sent = _pub_tick(monkeypatch, tmp_path, [False, True])
    assert fixes == [], "a single failed probe triggered a site-down repair"
    assert sent == [], "a single failed probe alarmed the owner"


def test_two_consecutive_failures_do_repair(monkeypatch, tmp_path):
    """A real outage must still be caught and fixed unattended."""
    fixes, sent = _pub_tick(monkeypatch, tmp_path, [False, False])
    assert len(fixes) == 1, "a confirmed outage was not repaired"
    assert sent, "a confirmed outage was not announced"


def test_the_strike_counter_resets_on_a_good_check(monkeypatch, tmp_path):
    """Alternating fail/pass is a flaky probe, not an outage. Without a reset
    it would accumulate strikes forever and eventually fire anyway."""
    fixes, _ = _pub_tick(monkeypatch, tmp_path,
                         [False, True, False, True, False, True])
    assert fixes == [], "alternating blips accumulated into a false outage"


def test_a_sustained_outage_is_still_caught_within_two_checks(monkeypatch, tmp_path):
    fixes, sent = _pub_tick(monkeypatch, tmp_path, [False, False, False])
    assert len(fixes) >= 1
    assert sent


def test_confirmation_is_configurable_but_never_zero():
    """PUBLIC_STRIKES=0 or 1 would restore the exact behaviour this replaced."""
    import watchdog as W
    assert W.PUBLIC_STRIKES >= 2


# ── 🔕 alert mute switches (added 2026-08-14) ───────────────────────────────
def test_muting_stops_the_message_not_the_detector(monkeypatch, tmp_path):
    """The distinction the whole module rests on. Turning the DETECTOR off
    would silently stop the pump radar feeding the dashboard strip and the
    record — "I muted a notification" must never mean "I stopped collecting
    the data"."""
    import alert_prefs as P
    import telegram_utils as T
    monkeypatch.setattr(P, "STORE_FILE", str(tmp_path / "prefs.json"))
    sent = []
    monkeypatch.setattr(T, "_route", lambda ch, force: ("tok", {"chat_id": "1"}, "b"))
    monkeypatch.setattr(T, "_post_one",
                        lambda url, body, retries: (sent.append(body), (True, 1))[1])

    assert T.send_message("x", kind="mover") is not False
    assert len(sent) == 1
    P.mute("mover")
    assert T.send_message("x", kind="mover") is False
    assert len(sent) == 1, "a muted alert still reached Telegram"
    # a DIFFERENT kind is unaffected
    assert T.send_message("y", kind="crowd") is not False
    assert len(sent) == 2


def test_an_untagged_send_is_never_silenced(monkeypatch, tmp_path):
    """Most call sites pass no kind. If an unknown/absent kind resolved to
    'muted', a typo at one call site would disable an alert nobody knows is
    off — silent, and only discoverable by missing something."""
    import alert_prefs as P
    monkeypatch.setattr(P, "STORE_FILE", str(tmp_path / "prefs.json"))
    P.mute("mover")
    assert P.is_muted(None) is False
    assert P.is_muted("") is False
    assert P.is_muted("not_a_registered_kind") is False


def test_only_registered_kinds_can_be_muted(monkeypatch, tmp_path):
    """Otherwise '/mute mvoer' silently succeeds and mutes nothing."""
    import alert_prefs as P
    monkeypatch.setattr(P, "STORE_FILE", str(tmp_path / "prefs.json"))
    assert P.mute("mvoer") is False
    assert P.load()["muted"] == []


def test_mute_survives_a_restart(monkeypatch, tmp_path):
    import alert_prefs as P
    monkeypatch.setattr(P, "STORE_FILE", str(tmp_path / "prefs.json"))
    P.mute("mover")
    assert P.is_muted("mover") is True
    assert P.unmute("mover") is True
    assert P.is_muted("mover") is False


def test_a_corrupt_prefs_file_does_not_silence_everything(monkeypatch, tmp_path):
    """Failing closed here would mute every alert at once, which is the worst
    possible direction for this particular failure."""
    import alert_prefs as P
    f = tmp_path / "prefs.json"
    f.write_text("{ not json")
    monkeypatch.setattr(P, "STORE_FILE", str(f))
    assert P.load()["muted"] == []
    assert P.is_muted("mover") is False


def test_a_prefs_failure_never_eats_an_alert(monkeypatch):
    """telegram_utils imports prefs inside a try. If that raised, every alert
    in the system would stop."""
    import telegram_utils as T
    import alert_prefs as P
    monkeypatch.setattr(P, "is_muted",
                        lambda k: (_ for _ in ()).throw(RuntimeError("boom")))
    sent = []
    monkeypatch.setattr(T, "_route", lambda ch, force: ("tok", {"chat_id": "1"}, "b"))
    monkeypatch.setattr(T, "_post_one",
                        lambda url, body, retries: (sent.append(body), (True, 1))[1])
    T.send_message("x", kind="mover")
    assert len(sent) == 1


def test_the_mover_alert_is_tagged():
    """The one the owner asked to silence. Untagged, /mute mover does nothing
    and the failure is invisible — the command reports success."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "strategy2_scanner.py"), encoding="utf-8").read()
    assert 'kind="mover"' in src


def test_the_mute_command_parses_its_argument():
    """`args` is the raw string after the command, not a list. args[0] takes
    the first CHARACTER, so '/mute mover' would look for a kind called 'm'."""
    import tempfile
    import alert_prefs as P
    import tg_commands as TC
    old = P.STORE_FILE
    P.STORE_FILE = tempfile.mktemp()
    try:
        assert "已靜音" in TC.handle("mute", "mover")
        assert P.is_muted("mover") is True
        assert "已開啟" in TC.handle("unmute", "mover")
        assert "不認得" in TC.handle("mute", "nonsense")
        assert "通知開關" in TC.handle("mute", "")
    finally:
        P.STORE_FILE = old


# ── one ticker at a time (2026-08-14) ────────────────────────────────────────
def test_two_processes_cannot_tick_at_once(tmp_path):
    """BOTH scanners call watchdog.tick(). A tick is load → probe the relays
    over the network → save, so without exclusion the process that loads first
    and saves last discards whatever the other recorded in between.

    Concretely: `public_fix` is the repair-attempt list that caps how often
    `tailscale.sh rearm` may run, and rearm takes the public site down for
    30-60s. On 2026-08-14 the entries kept being clobbered and the log read
    "auto-rearm #1" three times in 36 minutes — an hourly budget that never
    accumulated.

    Real subprocesses, because flock is held per open file and an in-process
    test would prove nothing about the case that actually happens.
    """
    import subprocess
    import sys
    import textwrap
    import os as _os

    app_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    lock = str(tmp_path / "wd.lock")
    # Drives watchdog's OWN _tick_lock, not a hand-rolled flock — the point is
    # that this module excludes correctly, not that flock does.
    prog = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {app_dir!r})
        import watchdog as W
        W.LOCK_FILE = {lock!r}
        with W._tick_lock() as mine:
            print("ACQUIRED" if mine else "BLOCKED", flush=True)
            if mine:
                time.sleep(float(sys.argv[1]))
    """)
    holder = subprocess.Popen([sys.executable, "-c", prog, "3"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        second = subprocess.run([sys.executable, "-c", prog, "0"],
                                capture_output=True, text=True, timeout=30)
        assert second.stdout.strip() == "BLOCKED", \
            f"a second process entered the tick while the first held it: {second.stdout!r}"
    finally:
        holder.kill()
        holder.wait()


def test_the_lock_is_released_so_the_next_tick_can_run(monkeypatch, tmp_path):
    """A lock that is not released turns a five-minute watchdog into a
    one-shot: strictly worse than the race it replaced."""
    import watchdog as W
    monkeypatch.setattr(W, "LOCK_FILE", str(tmp_path / "wd.lock"))
    for _ in range(3):
        with W._tick_lock() as mine:
            assert mine is True


def test_a_skipped_tick_reports_nothing_rather_than_lying(monkeypatch, tmp_path):
    """The loser of the race must return "no alerts", not raise and not claim
    someone else's."""
    import watchdog as W
    monkeypatch.setattr(W, "LOCK_FILE", str(tmp_path / "wd.lock"))
    monkeypatch.setattr(W, "_tick", lambda n: ["should not run"])
    with W._tick_lock():                      # hold it, as the other scanner would
        assert W.tick("strategy2_scanner.py") == []
