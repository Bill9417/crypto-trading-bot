import time

import requests

from config import BOT_TOKEN, CHAT_ID, TELEGRAM_QUIET


def send_message(message, parse_mode=None, *, force=False, retries=2):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    # Quiet mode: only explicitly-forced messages (the RSI extreme alert, the
    # naked-position safety alarm, S3 live-engine trade alerts, pump-radar
    # movers) are delivered. Everything else — S2 signal digests, order fills,
    # startup/shutdown, halt notices — is muted here so no message source can
    # leak through. Toggle with TELEGRAM_QUIET in .env.
    if TELEGRAM_QUIET and not force:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }
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
