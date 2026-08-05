"""Regression: build_health() must have a label for every key in _HEALTH_PROCS.
Found 2026-07-04 — the 's3' entry was added to _HEALTH_PROCS when Strategy 3
shipped, but the labels dict inside build_health() was never updated to match,
so /health 500'd (KeyError: 's3') for the entire time Strategy 3 has existed."""
from datetime import datetime

import app as A


def test_every_health_proc_key_has_a_label():
    health = A.build_health()
    keys_seen = {p["key"] for p in health["processes"]}
    expected = {key for key, _, _ in A._HEALTH_PROCS}
    assert keys_seen == expected
    for p in health["processes"]:
        assert p["label"], f"missing label for {p['key']}"


# ── pipeline pane: LINE quota + site link + backup freshness ─────────────────
def test_pipeline_no_data_yet(tmp_path, monkeypatch):
    import backup_state
    import line_push
    import site_link
    monkeypatch.setattr(line_push, "enabled", lambda: False)
    monkeypatch.setattr(line_push, "quota_cached", lambda: {})
    monkeypatch.setattr(site_link, "current_url", lambda: "")
    monkeypatch.setattr(backup_state, "BACKUP_DIR", str(tmp_path / "backups"))

    p = A._build_pipeline(str(tmp_path), datetime.now())
    assert p["line"] == {"enabled": False, "used": None, "limit": None,
                         "checked_ago_sec": None}
    assert p["site_link"] == {"url": ""}
    assert p["backup"]["exists"] is False


def test_pipeline_reads_quota_and_latest_backup(tmp_path, monkeypatch):
    import backup_state
    import line_push
    import site_link
    monkeypatch.setattr(line_push, "enabled", lambda: True)
    monkeypatch.setattr(line_push, "quota_cached",
                        lambda: {"used": 165, "limit": 200, "checked_at": 1000.0})
    monkeypatch.setattr(site_link, "current_url",
                        lambda: "https://example.trycloudflare.com")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "state-2026-07-19.zip").write_bytes(b"old")
    latest = backups / "state-2026-07-20.zip"
    latest.write_bytes(b"x" * 5000)
    monkeypatch.setattr(backup_state, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(backup_state, "OFFSITE_DIR", str(tmp_path / "offsite"))

    now = datetime.fromtimestamp(latest.stat().st_mtime + 3600)   # 1h later
    p = A._build_pipeline(str(tmp_path), now)
    assert p["line"]["used"] == 165 and p["line"]["limit"] == 200
    assert p["site_link"]["url"] == "https://example.trycloudflare.com"
    assert p["backup"]["exists"] is True
    assert p["backup"]["age_hours"] == 1.0
    assert p["backup"]["size_bytes"] == 5000
    assert p["backup"]["offsite_ok"] is False   # nothing copied there in this test


def test_build_health_flags_quota_and_stale_backup_as_issues(monkeypatch):
    import line_push
    monkeypatch.setattr(line_push, "enabled", lambda: True)
    monkeypatch.setattr(line_push, "quota_cached",
                        lambda: {"used": 190, "limit": 200, "checked_at": 1000.0})
    health = A.build_health()
    texts = " ".join(i["text"] for i in health["issues"])
    assert "190/200" in texts


# ── log scanning: the failure class that hid a dead watchdog ─────────────────
# 2026-07-29: launchd's auto-restart agent had failed on every single run for
# 16 days (4550x "Operation not permitted" — macOS blocks a launchd-spawned
# bash from reading anything under ~/Desktop). Nobody noticed, for two
# independent reasons, both fixed here and both pinned below.
def _write_logs(tmp_path, files):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    for name, body in files.items():
        (logs / name).write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_autoheal_log_is_watched_at_all():
    """Reason one: /health never opened the one log that would have said so."""
    assert "autoheal.log" in A._HEALTH_LOGS


def test_os_level_failures_are_counted_as_errors(tmp_path):
    """Reason two: the regex matched none of them, so the count read zero."""
    base = _write_logs(tmp_path, {
        "autoheal.log": "/bin/bash: /path/autoheal.sh: Operation not permitted\n" * 5,
    })
    entry = {e["name"]: e for e in A._log_health(base)}["autoheal.log"]
    assert entry["recent_errors"] == 5
    assert "Operation not permitted" in entry["last_error"]


def test_other_os_failure_shapes_also_count(tmp_path):
    base = _write_logs(tmp_path, {
        "bot.log": "\n".join([
            "bash: line 1: /x.sh: Permission denied",
            "python: command not found",
            "cp: /y: No such file or directory",
            "FATAL: engine stopped",
        ]) + "\n",
    })
    entry = {e["name"]: e for e in A._log_health(base)}["bot.log"]
    assert entry["recent_errors"] == 4


def test_normal_trading_output_is_not_flagged_as_an_error(tmp_path):
    """The widened pattern must not turn routine scanner chatter red — these
    lines all appear in the real logs and none of them is a failure."""
    base = _write_logs(tmp_path, {
        "strategy2.log": "\n".join([
            "[strategy2] volume gate failed",
            "[strategy2] EMA200 trend not aligned",
            "[strategy2] sweep done in 23s; sleeping 277s",
            "[strategy2] SIGNAL SHORT SLX score 26",
            "[bot] LONG blocked: RSI >= 80.0 (overbought top)",
        ]) + "\n",
    })
    entry = {e["name"]: e for e in A._log_health(base)}["strategy2.log"]
    assert entry["recent_errors"] == 0, entry["last_error"]


def test_missing_log_file_is_reported_not_crashed(tmp_path):
    base = _write_logs(tmp_path, {})
    entries = {e["name"]: e for e in A._log_health(base)}
    assert entries["bot.log"]["exists"] is False
    assert entries["bot.log"]["recent_errors"] == 0


# ── launchd agent config ────────────────────────────────────────────────────
# The plist used to carry a hardcoded absolute path into ~/Desktop. That is
# what let the agent rot silently for 16 days. It is now a template that
# install_autoheal.sh fills in from the repo's real location, so a move is a
# one-command fix. These guard the regression.
def _repo_root():
    from pathlib import Path
    return Path(A.__file__).resolve().parent.parent


def test_launchd_plist_is_a_template_not_a_hardcoded_path():
    plist = _repo_root() / "launchd" / "com.wolfman.wolfscanner.autoheal.plist"
    body = plist.read_text(encoding="utf-8")
    assert "__PROJECT_DIR__" in body, "template placeholder missing"
    assert "/Users/" not in body, "a machine-specific absolute path crept back in"


def test_launchd_template_still_declares_the_agent_correctly():
    import plistlib
    from xml.sax.saxutils import escape
    plist = _repo_root() / "launchd" / "com.wolfman.wolfscanner.autoheal.plist"
    filled = plist.read_text(encoding="utf-8").replace("__PROJECT_DIR__", escape("/tmp/x"))
    d = plistlib.loads(filled.encode("utf-8"))
    assert d["Label"] == "com.wolfman.wolfscanner.autoheal"
    assert d["ProgramArguments"] == ["/bin/bash", "/tmp/x/autoheal.sh"]
    assert d["StandardOutPath"] == "/tmp/x/app/logs/autoheal.log"
    assert d["StartInterval"] == 300
    assert d["RunAtLoad"] is True


def test_installer_escapes_xml_so_an_ampersand_in_the_path_cannot_break_it():
    """A '&' in a directory name produced 'unknown ampersand-escape sequence'
    and a plist launchd will not load. Caught in sandbox testing."""
    import plistlib
    from xml.sax.saxutils import escape
    plist = _repo_root() / "launchd" / "com.wolfman.wolfscanner.autoheal.plist"
    hostile = "/Users/w/交易 & more/crypto"
    filled = plist.read_text(encoding="utf-8").replace("__PROJECT_DIR__", escape(hostile))
    d = plistlib.loads(filled.encode("utf-8"))          # must not raise
    assert d["ProgramArguments"][1] == f"{hostile}/autoheal.sh"


def test_installer_and_migration_scripts_are_executable():
    root = _repo_root()
    for rel in ("launchd/install_autoheal.sh", "migrate_off_desktop.sh"):
        p = root / rel
        assert p.exists(), f"{rel} missing"
        assert p.stat().st_mode & 0o111, f"{rel} is not executable"


# ── 🔒 credential audit ──────────────────────────────────────────────────────
# The dashboard moved from a rotating Cloudflare quick-tunnel URL to a
# permanent public address on 2026-08-02. "Nobody knows the link" stopped
# being a control, and an audit that day found an ADMIN account whose
# username and password were both "1".
def test_health_reports_guessable_accounts(monkeypatch):
    import app as APP
    import set_password as SP

    class _U:
        def __init__(self, name, admin):
            self.username, self.is_admin, self.password = name, admin, "x"

    monkeypatch.setattr(SP, "weak_reason",
                        lambda u, h: "password is '1'" if u == "1" else None)
    monkeypatch.setattr(APP, "_auth_health", lambda: {
        "weak_accounts": [{"username": "1", "is_admin": True, "why": "password is '1'"}],
        "public_registration": False, "cookie_secure": True})
    with APP.app.test_request_context("/health"):
        auth = APP.build_health()["auth"]
    assert [w["username"] for w in auth["weak_accounts"]] == ["1"]
    assert auth["weak_accounts"][0]["is_admin"] is True


def test_health_survives_a_broken_credential_audit(monkeypatch):
    """A failure here must never take the ops page down."""
    import app as APP
    import set_password as SP

    def _boom(*a, **k):
        raise RuntimeError("hash backend gone")

    monkeypatch.setattr(SP, "weak_reason", _boom)
    with APP.app.app_context():
        assert APP._auth_health()["weak_accounts"] == []   # degrades, never raises
    with APP.app.test_request_context("/health"):
        assert "auth" in APP.build_health()


def test_weak_reason_catches_username_derived_passwords():
    import set_password as SP
    from werkzeug.security import generate_password_hash
    for pw in ("1", "admin", "wolfman", "wolfman123", "password"):
        h = generate_password_hash(pw, method="pbkdf2:sha256")
        assert SP.weak_reason("wolfman", h), f"{pw!r} should be flagged"
    strong = generate_password_hash("k7#pQx2vLm9!Rt", method="pbkdf2:sha256")
    assert SP.weak_reason("wolfman", strong) is None


# ── which engine is actually trading ────────────────────────────────────────
# 2026-08-05: /health printed "no live engine detected" in the hero directly
# above a process list that badged S1 as LIVE and S3 as "LIVE on BYBIT". Two
# contradictory claims about real money, and both were wrong. The hero was
# rendering _live_strategy_state(), which answers the admin SWITCHER's question
# (S1 or S2, on the Binance account) and rightly says "none" when neither is
# armed — while the process roles branched on STRATEGY2_LIVE alone, giving two
# answers for three modes, so an S1 running in Bybit-mirror mode was described
# as placing real orders on the one exchange it never touches.
import json as _json


def _marker(monkeypatch, tmp_path, payload):
    import app as APP
    p = tmp_path / "bot_strategy.json"
    p.write_text(_json.dumps(payload))
    monkeypatch.setattr(APP, "_s1_runtime", lambda: _json.loads(p.read_text()))


def test_s1_mode_is_read_from_what_the_bot_recorded(monkeypatch, tmp_path):
    import app as APP
    for mode in ("binance", "bybit", "scan_only"):
        _marker(monkeypatch, tmp_path, {"strategy": "default", "exec": mode})
        assert APP._s1_mode() == mode


def test_s1_mode_falls_back_to_the_lock_not_to_binance(monkeypatch):
    """A marker with no 'exec' predates the field and could be ANY mode.
    Guessing 'binance' would reprint the exact false claim this replaced; the
    bot lock is the one unambiguous signal, because SCAN_ONLY and S1_EXEC=bybit
    both deliberately skip acquiring it."""
    import app as APP
    import strategy2_live as S2L
    monkeypatch.setattr(APP, "_s1_runtime", lambda: {"strategy": "default"})
    monkeypatch.setattr(S2L, "s1_bot_running", lambda: True)
    assert APP._s1_mode() == "binance"

    monkeypatch.setattr(S2L, "s1_bot_running", lambda: False)
    import config as _config
    monkeypatch.setattr(_config, "read_env_var", lambda k, d=None: "true")
    assert APP._s1_mode() == "bybit"          # no lock + mirror armed
    monkeypatch.setattr(_config, "read_env_var", lambda k, d=None: "false")
    assert APP._s1_mode() == "scan_only"      # no lock, no mirror


def test_a_dead_process_is_never_counted_as_trading(monkeypatch):
    import app as APP
    monkeypatch.setattr(APP, "_s1_mode", lambda: "binance")
    monkeypatch.setattr(APP, "_scanner_live_engine", lambda: True)
    assert APP._live_engines(set()) == []


def test_s1_in_mirror_mode_reports_bybit_never_binance(monkeypatch):
    import app as APP
    import s1_bybit_mirror
    monkeypatch.setattr(APP, "_s1_mode", lambda: "bybit")
    monkeypatch.setattr(APP, "_s1_runtime", lambda: {"strategy": "default"})
    monkeypatch.setattr(s1_bybit_mirror, "enabled", lambda: True)
    got = APP._live_engines({"bot"})
    assert [e["venue"] for e in got] == ["Bybit"]
    assert "🪞" in got[0]["name"]


def test_mirror_mode_with_the_mirror_off_is_trading_nothing(monkeypatch):
    """S1_EXEC=bybit halts Binance. With the mirror disabled on top of that,
    the lifecycle runs but no order reaches any exchange — claiming it is live
    would be the same class of error in the other direction."""
    import app as APP
    import s1_bybit_mirror
    monkeypatch.setattr(APP, "_s1_mode", lambda: "bybit")
    monkeypatch.setattr(s1_bybit_mirror, "enabled", lambda: False)
    assert APP._live_engines({"bot"}) == []


def test_scan_only_companion_is_never_live(monkeypatch):
    import app as APP
    monkeypatch.setattr(APP, "_s1_mode", lambda: "scan_only")
    assert APP._live_engines({"bot"}) == []


def test_s3_counts_only_when_it_is_actually_armed(monkeypatch):
    import app as APP
    import strategy3_scanner as S3
    monkeypatch.setattr(APP, "_s1_mode", lambda: "scan_only")
    monkeypatch.setattr(S3, "mode_string", lambda: "LIVE on BYBIT")
    assert [e["key"] for e in APP._live_engines({"s3"})] == ["s3"]
    monkeypatch.setattr(S3, "mode_string", lambda: "ALERT-ONLY (STRATEGY3_LIVE=false)")
    assert APP._live_engines({"s3"}) == []


def test_the_regression_two_bybit_engines_are_both_reported(monkeypatch):
    """The exact live configuration that produced the contradiction:
    STRATEGY3_LIVE=true with the S1 Bybit mirror on. Both are trading, both on
    Bybit, and Binance is being traded by nothing."""
    import app as APP
    import s1_bybit_mirror
    import strategy3_scanner as S3
    monkeypatch.setattr(APP, "_s1_mode", lambda: "bybit")
    monkeypatch.setattr(APP, "_s1_runtime", lambda: {"strategy": "default"})
    monkeypatch.setattr(s1_bybit_mirror, "enabled", lambda: True)
    monkeypatch.setattr(S3, "mode_string", lambda: "LIVE on BYBIT")
    monkeypatch.setattr(APP, "_scanner_live_engine", lambda: False)
    got = APP._live_engines({"bot", "s2", "s3"})
    assert {e["key"] for e in got} == {"bot", "s3"}
    assert {e["venue"] for e in got} == {"Bybit"}


def test_the_page_never_contradicts_itself_about_live_money():
    """The whole point: a process badged LIVE and a hero saying nothing is
    trading cannot both be on the page."""
    import app as APP
    with APP.app.test_request_context("/health"):
        h = APP.build_health()
    badged = {p["key"] for p in h["processes"] if p["live"]}
    hero = {e["key"] for e in (h["engine"].get("live") or [])}
    assert badged == hero
    for p in h["processes"]:
        assert not (p["live"] and not p["running"]), f"{p['key']} live but not running"


def test_the_s1_role_names_its_exchange(monkeypatch):
    """'places real orders' with no venue reads as Binance."""
    import app as APP
    for mode, must in (("bybit", "BYBIT"), ("binance", "Binance")):
        monkeypatch.setattr(APP, "_s1_mode", lambda m=mode: m)
        with APP.app.test_request_context("/health"):
            role = next(p["role"] for p in APP.build_health()["processes"]
                        if p["key"] == "bot")
        assert must in role, f"{mode}: {role!r} does not say where it trades"
    monkeypatch.setattr(APP, "_s1_mode", lambda: "scan_only")
    with APP.app.test_request_context("/health"):
        role = next(p["role"] for p in APP.build_health()["processes"] if p["key"] == "bot")
    assert "NO orders" in role
