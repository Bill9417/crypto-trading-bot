import json
import os
import re
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
    TELEGRAM_TRADES_CHAT_ID,
    TELEGRAM_TRADES_THREAD_ID,
    TELEGRAM_TWSTOCKS_THREAD_ID,
)

# Channels that must NEVER reach the joinable group: the owner's DM and the
# private S1/S3/S4 trade feed. Kept as one set so a new private channel cannot
# be added to the routing table without landing in the guard test too.
#
# "s1signals" is RETIRED — its public topic (thread 4877) and every trade card
# in it were deleted 2026-08-08 at the owner's request. It stays here, aliased
# to the private feed, rather than being dropped: an unmapped channel name
# falls through _route() to the group's Alerts topic, so simply deleting the
# entry would turn any leftover reference into a public post. Failing closed
# beats tidiness. config.TELEGRAM_S1SIGNALS_THREAD_ID is now ignored.
PRIVATE_CHANNELS = frozenset({"private", "trades", "s1signals"})

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


# ── auto-clean (scheduled /clean) ────────────────────────────────────────────
# Telegram forbids deleting messages older than 48h, so waiting for a manual
# /clean risks messages ageing past the wall forever. Once per day at a quiet
# hour the scanner sweeps the >24h ledger automatically.
AUTO_CLEAN = os.getenv("TG_AUTO_CLEAN", "true").strip().lower() in ("1", "true", "yes", "on")
AUTO_CLEAN_HOUR = int(os.getenv("TG_AUTO_CLEAN_HOUR", "4"))
_AC_STATE = os.path.join(os.path.dirname(__file__), "tg_autoclean_state.json")


# A daily auto-clean can delete hundreds of messages at ~0.1s each — 90s+ of
# blocking. Run it OFF the scanner sweep so a clean day doesn't stall every
# other per-sweep task (movers, liq, whale…) behind it. The non-blocking lock
# guarantees at most one clean thread at a time regardless of the daily gate.
_ac_thread_lock = threading.Lock()


def _run_clean_async(hours: float) -> None:
    if not _ac_thread_lock.acquire(blocking=False):
        return                                # a previous sweep is still running
    try:
        print(f"[tg] auto-clean: {clean_old_messages(hours)}")
    finally:
        _ac_thread_lock.release()


def auto_clean_tick(now=None) -> bool:
    """Scanner hook: kick off one clean_old_messages(24) per local day after
    AUTO_CLEAN_HOUR, in a BACKGROUND thread so the sweep never blocks on it.
    Returns True when a clean was kicked off this call (not when it finished)."""
    if not (AUTO_CLEAN and TELEGRAM_GROUP_CHAT_ID):
        return False
    from datetime import datetime
    now = now or datetime.now()
    if now.hour < AUTO_CLEAN_HOUR:
        return False
    today = now.strftime("%Y-%m-%d")
    try:
        with open(_AC_STATE, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:  # noqa: BLE001
        state = {}
    if state.get("last") == today:
        return False
    # Mark done BEFORE spawning: the delete sweep runs asynchronously, so the
    # next scanner sweep must not re-trigger it while it's mid-flight. If the
    # clean is interrupted, its un-deleted entries stay in the ledger and are
    # picked up tomorrow — no data loss, just a delay.
    state["last"] = today
    tmp = _AC_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _AC_STATE)
    threading.Thread(target=_run_clean_async, args=(24,),
                     name="tg-autoclean", daemon=True).start()
    return True


def clean_old_messages(max_age_hours: float = 24.0) -> dict:
    """Delete every recorded message older than max_age_hours. Returns
    {"deleted", "too_old" (>48h, Telegram forbids), "kept", "failed"}.

    The ledger is re-merged under the lock at the end (drop only the ids we
    resolved, keep everything else) instead of overwriting with a stale
    snapshot — so messages sent DURING the ~90s delete sweep by other threads
    aren't clobbered out of the ledger and stay deletable later."""
    now = time.time()
    with _sent_lock:
        entries = _load_sent()
    deleted = too_old = failed = 0
    resolved = set()                          # (chat, mid) to drop from the ledger
    for e in entries:
        age_h = (now - e.get("ts", 0)) / 3600
        if age_h < max_age_hours:
            continue
        key = (e.get("chat"), e.get("mid"))
        if age_h >= DELETE_MAX_AGE_H:
            too_old += 1                      # undeletable forever — drop
            resolved.add(key)
            continue
        token = ALERTS_BOT_TOKEN if e.get("bot") == "alerts" else BOT_TOKEN
        if _delete_one(token, e.get("chat"), e.get("mid")):
            deleted += 1
            resolved.add(key)
        else:
            failed += 1                       # network blip — leave it, retry next time
        time.sleep(0.1)                       # deleteMessage is rate-limited too
    with _sent_lock:
        kept = [e for e in _load_sent()
                if (e.get("chat"), e.get("mid")) not in resolved]
        _save_sent(kept)
    return {"deleted": deleted, "too_old": too_old, "kept": len(kept),
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


def balance_pre(parts: list) -> list:
    """When an HTML message is CHUNKED, a split inside a <pre> block leaves an
    unclosed tag in one part and a stray closer in the next — Telegram 400s
    BOTH chunks and the whole message is lost. Close the open block at each
    chunk end and reopen it at the next chunk start."""
    out, reopen = [], False
    for p in parts:
        if reopen:
            p = "<pre>" + p
        reopen = p.count("<pre>") > p.count("</pre>")
        if reopen:
            p = p + "</pre>"
        out.append(p)
    return out


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


def _route(channel: str, force: bool):
    """Resolve (token, base_payload, bot_label) for a channel, or None when
    disabled. The three-tier routing (topics group → two-bot split →
    fallback) lives in exactly this one place — send_message() and
    send_message_and_pin() both call it, so a pin never targets the wrong
    chat_id/token just because routing fell through to a different tier.

      1. Topics group (TELEGRAM_GROUP_CHAT_ID + a thread id for this channel) —
         one bot, one group, each channel its own topic thread. Always sends.
      2. Two-bot split (ALERTS_BOT_TOKEN/CHAT_ID) — "alerts" goes to the
         dedicated second bot. Always sends.
      3. Fallback — everything goes to the original bot; "alerts" still obeys
         the old TELEGRAM_QUIET/force rule so a fresh checkout with none of
         the above configured behaves exactly as it always has.

    PRIVATE_CHANNELS bypass all three tiers:
      "private" — the owner's DM (TELEGRAM_CHAT_ID). Account balances and P&L
        (the daily report) are the owner's business, not the group's.
      "trades"  — the S1/S3/S4 feed. TELEGRAM_TRADES_CHAT_ID (+ thread) when
        the owner has made a private group, else the same DM. A topic is only
        as private as its group, so pointing it at the JOINABLE group is
        refused here rather than quietly publishing every trade.
    """
    thread_id = _TOPIC_THREAD.get(channel)
    payload = {}

    if channel in PRIVATE_CHANNELS:
        token, payload["chat_id"] = BOT_TOKEN, CHAT_ID
        dest = TELEGRAM_TRADES_CHAT_ID if channel == "trades" else ""
        if dest and TELEGRAM_GROUP_CHAT_ID and str(dest) == str(TELEGRAM_GROUP_CHAT_ID):
            print("Telegram: TELEGRAM_TRADES_CHAT_ID points at the joinable "
                  "group — refusing, sending to the owner's DM instead.")
        elif dest:
            payload["chat_id"] = dest
            if TELEGRAM_TRADES_THREAD_ID:
                payload["message_thread_id"] = TELEGRAM_TRADES_THREAD_ID
    elif TELEGRAM_GROUP_CHAT_ID and thread_id:
        token, payload["chat_id"], payload["message_thread_id"] = (
            BOT_TOKEN, TELEGRAM_GROUP_CHAT_ID, thread_id,
        )
    elif channel != "signals" and ALERTS_BOT_TOKEN and ALERTS_CHAT_ID:
        token, payload["chat_id"] = ALERTS_BOT_TOKEN, ALERTS_CHAT_ID
    else:
        token, payload["chat_id"] = BOT_TOKEN, CHAT_ID
        if channel != "signals" and TELEGRAM_QUIET and not force:
            return None

    if not token or not payload["chat_id"]:
        print(f"Telegram disabled ({channel}): missing bot token or chat id.")
        return None
    bot = "main" if token == BOT_TOKEN else "alerts"
    return token, payload, bot



# ── secret redaction ─────────────────────────────────────────────────────────
# requests puts the FULL request URL into its exception messages, and every
# Telegram URL embeds the bot token. Printing such an exception writes a live
# credential into app/logs/*.log — which /health then tails and renders in the
# browser (last_error, 220 chars, more than enough for a whole token). Anything
# that logs an exception from a Telegram call must go through this.
_TOKEN_RE = re.compile(r"(bot)\d{6,}:[A-Za-z0-9_\-]{20,}")


def redact(text) -> str:
    """Strip Telegram bot tokens out of text about to be logged or displayed."""
    return _TOKEN_RE.sub(r"\1***", str(text))


def send_message(message, parse_mode=None, *, force=False, retries=2, channel="alerts",
                 reply_markup=None):
    """Long messages are split on line boundaries and sent as in-order
    chunks; returns True only when EVERY chunk was delivered. See _route()
    for the channel routing rules.

    reply_markup (an inline keyboard dict) rides on the LAST chunk only —
    Telegram attaches a keyboard to one specific message, and a button under
    part 1 of 3 would sit above the text it acts on."""
    routed = _route(channel, force)
    if not routed:
        return False
    token, payload, bot = routed
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if parse_mode:
        payload["parse_mode"] = parse_mode

    ok = True
    parts = _chunks_of(message)
    if parse_mode == "HTML" and len(parts) > 1:
        parts = balance_pre(parts)          # a split inside <pre> must not 400
    for i, part in enumerate(parts):
        body = {**payload, "text": part}
        if reply_markup and i == len(parts) - 1:
            body["reply_markup"] = json.dumps(reply_markup)
        sent, mid = _post_one(url, body, retries)
        if sent and mid:
            _record_sent(payload["chat_id"], mid, bot)   # /clean can find it later
        if not sent:
            ok = False
    return ok


def pin_message(chat_id, message_id, *, bot: str = "main") -> bool:
    """pinChatMessage — returns False (never raises) when the bot lacks
    admin+pin rights in the chat, which is the common first-time state."""
    token = ALERTS_BOT_TOKEN if bot == "alerts" else BOT_TOKEN
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/pinChatMessage",
                          data={"chat_id": chat_id, "message_id": message_id,
                                "disable_notification": True}, timeout=15)
        if not r.ok:
            print(f"[tg] pin failed (bot needs admin+pin rights?): {r.text[:200]}")
        return r.ok
    except requests.RequestException as exc:
        print(f"[tg] pin error: {redact(exc)}")
        return False


def unpin_message(chat_id, message_id, *, bot: str = "main") -> bool:
    token = ALERTS_BOT_TOKEN if bot == "alerts" else BOT_TOKEN
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/unpinChatMessage",
                          data={"chat_id": chat_id, "message_id": message_id}, timeout=15)
        return r.ok
    except requests.RequestException as exc:
        print(f"[tg] unpin error: {redact(exc)}")
        return False


def send_message_and_pin(message, parse_mode=None, *, force=False, retries=2,
                         channel="alerts", unpin_previous=None):
    """Send, then pin the result — using the SAME resolved (token, chat_id)
    for both, so this never pins in the wrong chat just because routing fell
    through to a different tier (see _route()). Returns the new message_id
    (the send succeeded, whether or not the pin itself did — pin failure is
    logged, not fatal: most commonly the bot just isn't a group admin yet),
    or None if the send failed."""
    routed = _route(channel, force)
    if not routed:
        return None
    token, payload, bot = routed
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if parse_mode:
        payload["parse_mode"] = parse_mode
    parts = _chunks_of(message)
    if parse_mode == "HTML" and len(parts) > 1:
        parts = balance_pre(parts)
    last_mid = None
    for part in parts:
        sent, mid = _post_one(url, {**payload, "text": part}, retries)
        if not sent:
            return None
        if mid:
            _record_sent(payload["chat_id"], mid, bot)
            last_mid = mid
    if last_mid and pin_message(payload["chat_id"], last_mid, bot=bot):
        if unpin_previous and unpin_previous != last_mid:
            unpin_message(payload["chat_id"], unpin_previous, bot=bot)
    return last_mid
