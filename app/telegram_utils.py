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


def _post_one(url: str, payload: dict, retries: int) -> bool:
    """One paced POST with 429-aware retries. Network failures get the old
    backoff retries; a 429 sleeps out retry_after and tries again; any other
    HTTP error response (bad request / blocked bot) is final."""
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
            return True
        except requests.exceptions.HTTPError:
            print(f"Telegram API Error response: {response.text}")
            return False
        except requests.RequestException as exc:
            print(f"Telegram send failed (attempt {attempt + 1}/{retries + 1}): {exc}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    return False


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
    for part in _chunks_of(message):
        if not _post_one(url, {**payload, "text": part}, retries):
            ok = False
    return ok
