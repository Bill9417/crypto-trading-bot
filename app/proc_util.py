"""
Reaping for fire-and-forget children.

Two places here spawn a helper script and deliberately do NOT wait for it:
restart_ctl.request() launches restart.sh, and watchdog.run_autofix() launches
tailscale.sh rearm. Both are correct not to block — the caller of the first is
about to be pkill'd by the very script it started, and the second must not
stall a monitoring sweep behind a network repair.

But not blocking is not the same as not reaping. A child that exits while its
parent is alive and has never called wait() stays in the process table as
`Z <defunct>`, holding its pid until the parent dies. Measured, not assumed:
three discarded Popen objects in a parent that then sat idle showed as

    27385 27383 ZN   <defunct>
    27386 27383 ZN   <defunct>
    27387 27383 ZN   <defunct>

CPython papers over this by accident — Popen.__init__ sweeps a list of
garbage-collected-but-unwaited children, so the NEXT subprocess call in the
same process reaps the previous one. That is why this has never been visible:
watchdog shells out to `ps` and `dig` every tick, so its zombies are cleared by
the following sweep. It is a side effect of unrelated code, in the exact shape
this project keeps getting bitten by — a thing that works because of something
that was never meant to guarantee it. Stop relying on it.

A daemon thread per spawn, not a SIGCHLD handler: a signal handler installed
here would race the ordinary subprocess.run() calls all over this codebase for
the same exit statuses, and only works from the main thread anyway — while the
watchdog runs inside a scanner's worker thread.
"""
import threading


def reap(proc):
    """wait() for `proc` on a daemon thread. Returns True if a reaper started.

    Takes whatever the spawn returned rather than spawning itself, so the
    `spawn=` injection seam both callers use for testing keeps working — and
    so a test double (no .wait) is simply declined instead of crashing the
    restart path it is standing in for.
    """
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return False

    def _reaper():
        try:
            wait()
        except Exception:  # noqa: BLE001 — a reaper that raises is worse than a zombie
            pass

    threading.Thread(target=_reaper, name="reap", daemon=True).start()
    return True
