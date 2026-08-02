"""🔗 Site link — the dashboard URL, auto-announced when it changes.

Cloudflare quick tunnels mint a brand-new hostname every time cloudflared
restarts (e.g. after a reboot), so every saved link in the family's chats
dies. Each S2 sweep compares the newest tunnel URL against what was last
announced per channel; a change pushes the fresh link to the Telegram group
and the LINE group, and the /link (TG) · 網址 (LINE) commands always answer
with the current one. Per-channel state means a LINE outage retries next
sweep without re-spamming Telegram.
"""
import json
import os

import line_push

STATE_FILE = os.path.join(os.path.dirname(__file__), "site_link_state.json")


def stable_url() -> str:
    """The permanent link, when one is configured (Tailscale Funnel etc.).

    PUBLIC_BASE_URL is the modern setting; LINE_WEBHOOK_BASE is kept because
    it already did this job for the LINE webhook before there was a general
    name for it.
    """
    base = (os.getenv("PUBLIC_BASE_URL", "").strip()
            or os.getenv("LINE_WEBHOOK_BASE", "").strip())
    return base.rstrip("/") if base else ""


def is_stable() -> bool:
    """True when the link cannot change on reboot, so the messaging should
    stop promising to announce a new one."""
    return bool(stable_url())


def current_url() -> str:
    """The public dashboard base URL ('' when nothing is exposed)."""
    base = stable_url() or line_push.current_tunnel_url()
    return base.rstrip("/") if base else ""


def announce_text(url: str) -> str:
    if is_stable():
        # A permanent link is announced once; promising future updates here
        # would be a lie people plan around.
        return ("🔗 網站連結\n"
                f"{url}\n"
                "(這是固定網址，重開機也不會變，請放心收藏)")
    return ("🔗 網站連結已更新\n"
            f"{url}\n"
            "(電腦重啟後連結會更換，新連結都會自動發到這裡；"
            "也可隨時輸入指令查詢最新連結)")


def link_reply() -> str:
    """The /link · 網址 command answer."""
    url = current_url()
    if not url:
        return "目前偵測不到網站連結（通道尚未啟動）— 啟動後會自動公告。"
    if is_stable():
        return f"🔗 網站連結：\n{url}\n(固定網址，重開機也不會變)"
    return f"🔗 網站連結：\n{url}\n(電腦重啟後會更換，更換時自動公告新連結)"


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = first announce
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def tick() -> bool:
    """Announce a changed URL to each channel that hasn't seen it yet."""
    url = current_url()
    if not url:
        return False
    state = _load_state()
    changed = False
    if state.get("tg") != url:
        import telegram_utils
        if telegram_utils.send_message(announce_text(url), force=True):
            state["tg"] = url
            changed = True
    if line_push.enabled() and state.get("line") != url:
        if line_push.send(announce_text(url)):
            state["line"] = url
            changed = True
    if changed:
        _save_state(state)
        print(f"[sitelink] announced {url}")
    return changed
