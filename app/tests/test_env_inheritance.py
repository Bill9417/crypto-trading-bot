"""🧬 A restart must read .env, not a snapshot of it.

config.py does os.environ.setdefault() for every key in .env, so any process
that imports it carries the whole file in its environment and hands that to
every child — where setdefault() then cannot override it. One app-spawned
restart therefore freezes the .env of that moment forever, and later edits are
silently ignored down the whole chain.

Found 2026-08-20: live trading was disarmed in .env, but the running stack
still had LIVE_TRADING=true in its process environment, and a /restart would
have relaunched it ARMED. The pre-flight tests failed and aborted the launch,
which is the only reason anyone noticed.
"""
import os
import re

import config
import restart_ctl

SAFETY = ("LIVE_TRADING", "S4_EXEC", "STRATEGY3_LIVE", "S1_BYBIT_MIRROR")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))


def test_config_knows_which_keys_came_from_the_file():
    keys = config.env_file_keys()
    assert keys, "no keys recorded — the strip below has nothing to work from"
    for k in SAFETY:
        assert k in keys, f"{k} is not tracked, so a restart would inherit it"


def test_the_restart_child_does_not_inherit_env_file_values(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("S4_EXEC", "bybit")
    env = restart_ctl._clean_env()
    for k in SAFETY:
        assert k not in env, f"{k} survives into the restart — .env edits are ignored"
    # …and the child can still find an interpreter.
    assert env.get("PATH"), "PATH was stripped with the rest"


def test_request_passes_the_cleaned_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_ctl, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(restart_ctl, "LOG_FILE", str(tmp_path / "l.log"))
    monkeypatch.setenv("LIVE_TRADING", "true")
    seen = {}

    def spy(argv, **kw):
        seen.update(kw)
        return None

    ok, _ = restart_ctl.request("test", spawn=spy)
    assert ok is True
    assert "env" in seen, "the child inherits this process's environment wholesale"
    assert "LIVE_TRADING" not in seen["env"]


def test_restart_sh_strips_them_too_and_protects_path():
    """restart.sh is what actually runs, and it is re-read from disk on every
    restart — so it is the fix that works WITHOUT a restart first."""
    with open(os.path.join(ROOT, "restart.sh"), encoding="utf-8") as f:
        sh = f.read()
    assert 'unset "$_key"' in sh, "restart.sh does not strip inherited .env keys"
    # Inside the PROTECTED VALUE, not just anywhere in the file — the word
    # also appears in the comment above it.
    m = re.search(r'PROTECTED="([^"]*)"', sh)
    assert m and ' PATH ' in m.group(1), \
        'unsetting PATH would take the interpreter out with it'
    for name in ('HOME', 'SHELL'):
        assert name in m.group(1)
    # it must read the file to know what to strip
    assert '< "$APP/.env"' in sh
