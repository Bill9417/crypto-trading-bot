"""Regression: build_health() must have a label for every key in _HEALTH_PROCS.
Found 2026-07-04 — the 's3' entry was added to _HEALTH_PROCS when Strategy 3
shipped, but the labels dict inside build_health() was never updated to match,
so /health 500'd (KeyError: 's3') for the entire time Strategy 3 has existed."""
import app as A


def test_every_health_proc_key_has_a_label():
    health = A.build_health()
    keys_seen = {p["key"] for p in health["processes"]}
    expected = {key for key, _, _ in A._HEALTH_PROCS}
    assert keys_seen == expected
    for p in health["processes"]:
        assert p["label"], f"missing label for {p['key']}"
