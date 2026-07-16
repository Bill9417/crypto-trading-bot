#!/usr/bin/env python
"""One-time provisioning for the 📈 S1 交易訊號 Telegram topic.

Creates the forum topic in the group (createForumTopic) and writes its thread
id to app/.env as TELEGRAM_S1SIGNALS_THREAD_ID, so the Strategy-1 copy-trade
feed (bot.py → channel="s1signals") delivers there instead of falling back to
the general Signals thread.

    python create_s1_topic.py

Idempotent: if the id is already in .env it prints it and exits WITHOUT
creating a duplicate topic. Requires the bot to be an admin of
TELEGRAM_GROUP_CHAT_ID with the "Manage Topics" permission. Restart the stack
(./run_all.sh bg) afterwards so the bot picks up the new thread id.
"""
import sys

import requests

import config

TOPIC_NAME = "📈 S1 交易訊號"
ICON_COLOR = 0x8EEE98            # green — a trade-signal feed
ENV_KEY = "TELEGRAM_S1SIGNALS_THREAD_ID"


def main() -> int:
    existing = config.read_env_var(ENV_KEY)
    if existing:
        print(f"✓ {ENV_KEY} already set to {existing} — topic exists, nothing to do.")
        print("  (remove that line from .env first if you want a fresh topic.)")
        return 0

    token = config.BOT_TOKEN
    chat_id = config.TELEGRAM_GROUP_CHAT_ID
    if not token or not chat_id:
        print("✗ TELEGRAM_BOT_TOKEN and TELEGRAM_GROUP_CHAT_ID must both be set in .env.")
        return 1

    print(f"Creating forum topic “{TOPIC_NAME}” in group {chat_id} …")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/createForumTopic",
            json={"chat_id": chat_id, "name": TOPIC_NAME, "icon_color": ICON_COLOR},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — report any network/JSON failure plainly
        print(f"✗ Request failed: {exc}")
        return 1

    if not data.get("ok"):
        print(f"✗ Telegram refused: {data.get('description', data)}")
        print("  The bot must be an ADMIN of the group with the ‘Manage Topics’ permission.")
        return 1

    thread_id = data["result"]["message_thread_id"]
    config.set_env_var(ENV_KEY, thread_id)
    print(f"✓ Created topic — thread id = {thread_id}")
    print(f"✓ Wrote {ENV_KEY}={thread_id} to {config.ENV_PATH}")
    print("→ Restart the stack (./run_all.sh bg) so the bot delivers there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
