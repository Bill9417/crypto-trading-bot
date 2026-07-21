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
