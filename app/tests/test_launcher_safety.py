"""Single-instance invariant: never two of the same process.

2026-08-09 a strategy2_scanner outlived the pkill that was supposed to replace
it and ran ~11h beside its replacement. Nothing crashed and nothing logged an
error worth noticing — `ps` showed four healthy processes, because the
replacement had started perfectly. What actually broke was Telegram: two
processes long-polling getUpdates on one bot token makes Telegram answer the
loser with 409 Conflict, so /winrate /positions /restart and every inline
button were dead for half a day.

That failure is invisible by construction, which is why it gets tests rather
than a comment. They guard the three properties that would have prevented it:

  1. a launch cannot start on top of a survivor         (verify, then SIGKILL)
  2. two launches cannot interleave                     (the lock is on the
                                                         operation, not on one
                                                         of its three callers)
  3. exactly one process may poll getUpdates            (the 409 itself)

These read shell source because the launcher IS shell. A test that shelled out
to run_all.sh would kill the developer's own live stack, so the assertions are
made against the text — enough to catch the reintroduction of `pkill && echo
stopped`, which is the specific mistake that caused this.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "app")


def _read(*parts):
    path = os.path.join(ROOT, *parts)
    if not os.path.exists(path):
        pytest.skip(f"{path} not present")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── 1. a kill must be verified, not assumed ──────────────────────────────────
def test_run_all_verifies_kills_instead_of_trusting_pkill():
    """pkill's exit status says a signal was SENT, never that anything died.

    The old line was `pkill -f ... && echo "stopped"`, which prints a
    reassuring message on a process that is still running.
    """
    sh = _read("run_all.sh")
    assert "kill_pattern" in sh, "the verified-kill helper is gone"
    assert "pkill -9" in sh, "nothing escalates to SIGKILL — a trapped or blocked SIGTERM would survive"
    assert re.search(r"pgrep -f .*\|\|.*return 0", sh), \
        "kill_pattern must re-check with pgrep; without it it is just pkill again"


def test_no_stack_process_is_killed_without_verification():
    """Every one of the four stack processes goes through kill_pattern.

    Adding a fifth process and reaching for a bare pkill is the obvious way to
    reintroduce this, so the check is on the pattern, not on a list of names.
    """
    sh = _read("run_all.sh")
    body = sh.split("kill_pattern() {", 1)[-1]
    # `ython`, not `python`: every pattern here uses the [p]ython bracket trick
    # to stop pgrep matching itself, so the literal word never appears. Matching
    # on "python" made this assertion vacuous against the exact lines it exists
    # to reject.
    stray = [
        ln.strip() for ln in body.splitlines()
        if re.search(r"^\s*pkill -f .*ython", ln) and "pkill -9" not in ln
    ]
    assert not stray, f"these bypass kill_pattern and are unverified: {stray}"


def test_launch_aborts_rather_than_stacking_on_a_survivor():
    """If something refuses to die, NOT restarting is the safe outcome.

    A failed restart leaves the old stack running and is obvious. A duplicate
    is neither: it double-alerts, double-spends the 200/month LINE quota, and
    takes the Telegram command bot down entirely.
    """
    sh = _read("run_all.sh")
    clean = sh.split("--- clean slate", 1)[-1].split("HOST=", 1)[0]
    assert "exit 1" in clean, "a survivor must abort the launch, not be launched over"


# ── 2. one launch at a time, whoever asks ────────────────────────────────────
def test_the_launch_lock_lives_on_the_operation_not_on_one_caller():
    """Three callers race here: restart.sh (Telegram /restart), autoheal.sh
    (launchd, every 5 min) and a human. restart.sh's own lock only serialises
    restart-vs-restart; autoheal calls run_all.sh directly and never sees it.

    kill(A) · kill(B) · start(A) · start(B) leaves both stacks alive.
    """
    sh = _read("run_all.sh")
    assert "LAUNCH_LOCK" in sh, "run_all.sh has no lock of its own"
    assert "mkdir \"$LAUNCH_LOCK\"" in sh, "the lock must be mkdir — it is the atomic primitive here"
    lock_at = sh.index("LAUNCH_LOCK=")
    kill_at = sh.index("--- clean slate")
    assert lock_at < kill_at, "the lock is taken AFTER the kill — it protects nothing"


def test_a_dead_launch_cannot_wedge_the_lock_forever():
    """run_all.sh pkills a process tree and can be killed mid-flight. A lock
    that outlives its owner would disable /restart AND auto-heal permanently —
    strictly worse than the race it prevents."""
    sh = _read("run_all.sh")
    assert "LOCK_AGE" in sh and "rmdir" in sh, "no stale-lock recovery"
    assert re.search(r"LOCK_AGE.*-gt\s+\d+", sh), "stale lock is never aged out"


def test_the_lock_is_released_on_every_exit_path():
    sh = _read("run_all.sh")
    assert re.search(r"trap .*rmdir .*LAUNCH_LOCK.* EXIT", sh), \
        "without a trap, an aborted pre-flight leaves the lock held"


# ── 3. the scanner must actually be able to exit ─────────────────────────────
def test_scanner_shutdown_cannot_hang_on_a_thread_join():
    """strategy2_scanner traps SIGTERM, which replaces the kernel's guaranteed
    kill with a Python unwind through threading._shutdown(). Every thread this
    app starts is daemon=True, but market_intel runs a concurrent.futures pool
    whose workers are not, and a worker parked in a socket read never returns.

    bot.py already hit this and ends in os._exit; the scanner did not.
    """
    src = _read("app", "strategy2_scanner.py")
    assert "signal.SIGTERM" in src.replace("_signal.", "signal."), \
        "test is stale — the scanner no longer traps SIGTERM"
    tail = src[src.index('if __name__ == "__main__":'):]
    assert "os._exit(0)" in tail, \
        "a trapped SIGTERM with no hard exit can hang forever in threading._shutdown()"


def test_every_sigterm_trap_in_the_stack_has_a_hard_exit():
    """The rule, not the instance: trapping SIGTERM throws away the only kill
    the kernel guarantees. Anything that does so owes a hard exit."""
    for name in ("strategy2_scanner.py", "strategy3_scanner.py", "bot.py", "app.py"):
        src = _read("app", name)
        traps = "signal.SIGTERM" in src.replace("_signal.", "signal.")
        if not traps:
            continue          # no trap = kernel default = guaranteed death
        assert "os._exit(" in src, (
            f"{name} traps SIGTERM but never hard-exits — it can outlive its own "
            f"replacement, and two of anything here means duplicate alerts")


# ── the reason all of the above matters ──────────────────────────────────────
def test_only_one_module_may_poll_getupdates():
    """Telegram allows exactly ONE getUpdates poller per bot token; a second
    gets 409 Conflict and the commands stop working. One starter, so that
    'is the poller running?' is answerable by asking about one process.
    """
    starters = []
    for fn in sorted(os.listdir(APP)):
        if not fn.endswith(".py") or fn == "tg_commands.py":
            continue
        with open(os.path.join(APP, fn), encoding="utf-8") as fh:
            if "tg_commands.start()" in fh.read():
                starters.append(fn)
    assert starters == ["strategy2_scanner.py"], (
        f"getUpdates must have exactly one starter, found {starters} — "
        f"a second poller returns 409 Conflict and kills every bot command")
