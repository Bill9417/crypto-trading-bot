"""📱 LINE push — the 台股 messages, delivered to family on LINE.

LINE Notify was shut down (2025-03-31), so this uses the LINE Messaging API
through a LINE Official Account instead. The design is deliberately the
simplest thing that works for "my dad reads it on his phone":

  · GROUPS (the intended setup): invite the Official Account into the
    family LINE group → LINE calls our webhook (/line/webhook) → the group
    auto-subscribes (saved in line_ids.json), gets an instant Chinese
    welcome reply, and the owner gets a Telegram note. Leaving the group
    unsubscribes it. No IDs to copy, nothing to configure.
  · With no groups and no LINE_TO we BROADCAST — everyone who added the
    OA as a 1:1 friend gets the message. Once a group (or LINE_TO) exists,
    messages go THERE only, so nobody receives duplicates.
  · LINE_TO (comma-separated user/group IDs) forces explicit recipients.

Quota: the free plan allows 200 push messages/month, counted PER RECIPIENT.
One daily digest (~22 trading days) plus occasional SL/TP-hit alerts fits
2–3 family members comfortably — which is why only tw_stocks (the 14:00
digest) and tw_intraday level hits go to LINE, not every mover alert.

Everything here is failure-safe: a LINE outage logs and returns False,
never raises into the scanner loop. `_post` is the single network
choke-point (stubbed by the tests' autouse guard).
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime

import requests

import config

API = "https://api.line.me/v2/bot/message"
IDS_FILE = os.path.join(os.path.dirname(__file__), "line_ids.json")
MAX_LEN = 4900          # LINE text-message limit is 5000 chars
BATCH = 5               # API limit: 5 message objects per call

WELCOME_GROUP = ("✅ 已連接台股訊號！\n"
                 "每個交易日 14:00 會收到台股掃描（進場參考/停損/目標），\n"
                 "盤中觸到停損或目標也會即時提醒。")

# Service lifecycle notices — sent by the S2 scanner (the process that owns
# every 台股 message) on startup and on SIGTERM/SIGINT/crash shutdown.
START_MSG = ("✅ 台股訊號系統已啟動\n"
             "每個交易日 14:00 台股掃描、盤中停損/目標觸價提醒運作中")
STOP_MSG = ("🛑 台股訊號系統已停止\n"
            "訊息暫停發送，重新啟動後會自動通知")


def enabled() -> bool:
    return bool(config.LINE_CHANNEL_ACCESS_TOKEN
                or (config.LINE_CHANNEL_ID and config.LINE_CHANNEL_SECRET))


# ── auth ─────────────────────────────────────────────────────────────────────
# Preferred setup: only LINE_CHANNEL_ID + LINE_CHANNEL_SECRET in .env — we
# mint short-lived STATELESS channel access tokens ourselves (15 min, no
# issue-count limit, nothing to renew in the console). A console-issued
# long-lived LINE_CHANNEL_ACCESS_TOKEN, if present, is used as-is instead.
_tok_cache = {"token": "", "exp": 0.0}


def _token() -> str:
    if config.LINE_CHANNEL_ACCESS_TOKEN:
        return config.LINE_CHANNEL_ACCESS_TOKEN
    if not (config.LINE_CHANNEL_ID and config.LINE_CHANNEL_SECRET):
        return ""
    if time.time() < _tok_cache["exp"] - 60:
        return _tok_cache["token"]
    try:
        r = requests.post("https://api.line.me/oauth2/v3/token", timeout=15,
                          data={"grant_type": "client_credentials",
                                "client_id": config.LINE_CHANNEL_ID,
                                "client_secret": config.LINE_CHANNEL_SECRET})
        if r.status_code != 200:
            print(f"[line] token mint failed {r.status_code}: {r.text[:200]}")
            return ""
        d = r.json()
        _tok_cache.update(token=d["access_token"],
                          exp=time.time() + float(d.get("expires_in", 900)))
    except Exception as exc:  # noqa: BLE001 — auth outage = message skipped, not a crash
        print(f"[line] token mint error: {exc}")
        return ""
    return _tok_cache["token"]


# ── subscribed groups (webhook-captured) ─────────────────────────────────────
def _load_ids() -> dict:
    try:
        with open(IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = no subscriptions yet
        return {}


def _save_ids(ids: dict) -> None:
    tmp = IDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False)
    os.replace(tmp, IDS_FILE)


def targets() -> list:
    """Explicit LINE_TO plus every group the OA has been invited into."""
    ids = _load_ids()
    groups = [g for g, m in (ids.get("groups") or {}).items() if m.get("active")]
    return list(config.LINE_TO) + groups


# ── webhook (app.py /line/webhook) ───────────────────────────────────────────
def sig_ok(body: bytes, signature: str) -> bool:
    """X-Line-Signature check — HMAC-SHA256 of the raw body, base64."""
    secret = config.LINE_CHANNEL_SECRET
    if not secret:
        return True         # secret not configured yet — capture mode
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def handle_webhook(payload: dict) -> int:
    """Auto-subscribe groups/rooms the OA is invited into; a 'leave' event
    unsubscribes. Returns the number of subscription changes."""
    changed = 0
    for ev in (payload or {}).get("events") or []:
        src = ev.get("source") or {}
        gid = src.get("groupId") or src.get("roomId")
        etype = ev.get("type")
        if not gid:
            continue
        ids = _load_ids()
        groups = ids.setdefault("groups", {})
        known = groups.get(gid) or {}
        if etype == "leave":
            if known.get("active"):
                groups[gid] = {**known, "active": False}
                _save_ids(ids)
                changed += 1
        elif etype in ("join", "message"):
            if not known.get("active"):
                groups[gid] = {"active": True,
                               "since": datetime.now().strftime("%Y-%m-%d")}
                _save_ids(ids)
                changed += 1
                if ev.get("replyToken"):    # reply = free, no quota
                    _reply(ev["replyToken"], WELCOME_GROUP)
                _notify_owner(gid)
    return changed


def _reply(reply_token: str, text: str) -> None:
    try:
        code, body = _post("reply", {"replyToken": reply_token,
                                     "messages": [{"type": "text", "text": text}]})
        if code != 200:
            print(f"[line] reply failed {code}: {body[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[line] reply error: {exc}")


def _notify_owner(gid: str) -> None:
    """Tell the owner (on Telegram) that a LINE group just connected."""
    try:
        import telegram_utils
        telegram_utils.send_message(
            f"📱 LINE 群組已連接（{gid[:10]}…）— 台股日報＋觸價提醒將自動發送到該群組",
            force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[line] owner notify failed: {exc}")


def _post(path: str, payload: dict) -> tuple:
    """(status_code, body) — the only place that talks to LINE."""
    tok = _token()
    if not tok:
        return 0, "no token"
    r = requests.post(
        f"{API}/{path}", json=payload, timeout=15,
        headers={"Authorization": f"Bearer {tok}"})
    return r.status_code, r.text


def _chunks(text: str) -> list:
    """Split on newline boundaries so no chunk exceeds MAX_LEN."""
    text = text.strip()
    if len(text) <= MAX_LEN:
        return [text]
    out, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + 1 + len(ln) > MAX_LEN:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
        while len(cur) > MAX_LEN:          # single monster line
            out.append(cur[:MAX_LEN])
            cur = cur[MAX_LEN:]
    if cur:
        out.append(cur)
    return out


def send(text: str) -> bool:
    """Push to subscribed groups/LINE_TO; broadcast when neither exists."""
    if not enabled() or not (text or "").strip():
        return False
    msgs = [{"type": "text", "text": c} for c in _chunks(text)]
    tos = targets()
    ok = True
    for i in range(0, len(msgs), BATCH):
        batch = msgs[i:i + BATCH]
        for to in (tos or [None]):
            path = "broadcast" if to is None else "push"
            payload = {"messages": batch} if to is None else {"to": to, "messages": batch}
            try:
                code, body = _post(path, payload)
                if code == 429:            # monthly quota is 4xx too — retry once only helps rate limits
                    time.sleep(2)
                    code, body = _post(path, payload)
                if code != 200:
                    print(f"[line] {path} failed {code}: {body[:200]}")
                    ok = False
            except Exception as exc:  # noqa: BLE001 — never kill the scanner
                print(f"[line] {path} error: {exc}")
                ok = False
    return ok
