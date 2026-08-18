"""
Restart control — the stack gets restarted from wherever you already are.

Every code change used to end the same way: "needs ./run_all.sh bg", typed in
a terminal on the Mac. So a fix could be committed, tested and pushed and the
live processes would keep running the OLD code until the operator happened to
sit down at that machine. /health could already SEE the staleness; nothing
could act on it. Three ways in now, one path out:

  • /restart in Telegram (owner only, answered in the owner's DM)
  • the 🔄 立即重啟 button on the "new code is waiting" notice
  • RESTART_AUTO=true — the watchdog restarts by itself once the code settles
    (OFF by default; see the naked-position note on request())

The restart is deliberately NOT run in-process. ./run_all.sh bg pkills every
python process it knows about, including whichever one asked for the restart,
so an in-process call would be killed halfway and could never report back.
Instead the ask spawns restart.sh in its OWN session: that bash script is not
a python process, survives the kill, and reports the outcome itself. It can
therefore also report the one case that matters most — preflight (lint +
tests) failing, where run_all.sh exits BEFORE killing anything and the stack
still running is the old one, untouched.

Which files make a process stale is DERIVED, not listed. The hand-written
list this replaced never mentioned tg_commands.py or watchdog.py, so edits to
the command bot and the watchdog itself — this very feature — would have been
invisible to the staleness check that is supposed to announce them.
"""
import json
import os
import re
import subprocess
import time

import proc_util

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
STATE_FILE = os.path.join(APP, "restart_state.json")
LOG_FILE = os.path.join(APP, "logs", "restart.log")
RUNNER = os.path.join(ROOT, "restart.sh")

# (key, ps command pattern, entry module) — the source list for each is the
# import closure of the entry module, computed below.
PROCS = (
    ("web", r"python\S*\s+(-u\s+)?(\S*/)?app\.py", "app.py"),
    ("bot", r"python\S*\s+(-u\s+)?(\S*/)?bot\.py", "bot.py"),
    ("s2", r"python\S*\s+(-u\s+)?(\S*/)?strategy2_scanner\.py", "strategy2_scanner.py"),
    ("s3", r"python\S*\s+(-u\s+)?(\S*/)?strategy3_scanner\.py", "strategy3_scanner.py"),
)
LABELS = {"web": "Web dashboard", "bot": "S1 bot",
          "s2": "S2 scanner", "s3": "S3 flip"}

# A restart already in flight blocks another for this long. run_all.sh runs the
# whole test suite first, so the window between "asked" and "new processes up"
# is minutes, not seconds, and a double-tap must not launch two of them.
INFLIGHT_SEC = float(os.getenv("RESTART_INFLIGHT_SEC", "600"))
# mtime grace: a process is only stale if the file changed at least this long
# AFTER it started (ps start times are second-resolution).
MTIME_GRACE_SEC = 2


# ── which files matter to which process ──────────────────────────────────────
# Matches top-level AND function-local imports on purpose. This codebase
# imports inside functions all over (tg_commands.handle() alone has a dozen),
# and for "does the running code still match the disk?" a superset is the safe
# direction to be wrong in: an unnecessary restart costs a minute, a missed one
# runs stale code for days.
_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[ \t]+import"
    r"|import[ \t]+([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*))",
    re.M)
_closure_cache = {"ts": 0.0, "map": {}}
CLOSURE_TTL_SEC = 60


def _local_imports(path: str) -> set:
    """Module names imported by one file that are themselves files in app/."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return set()
    names = set()
    for frm, imp in _IMPORT_RE.findall(src):
        if frm:
            names.add(frm)
        for part in (imp or "").split(","):
            part = part.strip()
            if part:
                names.add(part)
    return {n for n in names if os.path.isfile(os.path.join(APP, n + ".py"))}


def sources(entry: str) -> tuple:
    """Every app/ file the entry module can reach, plus .env — i.e. the files
    whose edit means the running process no longer matches the disk."""
    now = time.time()
    if now - _closure_cache["ts"] > CLOSURE_TTL_SEC:
        _closure_cache["ts"], _closure_cache["map"] = now, {}
    hit = _closure_cache["map"].get(entry)
    if hit is not None:
        return hit
    seen, queue = {entry}, [entry]
    while queue:
        cur = queue.pop()
        for name in _local_imports(os.path.join(APP, cur)):
            fname = name + ".py"
            if fname not in seen:
                seen.add(fname)
                queue.append(fname)
    out = tuple(sorted(seen) + [".env"])
    _closure_cache["map"][entry] = out
    return out


# ── process table ────────────────────────────────────────────────────────────
def ps_snapshot() -> list:
    """One `ps` pass → [{pid, started, rss_kb, cmd}] for every process."""
    from datetime import datetime
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,lstart=,rss=,command="],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"},   # stable month names for lstart
        ).stdout
    except Exception:  # noqa: BLE001 — health must never take the page down
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 7)   # pid dow mon day hh:mm:ss year rss command
        if len(parts) < 8:
            continue
        pid, _dow, mon, day, hms, year, rss, cmd = parts
        try:
            started = datetime.strptime(f"{mon} {day} {hms} {year}", "%b %d %H:%M:%S %Y")
        except ValueError:
            started = None
        rows.append({"pid": int(pid), "started": started,
                     "rss_kb": int(rss) if rss.isdigit() else 0, "cmd": cmd})
    return rows


def changed_since(entry: str, started_ts: float, base: str = None) -> list:
    """Source files of `entry` modified after a process started at started_ts."""
    base = base or APP
    out = []
    for fname in sources(entry):
        try:
            if os.path.getmtime(os.path.join(base, fname)) > started_ts + MTIME_GRACE_SEC:
                out.append(fname)
        except OSError:
            continue
    return out


def stale(ps=None) -> list:
    """[{key, label, pid, changed}] for every RUNNING process whose code moved
    on disk after it started. A process that is down is not stale — that is the
    watchdog's separate, louder problem."""
    ps = ps_snapshot() if ps is None else ps
    out = []
    for key, pattern, entry in PROCS:
        for p in ps:
            if not re.search(pattern, p["cmd"]) or not p["started"]:
                continue
            changed = changed_since(entry, p["started"].timestamp())
            if changed:
                out.append({"key": key, "label": LABELS.get(key, key),
                            "pid": p["pid"], "changed": changed})
            break
    return out


def newest_change_ts(entries: list, base: str = None) -> float:
    """Most recent mtime across every changed file — 'when did editing stop'."""
    base = base or APP
    newest = 0.0
    for e in entries:
        for fname in e.get("changed") or []:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(base, fname)))
            except OSError:
                continue
    return newest


def tree_dirty() -> bool:
    """True only when git is certain there are uncommitted changes. Unknown
    (no git, no repo, timeout) returns False: a gate that can't measure must
    not be the thing that blocks a restart."""
    try:
        r = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


# ── in-flight state ──────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = nothing in flight
        return {}


def _save(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def in_flight(state=None, now=None) -> bool:
    state = _load() if state is None else state
    now = time.time() if now is None else now
    return bool(state.get("started_at")) and not state.get("finished_at") \
        and now - state["started_at"] < INFLIGHT_SEC


# ── the ask ──────────────────────────────────────────────────────────────────
def request(reason: str = "manual", *, spawn=None) -> tuple:
    """Kick off a restart. Returns (accepted, message-for-the-asker).

    NOT reflexively safe on a live account: S1 places the entry order and then
    its stop, and a restart landing between the two is exactly how this project
    got naked positions before. The window is seconds wide and preflight has to
    pass first, but it is why RESTART_AUTO defaults to off and why this is
    owner-only — a human picks the moment.
    """
    state = _load()
    if in_flight(state):
        waited = int(time.time() - state["started_at"])
        return False, f"⏳ 已經在重啟中了（{waited} 秒前開始），請等這一次跑完。"
    if not os.path.isfile(RUNNER):
        return False, f"⚠️ 找不到 {RUNNER}"

    _save({"started_at": time.time(), "reason": reason, "finished_at": None})
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        log = open(LOG_FILE, "a")            # noqa: SIM115 — handed to the child
        log.write(f"\n=== restart requested ({reason}) ===\n")
        log.flush()
        runner = spawn or subprocess.Popen
        # start_new_session: the caller is about to be pkill'd by run_all.sh.
        # Its own session/process group dies with it; this must not.
        #
        # reap() because the caller is only USUALLY killed. When preflight goes
        # red, or another launch already holds the lock, run_all.sh returns
        # without touching anything — restart.sh then exits under a parent that
        # is still alive and never waited for it, leaving a <defunct> entry for
        # the life of that process. The failure path is exactly the path that
        # repeats.
        proc_util.reap(
            runner(["/bin/bash", RUNNER, reason], cwd=ROOT,
                   stdout=log, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL, start_new_session=True))
    except Exception as exc:  # noqa: BLE001 — a failed spawn must clear the flag
        _save({"started_at": 0, "reason": reason, "finished_at": time.time()})
        return False, f"⚠️ 重啟啟動失敗: {str(exc)[:200]}"
    return True, ("🔄 開始重啟…\n"
                  "先跑 lint + 測試，全部綠燈才會換掉現在跑的程式。\n"
                  "測試沒過的話「舊的會繼續跑」，不會停機。\n"
                  "結果會再傳一則給你（約 1–3 分鐘）。")


# ── the report back (called by restart.sh, after run_all.sh exits) ───────────
def _log_tail(lines: int = 12) -> str:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        return ""
    marker = body.rfind("=== restart requested")
    return "\n".join((body[marker:] if marker >= 0 else body).splitlines()[-lines:])


def running_keys(ps=None) -> list:
    ps = ps_snapshot() if ps is None else ps
    return [key for key, pattern, _ in PROCS
            if any(re.search(pattern, p["cmd"]) for p in ps)]


def finish_text(rc: int, up: list, tail: str = "") -> str:
    """Outcome message. rc != 0 means run_all.sh aborted — and because
    preflight runs before anything is killed, that abort left the OLD stack
    alive. Saying 'restart failed' without saying that would read as an
    outage."""
    if rc == 0:
        names = "、".join(LABELS.get(k, k) for k in up) or "（沒有偵測到）"
        return f"✅ 重啟完成\n正在跑: {names}"
    import telegram_utils
    body = telegram_utils.redact(tail).strip() if tail else ""
    head = ("❌ 重啟中止 — lint / 測試沒過。\n"
            "舊的程式還在跑，沒有停機，新的改動還沒生效。")
    return f"{head}\n\n<pre>{body[-1200:]}</pre>" if body else head


def finish(rc: int) -> None:
    state = _load()
    state["finished_at"] = time.time()
    state["rc"] = rc
    _save(state)
    import telegram_utils
    text = finish_text(rc, running_keys(), _log_tail())
    telegram_utils.send_message(
        text, parse_mode="HTML" if "<pre>" in text else None,
        force=True, channel="private")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "finish":
        finish(int(sys.argv[2]))
    else:
        print(json.dumps({"stale": stale(), "in_flight": in_flight()},
                         default=str, indent=2))
