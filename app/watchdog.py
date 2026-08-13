"""
Process watchdog — a dead stack process becomes a Telegram alert, not silence.

The S2 and S3 scanners each call tick() every sweep, so they watch each
other (and the web app + S1 bot): if any expected process disappears from
the process table, a 🚨 alert goes to the Alerts topic (once per hour per
process), and a ✅ recovery note when it comes back. If BOTH scanners die at
once nobody is left to bark — that residual risk is accepted; the /health
page still shows it.
"""
import json
import os
import shutil
import subprocess
import time

EXPECTED = {
    "app.py": "web dashboard",
    "bot.py": "S1 bot",
    "strategy2_scanner.py": "S2 scanner",
    "strategy3_scanner.py": "S3 scanner",
}
# The tunnel isn't a python script EXPECTED matches on (parse_running only
# looks at "python" lines) — it gets its own pseudo-entry so the alert loop
# can share the same down/last_alert/recovery machinery.
TUNNEL_KEY = "cloudflared"
TUNNEL_LABEL = "Cloudflare 通道（對外網址）"
TUNNEL_FIX = "./tunnel.sh start"
# The public URL moved to a Tailscale Funnel, which is a system service and not
# a cloudflared process — so this check found "cloudflared tunnel" missing and
# fired a 🚨 alert every hour for a component that was retired on purpose.
# Opt-in now: set WATCHDOG_WATCH_TUNNEL=true only while a quick-tunnel is the
# public URL again.
WATCH_TUNNEL = os.getenv("WATCHDOG_WATCH_TUNNEL", "false").strip().lower() \
    in ("1", "true", "yes")

# ── the public URL, checked the only way that tells the truth ────────────────
# 2026-08-05: the Tailscale Funnel was registered, the ACL granted funnel, the
# HTTPS cert was valid and `tailscale funnel status` said "Funnel on" — and the
# public relays served nothing. LINE could not reach its webhook, so every 指令
# silently did nothing and the Quick Reply buttons never came back; /tw, /us and
# /welcome were dead for everyone off the tailnet. Nothing noticed, because the
# process watchdog only watches processes.
#
# It must NOT be checked over localhost or by hostname. MagicDNS resolves
# *.ts.net to the 100.x tailnet address from this Mac, so both answer 200 while
# the public path is down — which is exactly why a browser here looked fine.
# Public DNS gives the relay IPs; each is forced with --resolve.
PUBLIC_KEY = "public_url"
PUBLIC_LABEL = "對外網址（Tailscale Funnel）"
# ONE command on purpose. The two-step form was pasted as
# "./tailscale.sh off && on" on 2026-08-05 — the second half was not a command,
# so the public URL went from intermittently down to fully down. An alert that
# can be half-followed is an alert that can make things worse.
PUBLIC_FIX = "./tailscale.sh rearm"
# ── and then run that fix ourselves ─────────────────────────────────────────
# 2026-08-05: the Funnel registration went stale THREE times in one evening.
# Each time `tailscale funnel status` said "Funnel on", the cert was valid, the
# node was online, the Mac was awake (sleep is pinned by caffeinate) and no
# process of ours touches tailscale — the ingress simply stopped forwarding.
# `./tailscale.sh rearm` cured it every time. A fault we can detect every five
# minutes, with a remedy known to work, should not be waiting on a human to
# read a message: telling the owner their public site is down is worth much
# less than the site not being down.
#
# Spawned DETACHED rather than run inline: rearm takes up to a minute waiting
# on relays, and tick() is called from inside a scanner sweep — blocking there
# would delay signal detection to fix a web page. The next tick (5 min) sees
# the result and the existing recovery path announces it.
PUBLIC_AUTOFIX = os.getenv("PUBLIC_AUTOFIX", "true").strip().lower() \
    in ("1", "true", "yes")
# Capped per hour. If rearm is not curing it, the fault is something else and
# re-running it forever would bury that fact under its own noise.
PUBLIC_AUTOFIX_MAX = int(os.getenv("PUBLIC_AUTOFIX_MAX", "4"))
PUBLIC_AUTOFIX_WINDOW_SEC = 3600
# Consecutive failed checks before the URL counts as down. See the long note at
# the call site: the repair itself causes 30-60s of downtime, so acting on one
# flaky probe let the watchdog manufacture the outages it then reported.
PUBLIC_STRIKES = int(os.getenv("PUBLIC_STRIKES", "2"))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH_PUBLIC = os.getenv("WATCHDOG_WATCH_PUBLIC", "true").strip().lower() \
    in ("1", "true", "yes")
PUBLIC_PATH = os.getenv("WATCHDOG_PUBLIC_PATH", "/welcome")
PUBLIC_TIMEOUT = float(os.getenv("WATCHDOG_PUBLIC_TIMEOUT", "15"))
# ── new code that isn't running yet ─────────────────────────────────────────
# A stale process is not a broken one, so this is a nudge, not a 🚨: the code
# on disk moved after the process started, and nothing will pick it up until
# someone restarts. Two gates keep it from nagging mid-session:
#   • QUIET_SEC — the changed files must have stopped changing. Otherwise it
#     fires in the middle of a multi-file edit, when half the change is saved.
#   • a dirty git tree suppresses it entirely. Work in progress is not a
#     release; a clean tree is the closest honest signal that a batch is done.
# WATCH_STALE=false turns it off; RESTART_AUTO=true makes it act instead of ask.
WATCH_STALE = os.getenv("WATCHDOG_WATCH_STALE", "true").strip().lower() \
    in ("1", "true", "yes")
RESTART_AUTO = os.getenv("RESTART_AUTO", "false").strip().lower() \
    in ("1", "true", "yes")
STALE_QUIET_SEC = float(os.getenv("RESTART_QUIET_SEC", "600"))
STALE_COOLDOWN_SEC = float(os.getenv("RESTART_NOTICE_COOLDOWN_SEC", "21600"))  # 6h

STATE_FILE = os.path.join(os.path.dirname(__file__), "watchdog_state.json")
CHECK_SEC = 300
ALERT_COOLDOWN_SEC = 3600
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_logs(log_dir: str = None, max_bytes: int = MAX_LOG_BYTES) -> list:
    """Copy-truncate any oversized log so multi-week runs stay bounded
    (run_all.sh only rotates at restart). A plain mv would orphan the >>
    append fds the running processes hold — so copy to .1, then truncate in
    place; O_APPEND writers continue cleanly at the new EOF. The handful of
    lines written between copy and truncate can be lost — acceptable."""
    rotated = []
    d = log_dir or LOG_DIR
    try:
        names = [n for n in os.listdir(d) if n.endswith(".log")]
    except OSError:
        return rotated
    for name in sorted(names):
        path = os.path.join(d, name)
        try:
            if os.path.getsize(path) <= max_bytes:
                continue
            shutil.copyfile(path, path + ".1")
            with open(path, "r+") as f:
                f.truncate(0)
            rotated.append(name)
            print(f"[watchdog] rotated {name} (was over {max_bytes >> 20}MB)")
        except OSError as exc:  # noqa: PERF203 — per-file failure must not stop the rest
            print(f"[watchdog] rotate failed {name}: {exc}")
    return rotated


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = fresh start
        return {}


def _save_state(state: dict) -> None:
    # The temp name carries the PID. BOTH scanners call tick(), and with a
    # shared "<file>.tmp" they raced: each wrote the same temp path, the first
    # os.replace consumed it, and the second died with ENOENT — twice in the
    # S2 log. Harmless in itself (the state is rewritten 5 minutes later), but
    # it aborted the rest of that sweep's watchdog pass, so an alert could be
    # skipped by a coin flip. A per-process temp file cannot collide, and
    # os.replace stays atomic.
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def parse_running(ps_output: str, expected=None) -> set:
    """Which EXPECTED script names appear as python processes in ps output."""
    expected = expected or EXPECTED
    found = set()
    for line in ps_output.splitlines():
        if "python" not in line:
            continue
        for name in expected:
            if line.rstrip().endswith(name):
                found.add(name)
    return found


def missing(running: set, expected=None) -> list:
    expected = expected or EXPECTED
    return sorted(n for n in expected if n not in running)


def public_host() -> str:
    """Hostname of the public URL, or '' when none is configured."""
    import config
    base = (os.getenv("PUBLIC_BASE_URL")
            or getattr(config, "PUBLIC_BASE_URL", "") or "").strip()
    return base.split("://")[-1].split("/")[0] if base else ""


def public_ips(host: str) -> list:
    """Relay IPs from PUBLIC DNS. The system resolver returns the 100.x tailnet
    address on this machine, which is the whole reason a local check lies."""
    try:
        out = subprocess.run(["dig", "+short", "@1.1.1.1", host],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    return [l.strip() for l in out.splitlines()
            if l.strip() and l.strip()[0].isdigit()][:3]


def _relay_serves(host: str, ip: str, attempts: int = 2) -> bool:
    """One relay, retried once — a single timeout is a blip, not an outage."""
    for _ in range(attempts):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", str(int(PUBLIC_TIMEOUT)),
                 "--resolve", f"{host}:443:{ip}",
                 f"https://{host}{PUBLIC_PATH}"],
                capture_output=True, text=True, timeout=PUBLIC_TIMEOUT + 5)
            if r.stdout.strip().startswith("2"):
                return True
        except Exception:  # noqa: BLE001 — retry, then give up on this relay
            continue
    return False


def public_status(host: str = None, ips: list = None) -> dict:
    """{'ok': [ip…], 'bad': [ip…]} for every relay in public DNS."""
    host = public_host() if host is None else host
    if not host:
        return {"ok": [], "bad": []}
    ips = public_ips(host) if ips is None else ips
    out = {"ok": [], "bad": []}
    for ip in ips:
        out["ok" if _relay_serves(host, ip) else "bad"].append(ip)
    return out


def public_reachable(host: str = None, ips: list = None) -> bool:
    """True only when EVERY published relay serves the site.

    This used to accept one relay answering, on the reasoning that Tailscale
    publishes several and a single dead one is not an outage. That reasoning is
    wrong: public DNS hands the client all the A records and it picks whichever
    it likes, so one dead ingress is a coin-flip failure for real visitors, not
    a spare tyre. On 2026-08-05 one of two relays was persistently dead — the
    watchdog reported healthy while a phone off the tailnet got "cannot
    establish a secure connection" about half the time.

    Unknown (no host, no DNS) still returns True: a watchdog that alerts
    because it could not measure is worse than useless.
    """
    host = public_host() if host is None else host
    if not host:
        return True
    ips = public_ips(host) if ips is None else ips
    if not ips:
        return True
    st = public_status(host, ips)
    return not st["bad"]


def stale_due(entries: list, newest_change: float, dirty: bool,
              last_notice: float, now: float) -> bool:
    """Pure gate for the 'new code is waiting' nudge. Split out from the
    sending so the four conditions can be tested without a Telegram token."""
    if not entries:
        return False
    if dirty:                                   # mid-batch — not a release
        return False
    if newest_change and now - newest_change < STALE_QUIET_SEC:
        return False                            # still being edited
    return now - last_notice >= STALE_COOLDOWN_SEC


def stale_tick(state: dict, now: float) -> str:
    """Notice (or auto-restart) for processes running old code. Returns what
    it did, for the log — '' when there was nothing to do."""
    import restart_ctl
    if restart_ctl.in_flight(now=now):
        return ""
    entries = restart_ctl.stale()
    if not stale_due(entries, restart_ctl.newest_change_ts(entries),
                     restart_ctl.tree_dirty(), state.get("stale_notice", 0), now):
        return ""
    state["stale_notice"] = now

    import telegram_utils
    if RESTART_AUTO:
        ok, note = restart_ctl.request("watchdog auto (new code settled)")
        telegram_utils.send_message(f"🔄 偵測到新程式碼，自動重啟中\n\n{note}",
                                    force=True, channel="private")
        return f"auto-restart requested (ok={ok})"

    import tg_commands
    telegram_utils.send_message(
        tg_commands.fmt_stale(entries), force=True, channel="private",
        reply_markup=tg_commands.action_keyboard("restart"))
    return f"stale notice sent ({len(entries)} process(es))"


def autofix_allowed(attempts: list, now: float) -> bool:
    """True while we are still inside this hour's repair budget."""
    if not PUBLIC_AUTOFIX:
        return False
    recent = [t for t in attempts if now - t < PUBLIC_AUTOFIX_WINDOW_SEC]
    return len(recent) < PUBLIC_AUTOFIX_MAX


def prune_attempts(attempts: list, now: float) -> list:
    return [t for t in attempts if now - t < PUBLIC_AUTOFIX_WINDOW_SEC]


def run_autofix(spawn=None) -> bool:
    """Fire ./tailscale.sh rearm and return immediately. start_new_session so
    it is not tied to the scanner's process group — a restart of the stack
    mid-repair must not kill the repair."""
    script = os.path.join(REPO_ROOT, "tailscale.sh")
    if not os.path.isfile(script):
        print(f"[watchdog] autofix skipped — {script} not found")
        return False
    # The repair must not depend on being able to LOG the repair. A missing or
    # unwritable logs/ directory used to abort it entirely — which would leave
    # the public URL dead indefinitely, with the explanation sitting in the one
    # file that could not be opened. Same shape as the auto-heal agent that
    # failed silently for 16 days. Log if we can, run either way.
    log = subprocess.DEVNULL
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log = open(os.path.join(LOG_DIR, "tailscale_autofix.log"), "a")  # noqa: SIM115
    except OSError as exc:
        print(f"[watchdog] autofix log unavailable ({exc}) — running anyway")
    try:
        runner = spawn or subprocess.Popen
        runner(["/bin/bash", script, "rearm"], cwd=REPO_ROOT,
               stdout=log, stderr=subprocess.STDOUT,
               stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception as exc:  # noqa: BLE001 — a failed repair must not kill the sweep
        print(f"[watchdog] autofix failed to start: {exc}")
        return False


def public_alert_text(pub: dict, fix: str, *, fixing: bool = False,
                      attempts: int = 0) -> str:
    """A half-broken public URL and a dead one need different words.

    "已停止運行" for a partial outage would send the owner looking for a dead
    process while the site is up — and, worse, they would load it, get served
    by the working relay, see it fine and conclude the alert was noise. Say the
    share of visits that fail instead.

    When the repair is already running, the message says so and does NOT print
    a command: handing someone a fix that is executing right now invites them
    to run a second copy of it."""
    bad, ok = pub["bad"], pub["ok"]
    total = len(bad) + len(ok)
    tail = (f"🔧 已自動重新註冊（今天第 {attempts} 次），約 1 分鐘後生效，"
            f"好了會再通知你。" if fixing else f"修復:  {fix}")
    if ok:
        pct = round(100 * len(bad) / total)
        return (f"⚠️ 看門狗: {PUBLIC_LABEL} 只有一半在服務\n"
                f"{len(bad)}/{total} 個中繼沒有回應 ({'、'.join(bad)})。\n"
                f"瀏覽器會自己挑一個，所以大約 {pct}% 的連線會出現「無法建立安全連線」。\n"
                f"你自己開可能是好的 — 那只代表你剛好挑到活的那個。\n"
                f"{tail}")
    return (f"🚨 看門狗: {PUBLIC_LABEL} 完全連不上\n"
            f"{total} 個中繼都沒有回應。對外的網頁和 LINE webhook 現在都是斷的。\n"
            f"{tail}")


def tunnel_running(ps_output: str) -> bool:
    """Is the cloudflared quick-tunnel process alive? Its command line doesn't
    end in a bare script name like the EXPECTED python scripts (it's
    'cloudflared tunnel --url ...'), so this checks its own pattern rather
    than reusing parse_running."""
    return any("cloudflared tunnel" in line for line in ps_output.splitlines())


# ── orchestration ────────────────────────────────────────────────────────────
def _ps() -> str:
    return subprocess.run(["ps", "-ax", "-o", "command"],
                          capture_output=True, text=True, timeout=10).stdout


def tick(self_name: str) -> list:
    """Called from a scanner sweep. Returns the names alerted this call."""
    now = time.time()
    state = _load_state()
    if now - state.get("last_check", 0) < CHECK_SEC:
        return []
    state["last_check"] = now

    rotate_logs()                                # bounded logs, same 5-min cadence

    try:
        ps_text = _ps()
        running = parse_running(ps_text)
    except Exception as exc:  # noqa: BLE001 — a ps failure must not kill the sweep
        print(f"[watchdog] ps failed: {exc}")
        _save_state(state)
        return []
    running.add(self_name)                       # we are obviously alive
    down = missing(running)
    if WATCH_TUNNEL and not tunnel_running(ps_text):
        down = [*down, TUNNEL_KEY]
    # Measured ONCE and reused for the message — probing again to describe the
    # fault would double the curl round-trips and could describe a different
    # moment than the one that raised the alert.
    pub = None
    if WATCH_PUBLIC:
        _host = public_host()
        _ips = public_ips(_host) if _host else []
        if _host and _ips:                       # unknown ≠ down
            pub = public_status(_host, _ips)
            # ── CONFIRM BEFORE ACTING (2026-08-13) ──────────────────────────
            # A single failed probe used to be enough to both alert AND
            # re-arm. Both were wrong, and the re-arm was the expensive one:
            # `rearm` runs `serve reset` and takes the site down for 30-60s
            # while the relays re-register. So one flaky probe — a DNS hiccup,
            # a 15s timeout on a loaded Mac, a relay mid-rotation — triggered a
            # repair that CAUSED a real outage, and the owner got a 🚨 for it.
            # The watchdog was manufacturing a share of the events it reported.
            #
            # Two consecutive failed checks (~5 min apart) before anything
            # happens. A genuine outage is still caught within ~10 minutes and
            # still repaired unattended; a blip now costs nothing and says
            # nothing. The strictly-worse alternative is what was there before.
            strikes = int(state.get("public_strikes") or 0)
            if pub["bad"]:
                strikes += 1
                if strikes >= PUBLIC_STRIKES:
                    down = [*down, PUBLIC_KEY]
                else:
                    print(f"[watchdog] public URL failed probe {strikes}/"
                          f"{PUBLIC_STRIKES} — confirming before acting")
            else:
                strikes = 0
            state["public_strikes"] = strikes

    import telegram_utils
    alerted = []
    last_alert = state.get("last_alert") or {}
    was_down = set(state.get("down") or [])

    # Repair BEFORE alerting, so the message can say a fix is already running
    # instead of handing the owner a command we were about to run anyway.
    # Attempted on every tick the URL is down (not on the 1h alert cadence) —
    # five minutes of downtime is the target, not an hour.
    attempts = prune_attempts(state.get("public_fix") or [], now)
    fixing = False
    if PUBLIC_KEY in down and autofix_allowed(attempts, now):
        fixing = run_autofix()
        if fixing:
            attempts.append(now)
            print(f"[watchdog] public URL down — auto-rearm #{len(attempts)} started")
    state["public_fix"] = attempts

    def _label(name):
        return EXPECTED.get(name, PUBLIC_LABEL if name == PUBLIC_KEY else TUNNEL_LABEL)

    for name in down:
        if now - last_alert.get(name, 0) >= ALERT_COOLDOWN_SEC:
            last_alert[name] = now
            alerted.append(name)
            fix = (TUNNEL_FIX if name == TUNNEL_KEY else
               PUBLIC_FIX if name == PUBLIC_KEY else "./run_all.sh bg")
            # channel="private": send_message() defaults to "alerts", which
            # is a PUBLIC group topic. "restart with ./run_all.sh bg" is an
            # instruction only the owner can act on, and it advertises the
            # stack to everyone who joined via the invite link.
            telegram_utils.send_message(
                public_alert_text(pub, fix, fixing=fixing, attempts=len(attempts))
                if name == PUBLIC_KEY and pub else
                f"🚨 看門狗: {name} ({_label(name)}) 已停止運行!\n重啟:  {fix}",
                force=True, channel="private")
            print(f"[watchdog] ALERT: {name} is down")
    # Recovery is only claimable for things still being WATCHED. Turning a
    # check off drops its name from `down`, which is indistinguishable from the
    # thing coming back — and on 2026-08-03 that sent a "✅ cloudflared 已恢復
    # 運行" for a tunnel that had been retired, at the moment its check was
    # disabled. Unwatched names leave the state silently.
    watched = (set(EXPECTED) | ({TUNNEL_KEY} if WATCH_TUNNEL else set())
               | ({PUBLIC_KEY} if WATCH_PUBLIC else set()))
    for name in sorted(was_down - set(down)):
        if name not in watched:
            last_alert.pop(name, None)
            print(f"[watchdog] no longer watching {name} — dropped, not announced")
            continue
        telegram_utils.send_message(
            f"✅ 看門狗: {name} ({_label(name)}) 已恢復運行",
            force=True, channel="private")
        print(f"[watchdog] recovered: {name}")

    # New code waiting to run. Deliberately after the down/recovery pass and
    # skipped while anything is down — "S2 is dead" and "S2 is a version
    # behind" arriving together would bury the one that matters.
    if WATCH_STALE and not down:
        try:
            did = stale_tick(state, now)
            if did:
                print(f"[watchdog] {did}")
        except Exception as exc:  # noqa: BLE001 — a nudge must not kill the sweep
            print(f"[watchdog] stale check failed: {exc}")

    state["down"] = down
    state["last_alert"] = last_alert
    _save_state(state)
    return alerted
