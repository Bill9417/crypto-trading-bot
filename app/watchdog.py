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
import subprocess
import time

EXPECTED = {
    "app.py": "web dashboard",
    "bot.py": "S1 bot",
    "strategy2_scanner.py": "S2 scanner",
    "strategy3_scanner.py": "S3 scanner",
}
STATE_FILE = os.path.join(os.path.dirname(__file__), "watchdog_state.json")
CHECK_SEC = 300
ALERT_COOLDOWN_SEC = 3600


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


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

    try:
        running = parse_running(_ps())
    except Exception as exc:  # noqa: BLE001 — a ps failure must not kill the sweep
        print(f"[watchdog] ps failed: {exc}")
        _save_state(state)
        return []
    running.add(self_name)                       # we are obviously alive
    down = missing(running)

    import telegram_utils
    alerted = []
    last_alert = state.get("last_alert") or {}
    was_down = set(state.get("down") or [])

    for name in down:
        if now - last_alert.get(name, 0) >= ALERT_COOLDOWN_SEC:
            last_alert[name] = now
            alerted.append(name)
            telegram_utils.send_message(
                f"🚨 看門狗: {name} ({EXPECTED[name]}) 已停止運行!\n"
                f"重啟:  ./run_all.sh bg", force=True)
            print(f"[watchdog] ALERT: {name} is down")
    for name in sorted(was_down - set(down)):
        telegram_utils.send_message(
            f"✅ 看門狗: {name} ({EXPECTED.get(name, '?')}) 已恢復運行", force=True)
        print(f"[watchdog] recovered: {name}")

    state["down"] = down
    state["last_alert"] = last_alert
    _save_state(state)
    return alerted
