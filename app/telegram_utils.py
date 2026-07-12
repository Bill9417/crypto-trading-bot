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
    # 🌍 big-event radar / 💻 tech digest / 📈 daily report — each falls back
    # to the Alerts thread until its own topic exists
    "events": TELEGRAM_EVENTS_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "tech": TELEGRAM_TECH_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "report": TELEGRAM_REPORT_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "twstocks": TELEGRAM_TWSTOCKS_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
    "liq": TELEGRAM_LIQ_THREAD_ID or TELEGRAM_ALERTS_THREAD_ID,
}


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
    """
    thread_id = _TOPIC_THREAD.get(channel)
    payload = {"text": message}

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

    # api.telegram.org intermittently times out from this network (observed in
    # bot.log), so network-level failures get a couple of retries; an HTTP error
    # response (bad request / blocked bot) is final and never retried.
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, data=payload, timeout=15)
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
