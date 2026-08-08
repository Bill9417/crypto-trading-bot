#!/usr/bin/env python
"""One-time: close every signal/broadcast topic to regular-member posting,
leaving General (the default topic) open for free chat.

Telegram's forum "closed" topic state blocks NEW messages from ordinary
members but NOT from admins — and this bot is already an admin with the
"Manage Topics" right (it created several of these topics itself, see
create_s1_topic.py), so scheduled signal/digest posts keep flowing after
closing. This does NOT delete history, unpin anything, or touch General.

    python close_signal_topics.py            # closes ALL topics below
    python close_signal_topics.py --test-one # closes just one (Tech) + a
                                              # confirmation post, to verify
                                              # the bot can still post in a
                                              # closed topic before doing the rest

Re-running after topics are already closed prints "TOPIC_NOT_MODIFIED" for
each one — that's Telegram confirming the desired state already holds, not
a real failure.
"""
import sys

import requests

import config
import telegram_utils

TOKEN = config.BOT_TOKEN
CHAT_ID = config.TELEGRAM_GROUP_CHAT_ID

# (label, thread_id, telegram_utils channel name for the confirmation post)
TOPICS = [
    ("📊 訊號", config.TELEGRAM_SIGNALS_THREAD_ID, "signals"),
    ("🔔 提醒", config.TELEGRAM_ALERTS_THREAD_ID, "alerts"),
    ("🌍 大事件", config.TELEGRAM_EVENTS_THREAD_ID, "events"),
    ("💻 科技", config.TELEGRAM_TECH_THREAD_ID, "tech"),
    ("📈 每日報告", config.TELEGRAM_REPORT_THREAD_ID, "report"),
    ("🇹🇼 台股", config.TELEGRAM_TWSTOCKS_THREAD_ID, "twstocks"),
    ("💥 清算", config.TELEGRAM_LIQ_THREAD_ID, "liq"),
    # 📈 S1 交易訊號 was DELETED 2026-08-08 — S1/S3/S4 moved to the
    # private "trades" feed, and the public topic went with them.
]

NOTICE = "📌 本主題現為「僅限訊號公告」— 開放聊天請至一般 (General) 頻道。"


def close_topic(thread_id: str) -> tuple[bool, str]:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/closeForumTopic",
            json={"chat_id": CHAT_ID, "message_thread_id": int(thread_id)},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if not data.get("ok"):
        return False, data.get("description", str(data))
    return True, ""


def run(topics):
    if not TOKEN or not CHAT_ID:
        print("✗ TELEGRAM_BOT_TOKEN / TELEGRAM_GROUP_CHAT_ID missing from .env — abort.")
        return 1
    ok_all = True
    for label, thread_id, channel in topics:
        if not thread_id:
            print(f"— {label}: no thread id configured, skipped")
            continue
        ok, err = close_topic(thread_id)
        if not ok:
            print(f"✗ {label} (thread {thread_id}): close failed — {err}")
            ok_all = False
            continue
        print(f"✓ {label} (thread {thread_id}): closed to member posting")
        sent = telegram_utils.send_message(NOTICE, channel=channel, force=True)
        print(f"  {'✓' if sent else '✗'} confirmation post via bot: {'sent' if sent else 'FAILED — bot may not be able to post here anymore!'}")
        if not sent:
            ok_all = False
    return 0 if ok_all else 1


if __name__ == "__main__":
    if "--test-one" in sys.argv:
        sys.exit(run(TOPICS[3:4]))   # Tech — lowest-stakes topic to verify on first
    sys.exit(run(TOPICS))
