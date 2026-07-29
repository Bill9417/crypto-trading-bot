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
