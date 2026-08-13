"""
🔕 Which alert types are muted.

Asked for the 急拉 MOVER alerts to be switchable off. Built as a general
mechanism rather than a MOVER_ALERTS flag, because there are nine alert types
firing into Telegram and the next one to become annoying would otherwise get
its own env var, its own restart, and its own way of being spelled.

MUTING HAPPENS AT THE SEND, NOT AT THE DETECTOR. A muted MOVER is still
detected, still logged, and still appears on the dashboard — only the Telegram
message is skipped. That distinction matters: turning the detector off would
silently stop the pump radar feeding the dashboard strip, and "I muted a
notification" should never mean "I stopped collecting the data".

Runtime, not env. An env var needs a file edit and a restart to silence
something that is annoying you right now; /mute takes effect on the next alert.
"""
import json
import os
import time

STORE_FILE = os.path.join(os.path.dirname(__file__), "alert_prefs.json")

# The registry is what /mute lists, so an unregistered kind cannot be muted by
# a typo that silently matches nothing.
KINDS = {
    "mover":  "🚀 急拉 / 急殺 MOVER（幣價一小時異動）",
    "crowd":  "🐋 OI 異常（未平倉異常堆積）",
    "flip":   "🚀 壓力翻支撐",
    "s4":     "📊 S4 掃描設定",
    "liq":    "💥 清算瀑布",
    "whale":  "🐳 巨鯨動向",
    "event":  "🌍 大事件雷達",
    "tech":   "💻 科技新聞",
    "price":  "🔔 價格提醒",
}


def _blank() -> dict:
    return {"muted": [], "changed": None}


def load(path: str = None) -> dict:
    try:
        with open(path or STORE_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return _blank()
    for k, v in _blank().items():
        d.setdefault(k, v)
    d["muted"] = [k for k in d["muted"] if k in KINDS]
    return d


def save(store: dict, path: str = None) -> None:
    p = path or STORE_FILE
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(tmp, p)


def is_muted(kind: str) -> bool:
    """False for anything unregistered — an unknown kind must still be
    delivered. Failing silent here would mean a typo in a call site quietly
    disables an alert nobody knows is off."""
    if not kind or kind not in KINDS:
        return False
    return kind in load()["muted"]


def mute(kind: str) -> bool:
    if kind not in KINDS:
        return False
    st = load()
    if kind not in st["muted"]:
        st["muted"].append(kind)
        st["changed"] = time.time()
        save(st)
    return True


def unmute(kind: str) -> bool:
    if kind not in KINDS:
        return False
    st = load()
    if kind in st["muted"]:
        st["muted"].remove(kind)
        st["changed"] = time.time()
        save(st)
    return True


def status_text() -> str:
    """What /mute prints with no argument."""
    st = load()
    muted = set(st["muted"])
    lines = ["🔕 <b>通知開關</b>", ""]
    for k, label in KINDS.items():
        mark = "🔕 靜音" if k in muted else "🔔 開啟"
        lines.append(f"{mark}  <code>{k}</code>  {label}")
    lines += ["", "用法：<code>/mute mover</code> 關閉、<code>/unmute mover</code> 開啟。",
              "靜音只擋 Telegram 通知 —— 偵測、紀錄和網頁都照常。"]
    return "\n".join(lines)
