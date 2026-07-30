#!/usr/bin/env python
"""One-time: populate the Telegram "☰ Menu" command list (setMyCommands) so
tapping Menu shows a scrollable, emoji-labelled list instead of members
having to remember /commands or read the /help wall of text.

Only genuinely public, read-only commands are listed — admin/owner-only
ones (/clean /cleanall /resume /halt /whaleadd /whalerm /report) are left
out of the menu on purpose: they still work if typed (gated server-side in
tg_commands.handle(), unaffected by this file either way), but listing them
publicly would just invite non-admins to try commands that always no-op or
403 for them.

    python set_bot_commands.py
"""
import sys

import requests

import config

TOKEN = config.BOT_TOKEN

# (command, description) — order is the order shown in the Menu list.
COMMANDS = [
    ("guide", "📖 群組導覽 — 新朋友從這裡開始"),
    ("help", "❓ 完整指令清單"),
    ("price", "💰 即時報價"),
    ("winrate", "📊 真實帳戶勝率報告"),
    ("positions", "📋 未平倉部位"),
    ("signals", "🔔 最近訊號（含進場/停損/目標）"),
    ("outcomes", "✅ 訊號成績單（48h 後真實結果）"),
    ("paper", "📝 S1 前測戰績（紙上模擬）"),
    ("mom", "📈 ETH 動能前測戰績"),
    ("tw", "🇹🇼 最新台股掃描"),
    ("twnow", "🇹🇼 台股即時現況"),
    ("us", "🇺🇸 昨夜美股收盤摘要"),
    ("liq", "💥 清算地圖與統計"),
    ("whale", "🐳 巨鯨持倉追蹤"),
    ("alerts", "⏰ 目前到價提醒"),
    ("link", "🔗 網站儀表板連結"),
]


def main() -> int:
    if not TOKEN:
        print("✗ TELEGRAM_BOT_TOKEN missing from .env — abort.")
        return 1
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
            json=payload, timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Request failed: {exc}")
        return 1
    if not data.get("ok"):
        print(f"✗ Telegram refused: {data.get('description', data)}")
        return 1
    print(f"✓ Set {len(COMMANDS)} commands in the Menu list.")
    print("  Open the bot's chat and tap the ☰ Menu button (or type /) to see it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
