import json
import os
import threading
import time

import requests

from config import (
    ALERTS_BOT_TOKEN,
    ALERTS_CHAT_ID,
    BOT_TOKEN,
    CHAT_ID,
    TELEGRAM_ALERTS_THREAD_ID,
    TELEGRAM_EVENTS_THREAD_ID,
    TELEGRAM_GROUP_CHAT_ID,
    TELEGRAM_LIQ_THREAD_ID,
    TELEGRAM_QUIET,
    TELEGRAM_REPORT_THREAD_ID,
    TELEGRAM_SIGNALS_THREAD_ID,
    TELEGRAM_TECH_THREAD_ID,
    TELEGRAM_TWSTOCKS_THREAD_ID,
)

_TOPIC_THREAD = {
    "signals": TELEGRAM_SIGNALS_THREAD_ID,
    "alerts": TELEGRAM_ALERTS_THREAD_ID,
    # 🌍 event radar / 💻 tech digest / 📈 daily report / 🇹🇼 台股 / 💥 清算 —
    # each falls back to the Alerts thread until its own topic exists
    "events": TELEGRAM_EVENTS_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "tech": TELEGRAM_TECH_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "report": TELEGRAM_REPORT_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "twstocks": TELEGRAM_TWSTOCKS_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "liq": TELEGRAM_LIQ_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
}

# Telegram hard limits (observed live 2026-07-12): ~20 messages/minute to the
# same group → HTTP 429 with retry_after, and 4096 chars per message → HTTP
# 400 "message is too long". Both used to LOSE messages silently (an 80-signal
# digest vanished; a burst of HC alerts was dropped). So every send now goes
# through: (1) a process-wide pacer that spaces sends ≥ MIN_GAP_SEC apart,
# (2) automatic chunking of long messages on line boundaries, and (3) a
# retry that honours 429's retry_after.
MIN_GAP_SEC = 3.05
MAX_LEN = 4096
CHUNK_LEN = 3900          # headroom under MAX_LEN for safety
MAX_429_WAIT = 35.0

_send_lock = threading.Lock()
_last_send_ts = 0.0

# ── sent-message ledger (for /clean) ────────────────────────────────────────
# Bots can't read chat history, so deleting old messages is only possible for
# messages whose ids we recorded ourselves. Every successful send below (and
# every command reply / incoming command seen by tg_commands) is appended
# here. Telegram refuses deletion of anything older than 48h, so the ledger
# trims itself past LEDGER_RETAIN_SEC.
SENT_LOG = os.path.join(os.path.dirname(__file__), "tg_sent_log.json")
LEDGER_RETAIN_SEC = 72 * 3600
DELETE_MAX_AGE_H = 48.0          # hard Telegram limit — older is undeletable
_sent_lock = threading.Lock()


def _load_sent() -> list:
    try:
        with open(SENT_LOG, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:  # noqa: BLE001 — missing/corrupt ledger = start fresh
        return []


def _save_sent(entries: list) -> None:
    tmp = SENT_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    os.replace(tmp, SENT_LOG)


def _record_sent(chat_id, message_id, bot: str = "main", ts: float = None) -> None:
    """Remember a message we produced so /clean can delete it later."""
    if not (chat_id and message_id):
        return
    now = ts or time.time()
    with _sent_lock:
        entries = [e for e in _load_sent() if now - e.get("ts", 0) < LEDGER_RETAIN_SEC]
        entries.append({"ts": now, "chat": chat_id, "mid": message_id, "bot": bot})
        _save_sent(entries)


def _delete_one(token: str, chat_id, message_id) -> bool:
    """deleteMessage with one 429-aware retry. A 400 ('message to delete not
    found' / already gone / no rights) is treated as gone — retrying is
    pointless, the ledger entry should be dropped either way."""
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    for attempt in (0, 1):
        try:
            r = requests.post(url, data={"chat_id": chat_id,
                                         "message_id": message_id}, timeout=15)
        except requests.RequestException:
            return False                      # network blip — retry next /clean
        if r.status_code == 429 and attempt == 0:
            try:
                wait = float((r.json().get("parameters") or {})
                             .get("retry_after") or 3)
            except Exception:  # noqa: BLE001
                wait = 3.0
            time.sleep(min(wait + 0.5, MAX_429_WAIT))
            continue
        # 200 = deleted; 400 = already gone / undeletable → drop either way.
        # 5xx = Telegram hiccup → keep the entry and retry on the next /clean.
        return r.ok or r.status_code == 400
    return False


def deep_clean(chat_id, upto_mid: int, limit: int = 3000) -> dict:
    """Pre-ledger backfill sweep. Bots can't LIST chat history, but group
    message ids are sequential — so try deleteMessage on every id below the
    first ledger-recorded one (newest first, capped at `limit` ids). Deletes
    whatever Telegram permits: anything younger than 48h the bot may remove;
    ids that are already gone, service messages, or past the 48h wall are
    skipped. Ages are unknowable without history access, so this is
    all-or-nothing by design — use it once to clear the pre-ledger era."""
    deleted = skipped = 0
    start = max(1, int(upto_mid) - limit)
    for mid in range(int(upto_mid) - 1, start - 1, -1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                data={"chat_id": chat_id, "message_id": mid}, timeout=15)
            if r.status_code == 429:
                try:
                    wait = float((r.json().get("parameters") or {})
                                 .get("retry_after") or 3)
                except Exception:  # noqa: BLE001
                    wait = 3.0
                time.sleep(min(wait + 0.5, MAX_429_WAIT))
                r = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                    data={"chat_id": chat_id, "message_id": mid}, timeout=15)
        except requests.RequestException:
            skipped += 1
            continue
        if r.ok:
            deleted += 1
        else:
            skipped += 1
        time.sleep(0.05)
    return {"deleted": deleted, "skipped": skipped,
            "tried": int(upto_mid) - start}


def clean_old_messages(max_age_hours: float = 24.0) -> dict:
    """Delete every recorded message older than max_age_hours. Returns
    {"deleted", "too_old" (>48h, Telegram forbids), "kept", "failed"}."""
    now = time.time()
    with _sent_lock:
        entries = _load_sent()
    keep, deleted, too_old, failed = [], 0, 0, 0
    for e in entries:
        age_h = (now - e.get("ts", 0)) / 3600
        if age_h < max_age_hours:
            keep.append(e)
            continue
        if age_h >= DELETE_MAX_AGE_H:
            too_old += 1                      # undeletable forever — drop
            continue
        token = ALERTS_BOT_TOKEN if e.get("bot") == "alerts" else BOT_TOKEN
        if _delete_one(token, e.get("chat"), e.get("mid")):
            deleted += 1
        else:
            failed += 1
            keep.append(e)                    # network blip — retry next time
        time.sleep(0.1)                       # deleteMessage is rate-limited too
    with _sent_lock:
        _save_sent(keep)
    return {"deleted": deleted, "too_old": too_old, "kept": len(keep),
            "failed": failed}


def _pace() -> None:
    """Space sends MIN_GAP_SEC apart across ALL threads of this process.
    Sleeping inside the lock is intentional — concurrent senders must queue."""
    global _last_send_ts
    with _send_lock:
        wait = _last_send_ts + MIN_GAP_SEC - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_send_ts = time.time()


def _chunks_of(message: str) -> list:
    """Split an over-limit message into ≤CHUNK_LEN parts on line boundaries."""
    if len(message) <= MAX_LEN:
        return [message]
    parts, cur = [], ""
    for line in message.split("\n"):
        while len(line) > CHUNK_LEN:              # a single monster line
            parts.append(line[:CHUNK_LEN])
            line = line[CHUNK_LEN:]
        if cur and len(cur) + 1 + len(line) > CHUNK_LEN:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def _post_one(url: str, payload: dict, retries: int):
    """One paced POST with 429-aware retries. Returns (ok, message_id) —
    message_id may be None when the response body wasn't parseable. Network
    failures get the old backoff retries; a 429 sleeps out retry_after and
    tries again; any other HTTP error (bad request / blocked bot) is final."""
    for attempt in range(retries + 1):
        _pace()
        try:
            response = requests.post(url, data=payload, timeout=15)
            if response.status_code == 429:
                try:
                    wait = float((response.json().get("parameters") or {})
                                 .get("retry_after") or 5)
                except Exception:  # noqa: BLE001 — unparseable 429 body
                    wait = 5.0
                wait = min(wait + 0.5, MAX_429_WAIT)
                print(f"Telegram rate-limited (429): waiting {wait:.0f}s "
                      f"(attempt {attempt + 1}/{retries + 1})")
                if attempt < retries:
                    time.sleep(wait)
                continue
            response.raise_for_status()
            try:
                mid = (response.json().get("result") or {}).get("message_id")
            except Exception:  # noqa: BLE001 — sent fine, id just unknown
                mid = None
            return True, mid
        except requests.exceptions.HTTPError:
            print(f"Telegram API Error response: {response.text}")
            return False, None
        except requests.RequestException as exc:
            print(f"Telegram send failed (attempt {attempt + 1}/{retries + 1}): {exc}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    return False, None


def send_message(message, parse_mode=None, *, force=False, retries=2, channel="alerts"):
    """channel="signals" (RSI extremes + good-entry-chance cards) vs channel=
    "alerts" (everything else the bot sends automatically). Three ways this can
    be delivered, tried in order:
      1. Topics group (TELEGRAM_GROUP_CHAT_ID + a thread id for this channel) —
         one bot, one group, each channel its own topic thread. Always sends.
      2. Two-bot split (ALERTS_BOT_TOKEN/CHAT_ID) — "alerts" goes to the
         dedicated second bot. Always sends.
      3. Fallback — everything goes to the original bot; "alerts" still obeys
         the old TELEGRAM_QUIET/force rule so a fresh checkout with none of
         the above configured behaves exactly as it always has.

    Long messages are split on line boundaries and sent as in-order chunks;
    returns True only when EVERY chunk was delivered.
    """
    thread_id = _TOPIC_THREAD.get(channel)
    payload = {}

    if TELEGRAM_GROUP_CHAT_ID and thread_id:
        token, payload["chat_id"], payload["message_thread_id"] = (
            BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID, thread_id,
        )
    elif channel != "signals" and ALERTS_BOT_TOKEN and ALERTS_CHAT_ID:
        token, payload["chat_id"] = ALERTS_BOT_TOKEN, ALERTS_CHAT_ID
    else:
        token, payload["chat_id"] = BOT_TOKEN, CHAT_ID
        if channel != "signals" and TELEGRAM_QUIET and not force:
            return False

    if not token or not payload["chat_id"]:
        print(f"Telegram disabled ({channel}): missing bot token or chat id.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if parse_mode:
        payload["parse_mode"] = parse_mode

    ok = True
    bot = "main" if token == BOT_TOKEN else "alerts"
    for part in _chunks_of(message):
        sent, mid = _post_one(url, {**payload, "text": part}, retries)
        if sent and mid:
            _record_sent(payload["chat_id"], mid, bot)   # /clean can find it later
        if not sent:
            ok = False
    return ok
