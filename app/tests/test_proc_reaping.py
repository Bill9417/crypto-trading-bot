"""
Fire-and-forget children must still be reaped.

Mutation-checked: deleting the proc_util.reap(...) wrapper in either caller
turns the two "wired" tests red, and weakening reap() itself turns the real-
process test red. A guard that has never been seen to fail is not evidence.
"""
import subprocess
import time

import proc_util
import pytest


class _FakeProc:
    """A spawn result that records whether anyone waited for it."""

    def __init__(self):
        self.waited = False

    def wait(self):
        self.waited = True
        return 0


def _settle(pred, timeout=3.0):
    """The reaper runs on its own thread, so poll rather than assume."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_reap_waits_on_a_real_child():
    proc = subprocess.Popen(["/bin/bash", "-c", "exit 3"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    assert proc_util.reap(proc) is True
    assert _settle(lambda: proc.returncode is not None), "child never reaped"
    assert proc.returncode == 3
    # And the process table agrees — no <defunct> row left behind.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                         capture_output=True, text=True).stdout
    assert "Z" not in out


def test_reap_declines_a_test_double_instead_of_crashing():
    # request(spawn=lambda *a, **k: None) is a real pattern in this suite; the
    # reaper must not be the thing that breaks the path it is protecting.
    assert proc_util.reap(None) is False
    assert proc_util.reap(object()) is False


def test_restart_request_reaps_its_child(tmp_path, monkeypatch):
    import restart_ctl
    monkeypatch.setattr(restart_ctl, "STATE_FILE", str(tmp_path / "restart.json"))
    monkeypatch.setattr(restart_ctl, "LOG_FILE", str(tmp_path / "logs" / "r.log"))
    fake = _FakeProc()
    ok, _ = restart_ctl.request("test", spawn=lambda *a, **k: fake)
    assert ok is True
    assert _settle(lambda: fake.waited), \
        "restart.sh is spawned but never reaped — a red preflight leaves a zombie"


def test_autofix_reaps_its_child(monkeypatch):
    import watchdog
    if not __import__("os").path.isfile(
            __import__("os").path.join(watchdog.REPO_ROOT, "tailscale.sh")):
        pytest.skip("tailscale.sh not present")
    fake = _FakeProc()
    assert watchdog.run_autofix(spawn=lambda *a, **k: fake) is True
    assert _settle(lambda: fake.waited), \
        "tailscale.sh rearm is spawned but never reaped — this parent always survives"
