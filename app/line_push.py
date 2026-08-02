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
import re
import time
from datetime import datetime

import requests

import config

API = "https://api.line.me/v2/bot/message"
IDS_FILE = os.path.join(os.path.dirname(__file__), "line_ids.json")
MAX_LEN = 4900          # LINE text-message limit is 5000 chars
BATCH = 5               # API limit: 5 message objects per call

WELCOME_GROUP = ("✅ 已連接台股訊號！\n"
                 "每個交易日 08:00 會收到美股收盤摘要（開盤前參考），\n"
                 "14:00 收到台股掃描（進場參考/停損/目標），\n"
                 "盤中觸到停損或目標也會即時提醒。\n"
                 "輸入「說明」可查看指令。")

HELP_MSG = ("📖 台股天地指令：\n"
            "訊號 — 今日台股掃描結果\n"
            "美股 — 昨夜美股收盤摘要\n"
            "現況 — 即時大盤與追蹤個股\n"
            "期貨 — 台指期趨勢與關鍵價位\n"
            "網址 — 網站儀表板連結\n"
            "說明 — 顯示本說明")

# Service lifecycle notices — sent by the S2 scanner (the process that owns
# every 台股 message) on startup and on SIGTERM/SIGINT/crash shutdown.
START_MSG = ("✅ 台股訊號系統已啟動\n"
             "每個交易日 14:00 台股掃描、盤中停損/目標觸價提醒運作中")
STOP_MSG = ("🛑 台股訊號系統已停止\n"
            "訊息暫停發送，重新啟動後會自動通知")

# 👆 Tap-instead-of-type buttons. LINE has no group-chat equivalent of a
# persistent Rich Menu (that feature only attaches to 1:1 chats with the OA —
# there is no API to bind one to a group/room ID), so this is the group-
# compatible substitute: Quick Reply buttons attached to every outgoing
# message, which DO render in group chats and reappear on each new bot
# message. Max 13 items; each label ≤20 chars.
QUICK_REPLY = {"items": [
    {"type": "action", "action": {"type": "message", "label": "📊 訊號", "text": "訊號"}},
    {"type": "action", "action": {"type": "message", "label": "🇺🇸 美股", "text": "美股"}},
    {"type": "action", "action": {"type": "message", "label": "📈 現況", "text": "現況"}},
    {"type": "action", "action": {"type": "message", "label": "📉 期貨", "text": "期貨"}},
    {"type": "action", "action": {"type": "message", "label": "🔗 網址", "text": "網址"}},
    {"type": "action", "action": {"type": "message", "label": "❓ 說明", "text": "說明"}},
]}


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
    """Auto-subscribe groups/rooms the OA is invited into ('leave'
    unsubscribes) and answer text commands — 訊號/現況/說明 — with free
    reply messages (replies never count against the monthly push quota).
    Returns the number of subscription changes."""
    changed = 0
    for ev in (payload or {}).get("events") or []:
        src = ev.get("source") or {}
        gid = src.get("groupId") or src.get("roomId")
        etype = ev.get("type")
        token = ev.get("replyToken")
        replied = False
        if gid:
            ids = _load_ids()
            groups = ids.setdefault("groups", {})
            known = groups.get(gid) or {}
            # join = explicit (re)invite → (re)activate; a message also
            # subscribes a never-seen group (backfills groups joined before
            # the webhook went live).
            if etype == "leave":
                if known.get("active"):
                    groups[gid] = {**known, "active": False}
                    _save_ids(ids)
                    changed += 1
            elif (etype == "join" and not known.get("active")) or \
                 (etype == "message" and gid not in groups):
                groups[gid] = {"active": True,
                               "since": datetime.now().strftime("%Y-%m-%d")}
                _save_ids(ids)
                changed += 1
                if token:                   # the one replyToken goes to the welcome
                    _reply(token, WELCOME_GROUP)
                    replied = True
                _notify_owner(gid)
        if etype == "message" and token and not replied:
            msg = ev.get("message") or {}
            if msg.get("type") == "text":
                resp = _command_reply(msg.get("text", ""))
                if resp:
                    _reply(token, resp)
    return changed


def _command_reply(text: str):
    """Response for a recognised command, else None (bots in groups see every
    message — only exact keywords answer, anything else stays silent)."""
    t = (text or "").strip().lower()
    if t in ("訊號", "台股", "tw"):
        try:
            import tw_stocks
            return (tw_stocks._load_state().get("last_digest_plain")
                    or "今日還沒有台股掃描 — 每個交易日 14:00 後更新。")
        except Exception as exc:  # noqa: BLE001 — a broken command stays silent
            print(f"[line] 訊號 command failed: {exc}")
            return None
    if t in ("美股", "美國", "us"):
        try:
            import us_market
            return (us_market._load_state().get("last_plain")
                    or "今天還沒有美股收盤摘要 — 每個交易日 08:00 前後更新。")
        except Exception as exc:  # noqa: BLE001 — a broken command stays silent
            print(f"[line] 美股 command failed: {exc}")
            return None
    if t in ("現況", "即時", "now"):
        try:
            import tw_intraday
            return tw_intraday.snapshot_plain()
        except Exception as exc:  # noqa: BLE001
            print(f"[line] 現況 command failed: {exc}")
            return None
    if t in ("期貨", "台指", "futures"):
        try:
            import tw_intraday
            return tw_intraday.taifex_plain()
        except Exception as exc:  # noqa: BLE001
            print(f"[line] 期貨 command failed: {exc}")
            return None
    if t in ("網址", "連結", "link"):
        try:
            import site_link
            return site_link.link_reply()
        except Exception as exc:  # noqa: BLE001
            print(f"[line] 網址 command failed: {exc}")
            return None
    if t in ("說明", "幫助", "指令", "help"):
        return HELP_MSG
    return None


def _reply(reply_token: str, text: str) -> None:
    try:
        code, body = _post("reply", {"replyToken": reply_token,
                                     "messages": [{"type": "text", "text": text,
                                                  "quickReply": QUICK_REPLY}]})
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


def _put(url: str, payload: dict) -> tuple:
    """PUT choke-point (webhook-endpoint registration; stubbed in tests)."""
    tok = _token()
    if not tok:
        return 0, "no token"
    r = requests.put(url, json=payload, timeout=15,
                     headers={"Authorization": f"Bearer {tok}"})
    return r.status_code, r.text


def _get(url: str) -> tuple:
    """GET choke-point (quota checks; stubbed in tests)."""
    tok = _token()
    if not tok:
        return 0, "no token"
    r = requests.get(url, timeout=15,
                     headers={"Authorization": f"Bearer {tok}"})
    return r.status_code, r.text


# ── push-quota guard ─────────────────────────────────────────────────────────
# The free plan's 200 pushes/month run out SILENTLY — dad's messages just stop
# and nothing errors. Once a day the scanner compares consumption against the
# plan limit and warns the owner on Telegram past QUOTA_WARN_RATIO. The check
# result is persisted so /health (a SEPARATE web process) can display it
# without making its own live LINE API calls on every page poll.
QUOTA_WARN_RATIO = 0.8
QUOTA_STATE_FILE = os.path.join(os.path.dirname(__file__), "line_quota_state.json")
_quota_state = {"date": ""}


def quota_status():
    """{'limit': int|None, 'used': int} from LINE's quota APIs, or None."""
    try:
        code_q, body_q = _get("https://api.line.me/v2/bot/message/quota")
        code_c, body_c = _get("https://api.line.me/v2/bot/message/quota/consumption")
        if code_q != 200 or code_c != 200:
            return None
        q, c = json.loads(body_q), json.loads(body_c)
        limit = q.get("value") if q.get("type") == "limited" else None
        return {"limit": limit, "used": int(c.get("totalUsage") or 0)}
    except Exception as exc:  # noqa: BLE001 — quota check must never break a sweep
        print(f"[line] quota check error: {exc}")
        return None


def quota_cached() -> dict:
    """Last quota_tick() result, read from disk — safe for a page poll to call
    (no network). {} when never checked or checked before enabled()."""
    try:
        with open(QUOTA_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = never checked yet
        return {}


def quota_tick() -> bool:
    """Once per local day: persist the current quota reading (for /health) and
    warn the owner on Telegram once past QUOTA_WARN_RATIO. Returns True when a
    check actually ran."""
    if not enabled():
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    if _quota_state["date"] == today:
        return False
    _quota_state["date"] = today
    q = quota_status()
    if not q:
        return True
    tmp = QUOTA_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({**q, "checked_at": time.time()}, f)
    os.replace(tmp, QUOTA_STATE_FILE)
    if q.get("limit") and q["used"] >= q["limit"] * QUOTA_WARN_RATIO:
        print(f"[line] quota warning: {q['used']}/{q['limit']} used")
        try:
            import telegram_utils
            telegram_utils.send_message(
                f"📱 LINE 推播額度警告:本月已用 {q['used']}/{q['limit']} 則。"
                f"額度用完後推播會靜默停止(群組指令的免費回覆不受影響)— "
                f"下月 1 號自動重置。",
                force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[line] quota warn send failed: {exc}")
    return True


# ── webhook endpoint auto-registration ───────────────────────────────────────
# Cloudflare QUICK tunnels mint a brand-new hostname every time cloudflared
# restarts (e.g. after a reboot). Instead of asking anyone to re-paste the
# URL into the LINE console, the scanner re-registers the endpoint through
# LINE's API whenever the tunnel URL changes. Manual override: LINE_WEBHOOK_BASE.
TUNNEL_LOG = os.path.join(os.path.dirname(__file__), "..", "cloudflared.log")
_synced = {"url": ""}


def current_tunnel_url() -> str:
    """Newest trycloudflare URL in the tunnel log ('' when absent)."""
    try:
        with open(TUNNEL_LOG, "r", encoding="utf-8", errors="ignore") as f:
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", f.read())
        return urls[-1] if urls else ""
    except Exception:  # noqa: BLE001 — no tunnel = nothing to register
        return ""


def sync_webhook() -> bool:
    """Point LINE's webhook at the current public URL; no-op until it changes."""
    if not enabled():
        return False
    base = (os.getenv("PUBLIC_BASE_URL", "").strip()
            or os.getenv("LINE_WEBHOOK_BASE", "").strip()
            or current_tunnel_url())
    if not base:
        return False
    url = base.rstrip("/") + "/line/webhook"
    if _synced["url"] == url:
        return True
    try:
        code, body = _put("https://api.line.me/v2/bot/channel/webhook/endpoint",
                          {"endpoint": url})
        if code == 200:
            _synced["url"] = url
            print(f"[line] webhook endpoint → {url}")
            return True
        print(f"[line] webhook sync failed {code}: {body[:200]}")
    except Exception as exc:  # noqa: BLE001 — never kill the scanner
        print(f"[line] webhook sync error: {exc}")
    return False


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


def _push_messages(msgs: list) -> bool:
    """Push pre-built LINE message objects to subscribed groups/LINE_TO,
    broadcasting when neither exists. Shared by send() (plain text) and the
    Flex-card senders — the only difference is who builds the message dicts."""
    if not enabled() or not msgs:
        return False
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


def send(text: str) -> bool:
    """Push to subscribed groups/LINE_TO; broadcast when neither exists.
    Quick-reply buttons ride on the LAST chunk (LINE shows whichever message
    the user is currently viewing — putting it there keeps it visible)."""
    if not (text or "").strip():
        return False
    msgs = [{"type": "text", "text": c} for c in _chunks(text)]
    if msgs:
        msgs[-1] = {**msgs[-1], "quickReply": QUICK_REPLY}
    return _push_messages(msgs)


def send_lifecycle(text: str) -> bool:
    """開機/關機通知 — gated by LINE_LIFECYCLE_NOTICE, which defaults to OFF.

    Kept separate from send() because these two messages fire on EVERY
    ./run_all.sh restart. While iterating that is pure noise in 爸爸's group,
    and each one spends a push from the 200/month quota to say nothing he
    asked for. Set LINE_LIFECYCLE_NOTICE=true in app/.env to turn them back on.

    Deliberately does NOT gate webhook re-registration: sync_webhook() must
    still run on every start or LINE keeps posting to the previous
    quick-tunnel URL, which dies with the old process. Silencing a notice
    should never cost you inbound messages.
    """
    if not config.LINE_LIFECYCLE_NOTICE:
        return False
    return send(text)


# ── Flex Message cards — 進場/停損/目標 as a tappable carousel ────────────────
# Flex Messages render fine in group chats (unlike Rich Menus). Used only for
# the "here are today's picks" case: a multi-stock list is where a card beats
# a wall of text. A 觀望/no-signal day stays plain text — one administrative
# line doesn't need a card, and a fallback is simpler to reason about than a
# card with nothing in it.
def _stock_bubble(code: str, name: str, s: dict) -> dict:
    import tw_stocks
    px = tw_stocks._px
    risk = (s["ref"] - s["sl"]) / s["ref"] * 100
    gain = (s["tp"] - s["ref"]) / s["ref"] * 100

    def _row(label, value, color):
        return {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#8a99ad", "flex": 2},
            {"type": "text", "text": value, "size": "sm", "color": color, "flex": 5,
             "weight": "bold", "align": "end"},
        ]}

    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "horizontal", "paddingAll": "12px",
                   "backgroundColor": "#1c1c28", "contents": [
                       {"type": "text", "text": code, "weight": "bold", "size": "lg",
                        "color": "#ffffff"},
                       {"type": "text", "text": name, "size": "sm", "color": "#8a99ad",
                        "align": "end", "gravity": "center"},
                   ]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
                 "contents": [
                     _row("進場", px(s["ref"]), "#e6e6e6"),
                     _row("停損", f"{px(s['sl'])} (-{risk:.1f}%)", "#f23645"),
                     _row("目標", f"{px(s['tp'])} (+{gain:.1f}%)", "#26a69a"),
                 ]},
    }


def build_digest_flex(now, setups: list) -> dict:
    """setups: [(code, name, setup-dict), ...] — same shape tw_stocks.tick()
    already builds for the plain-text digest. Capped at MAX_SHOW, matching
    the text version so the two never disagree on how many stocks 'today's
    picks' means."""
    import tw_stocks
    bubbles = [_stock_bubble(code, name, s) for code, name, s in setups[:tw_stocks.MAX_SHOW]]
    alt = f"🇹🇼 台股掃描 {now.strftime('%m-%d')}：今日訊號 {len(setups)} 檔"
    return {"type": "flex", "altText": alt,
            "contents": {"type": "carousel", "contents": bubbles},
            "quickReply": QUICK_REPLY}


def send_tw_digest(now, reg: dict, setups: list, plain_fallback: str) -> bool:
    """The daily 14:00 digest: a Flex carousel when today has actionable
    picks, else the plain-text 觀望/no-signal message. Falls back to plain
    text if the flex push itself fails, so a malformed card never means dad
    gets nothing."""
    if not enabled():
        return False
    if reg.get("ok") and setups:
        if _push_messages([build_digest_flex(now, setups)]):
            return True
        print("[line] digest flex send failed — falling back to plain text")
    return send(plain_fallback)
