import requests

from config import BOT_TOKEN, CHAT_ID


def send_message(message, parse_mode=None):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        response = requests.post(url, data=payload, timeout=15)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError:
        print(f"Telegram API Error response: {response.text}")
        return False
    except requests.RequestException as exc:
        print(f"Telegram send failed: {exc}")
        return False
