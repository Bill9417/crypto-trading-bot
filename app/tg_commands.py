"""
Telegram command bot — ask the group for stats and it answers.

A daemon thread inside the Strategy-2 scanner long-polls getUpdates on the
main bot token and answers slash-commands, replying in whatever topic thread
the command was typed in:

    /guide      the group guide — topics map + honesty policy (also auto-sent
                as a short welcome when a new member joins the group)
    /price      spot quotes, default BTC ETH SOL BNB (Binance public REST)
    /winrate    win-rate report from REAL exchange records (Binance + Bybit)
    /positions  open positions on both accounts, with unrealized P&L
    /signals    last fired S2 signals with their Entry/SL/TP plans
    /alerts     price alerts currently armed (dashboard 🔔 card)
    /report     today's daily report, on demand
    /tw         latest 台股 scan (TAIEX regime + TW50 setups)
    /twnow      live 台股 snapshot: TAIEX, TW50 leaders, tracked setups
    /liq        BTC/ETH liquidations: 24h tallies, recent prints with
                prices, and the estimated 🧲 liquidation map
    /paper      S1 forward paper-tracker status (no real money) — the full
                live strategy vs a longs-only variant, tracked forward in
                real time since the 2026-07-27 walk-forward found shorts
                carrying S1's whole negative expectancy
    /clean [h]  ADMIN-ONLY: delete the bot's messages older than h hours
                (default 24; Telegram forbids deleting anything older than
                48h, and only messages sent since the ledger exists are
                tracked). Sender must be a group admin or the owner.
    /restart    OWNER-ONLY: restart the whole stack so new code takes effect
                (./run_all.sh bg, run detached by restart_ctl). "/restart
                check" only reports what is running stale, and changes nothing
    /cleanall   ADMIN-ONLY, needs "/cleanall yes": one-time backfill sweep of
                everything sent BEFORE the ledger existed (sequential-id brute
                force below the first recorded message; 48h wall still applies)
    /help       this list

Safety: commands are only honoured from the configured group / owner chats —
anything else is silently ignored. No command places or closes an order. Two
commands are not pure reads — /clean deletes the bot's own messages, and
/restart bounces the stack — and both are gated tighter than the rest
(/restart is owner-only, and the inline button re-checks the tapper). First run
seeds the update offset silently so a backlog of old messages never triggers
a reply storm (same pattern as event_radar's first-run seeding).

No webhook needed: getUpdates long-polling works from behind NAT and nothing
else consumes this bot's update queue.
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

import config
import telegram_utils

STATE_FILE = os.path.join(os.path.dirname(__file__), "tg_commands_state.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")

POLL_TIMEOUT = int(os.getenv("TG_CMD_POLL_TIMEOUT", "25"))   # long-poll seconds

# Chats allowed to command the bot: the topics group + the owner's DMs.
ALLOWED_CHATS = {str(c) for c in (config.TELEGRAM_GROUP_CHAT_ID, config.CHAT_ID,
                                  config.ALERTS_CHAT_ID) if c}

# Destructive commands need more than "typed inside the group": the SENDER
# must be a group admin (checked live via getChatAdministrators, cached) or
# the owner (the DM chat ids double as the owner's user ids).
ADMIN_COMMANDS = {"clean", "clear", "purge", "cleanall", "resume", "halt",
                  "whaleadd", "whalerm", "whalesync"}
OWNER_IDS = {str(c) for c in (config.CHAT_ID, config.ALERTS_CHAT_ID) if c}
# Commands that read the REAL money: balances, open positions, entry prices,
# unrealized/realized P&L in USDT. Strictly the owner's — not group admins,
# not members — and the answer always goes to the owner's DM no matter where
# it was typed. The group is being promoted publicly; anyone who joins could
# otherwise type /positions and read the owner's book.
# restart/reboot are here rather than in ADMIN_COMMANDS on purpose: a group
# admin is trusted to delete messages, not to bounce the live trading engine.
# s3/xaut/s4 joined 2026-08-08 with the private trade feed: it would be absurd
# to move S1/S3/S4 out of the public group and then let any member type /s3 and
# read the same state back.
OWNER_ONLY_COMMANDS = {"report", "positions", "pos", "winrate", "stats", "wr",
                       "restart", "reboot", "s3", "xaut", "s4", "stockperp"}
PRIVATE_REPLY_COMMANDS = set(OWNER_ONLY_COMMANDS)
ADMIN_CACHE_SEC = 300

# 👆 Tap-to-refresh — the "live data" commands where re-running is a natural
# next action. Telegram has no persistent menu-button equivalent to LINE's
# Quick Reply that survives across messages, but inline keyboard buttons
# attached to a reply DO stay tappable indefinitely (they're part of that
# specific message, not a suggestion bar), which is the closer fit here.
REFRESHABLE_CMDS = {"positions", "price", "signals", "winrate", "alerts",
                    "liq", "whale", "whaletop", "twnow", "paper", "us", "s4",
                    "s3", "xaut"}
_admin_cache = {"ts": 0.0, "ids": set()}


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh start
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ── pure helpers (unit-testable) ─────────────────────────────────────────────
def parse_command(text: str):
    """'/winrate@WolfBot btc' → ('winrate', 'btc'); None for non-commands."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, args = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    return (cmd, args.strip()) if cmd else None


def allowed(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHATS


def refresh_keyboard(cmd: str, args: str = "") -> dict:
    """Inline keyboard with a single 🔄 Refresh button that re-runs cmd/args
    via a callback_query. callback_data has Telegram's 64-byte hard limit —
    fine for REFRESHABLE_CMDS, which take short or no args in practice."""
    data = f"r:{cmd}:{args}"[:64]
    return {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": data}]]}


def parse_refresh_callback(data: str):
    """'r:positions:' → ('positions', ''); None for anything else."""
    if not (data or "").startswith("r:"):
        return None
    _, _, rest = data.partition(":")
    cmd, _, args = rest.partition(":")
    return (cmd, args) if cmd else None


# 👆 Action buttons — a SEPARATE protocol from the r: refresh buttons above,
# because refresh is idempotent and these are not. A refresh reply keeps its
# own 🔄 button so it can be tapped again; an action button is stripped from
# the message the moment it fires, so a double-tap (or a scroll back to the
# same message tomorrow) cannot launch a second restart.
ACTIONS = {"restart": "🔄 立即重啟"}


def action_keyboard(action: str) -> dict:
    return {"inline_keyboard": [[{"text": ACTIONS[action],
                                  "callback_data": f"x:{action}"}]]}


def parse_action_callback(data: str):
    """'x:restart' → 'restart'; None for anything that isn't a known action."""
    if not (data or "").startswith("x:"):
        return None
    action = (data or "").partition(":")[2]
    return action if action in ACTIONS else None


def _group_admin_ids() -> set:
    """User ids of the topics group's admins, cached ADMIN_CACHE_SEC. On an
    API failure the stale cache keeps serving (owner ids always work)."""
    if not config.TELEGRAM_GROUP_CHAT_ID:
        return set()
    if time.time() - _admin_cache["ts"] < ADMIN_CACHE_SEC:
        return _admin_cache["ids"]
    try:
        res = _api("getChatAdministrators",
                   chat_id=config.TELEGRAM_GROUP_CHAT_ID)
        _admin_cache["ids"] = {str((a.get("user") or {}).get("id"))
                               for a in res.get("result") or []}
    except Exception as exc:  # noqa: BLE001 — keep the stale set, retry later
        print(f"[tgcmd] getChatAdministrators failed: {exc}")
    _admin_cache["ts"] = time.time()
    return _admin_cache["ids"]


def is_admin(user_id) -> bool:
    uid = str(user_id)
    return uid in OWNER_IDS or uid in _group_admin_ids()


def authorized(cmd: str, user_id) -> bool:
    """Read-only commands: anyone in an allowed chat. ADMIN_COMMANDS: group
    admins / the owner only. OWNER_ONLY_COMMANDS: strictly the owner."""
    if cmd in OWNER_ONLY_COMMANDS:
        return str(user_id) in OWNER_IDS
    return cmd not in ADMIN_COMMANDS or is_admin(user_id)


def _n(v, digits=2):
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _pnl(v):
    try:
        return f"{float(v):+,.2f}"
    except (TypeError, ValueError):
        return "?"


def fmt_winrate(binance: dict, bybit: dict, owner: bool = False) -> str:
    """The /winrate report — REAL exchange records, fees included, not the
    simulated tracker. Sections degrade to the error text on an API blip.

    The Bybit panel is the whole SUB-ACCOUNT: S1's mirror, S3's flip engine and
    the operator's own manual trades all settle there, so its headline is not
    any one strategy's track record. owner=True appends the per-strategy split
    (see strategy_ledger)."""
    import tg_format
    lines = ["🎯 勝率 · 真實帳戶成交\n"]
    for icon, name, s in (("🟨", "Binance · S1/S2", binance),
                          ("🟧", "Bybit · S1+S3+手動（整個子帳戶）", bybit)):
        lines.append(f"{icon} {name}")
        if not (s or {}).get("ok"):
            lines.append(f"暫時無法取得（{(s or {}).get('error', 'no data')}）\n")
            continue
        n = s.get("n_trades") or 0
        if not n:
            lines.append("尚無平倉紀錄\n")
            continue
        pf = s.get("profit_factor")
        streak = f"{s.get('streak_type') or ''}{s.get('streak') or 0}"
        week = sum(d.get("net") or 0.0 for d in (s.get("daily") or [])[-7:])
        lines.append(tg_format.pre_table([
            ("成交", f"{n} 筆", f"{s.get('wins')}勝 {s.get('losses')}敗",
             f"→ {_n(s.get('win_rate'), 1)}%"),
            ("淨值", f"{_pnl(s.get('net'))} USDT", "含手續費",
             f"PF {pf if pf is not None else '—'}"),
            ("平均", f"獲利 {_pnl(s.get('avg_win'))}",
             f"虧損 {_pnl(s.get('avg_loss'))}", f"連續 {streak}"),
            ("7日", _pnl(week), "最大回撤", _pnl(s.get('max_drawdown'))),
        ], align="llll"))
        lines.append("")
    lines.append("⚠️ 勝率不代表賺錢 — 90% 勝率但那 10% 賠很大照樣虧。"
                 "要跟獲利因子（PF）和淨值一起看。")
    if owner and (bybit or {}).get("ok") and (bybit or {}).get("trades"):
        try:
            import strategy_ledger
            split = strategy_ledger.report(bybit["trades"])
            fees = strategy_ledger.fee_report(bybit["trades"])
        except Exception as exc:  # noqa: BLE001 — a broken split must not eat the report
            split, fees = f"（各策略拆帳暫時無法計算：{str(exc)[:80]}）", ""
        lines += ["", split]
        if fees:
            lines += ["", fees]
    return "\n".join(lines)


def fmt_positions(binance: dict, bybit: dict) -> str:
    import tg_format
    lines = ["📌 未平倉部位\n"]
    for icon, name, snap in (("🟨", "Binance", binance), ("🟧", "Bybit", bybit)):
        lines.append(f"{icon} {name}")
        if not (snap or {}).get("ok", True) and not (snap or {}).get("positions"):
            lines.append(f"暫時無法取得（{(snap or {}).get('error', 'no data')}）\n")
            continue
        poss = (snap or {}).get("positions") or []
        if not poss:
            lines.append("無持倉\n")
            continue
        rows = [("幣種", "方向", "進場", "未實現", "", "")]
        for p in poss[:10]:
            base = (p.get("symbol") or "?").split("/")[0]
            pct = p.get("pnl_pct")
            rows.append((base, tg_format.dir_zh(p.get("side"), arrow=False),
                         tg_format.fmt_price(p.get("entry")),
                         _pnl(p.get("unrealized_pnl")),
                         f"{_pnl(pct)}%" if pct is not None else "",
                         p.get("engine") or ""))       # s3 / s1鏡 on Bybit rows
        lines.append(tg_format.pre_table(rows))
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_signals(payload: dict, limit: int = 5) -> str:
    import tg_format
    sigs = (payload or {}).get("signals") or []
    if not sigs:
        return "過去24小時沒有 S2 訊號。"
    lines = [f"📊 最近 {min(limit, len(sigs))} 個 S2 訊號 "
             f"({payload.get('timeframe', '15m')})\n"]
    rows = [("", "方向", "信心", "", "進場", "停損", "目標", "")]
    for s in sigs[:limit]:
        age_min = int((time.time() - (s.get("ts") or 0)) / 60)
        age = f"{age_min}分前" if age_min < 120 else f"{age_min // 60}小時前"
        arrow = "🟢" if s.get("direction") == "long" else "🔴"
        star = "⭐" if s.get("premium") else ""
        dir_zh = "做多" if s.get("direction") == "long" else "做空"
        has_plan = s.get("entry") and s.get("sl") and s.get("tp2")
        rows.append((
            f"{arrow} {s.get('base')}", f"{dir_zh}{star}",
            s.get("score"), age,
            tg_format.fmt_price(s["entry"]) if has_plan else "",
            tg_format.fmt_price(s["sl"]) if has_plan else "",
            tg_format.fmt_price(s["tp2"]) if has_plan else "", ""))
    lines.append(tg_format.pre_table(rows))
    lines.append("\n⭐ = 精選訊號（信心+BTC同向+趨勢確認）· 目標 = 最終目標 TP2")
    return "\n".join(lines)


def fmt_alerts(alerts: list) -> str:
    import tg_format
    active = [a for a in alerts if not a.get("triggered")]
    fired = [a for a in alerts if a.get("triggered")]
    if not alerts:
        return "🔔 尚未設定到價提醒 — 可在儀表板新增。"
    rows = []
    for a in active:
        rows.append(("▸", a["base"],
                     "▲ 突破" if a["direction"] == "above" else "▼ 跌破",
                     f"{a['price']:,.6g}"))
    for a in fired[:5]:
        rows.append(("✓", a["base"], "已觸發",
                     f"{a.get('triggered_price', 0):,.6g}"))
    return "🔔 到價提醒\n" + tg_format.pre_table(rows, align="lllr")


# ── group guide + welcome (the promo surface) ────────────────────────────────
GUIDE = (
    "📖 群組導覽\n"
    "\n"
    "本群由 24/7 自動化交易系統驅動。所有訊號都附進場/停損/目標，"
    "而且每個訊號 48 小時後會用真實 K 線結算成績（/outcomes）— "
    "我們公開輸單，不只貼贏單。\n"
    "\n"
    "🗂 主題頻道\n"
    "📊 訊號 — S2 掃描（15m）：⭐ 精選訊號 + 訊號榜 + 週日成績單\n"
    "📈 每日報告 — 每天早上市場快報（價格、恐懼貪婪、總經日曆）\n"
    "🌍 大事件 — Fed／地緣政治／監管／駭客 突發 + 行情劇變警報\n"
    "💥 清算 — BTC/ETH 清算連鎖警報 + 🧲 清算地圖 + 🐳 巨鯨持倉追蹤\n"
    "🇹🇼 台股 — TW50 回踩掃描（每交易日 14:00）+ 盤中異動\n"
    "💻 科技 — AI／科技新聞摘要（每 6 小時）\n"
    "🔔 提醒 — 到價提醒與系統通知\n"
    "\n"
    "🤖 常用指令\n"
    "/price — 即時報價 · /signals — 最近訊號 · /outcomes — 成績單\n"
    "/liq — 清算地圖 · /whale — 巨鯨持倉 · /help — 完整清單\n"
    "\n"
    "⚠️ 誠實原則：勝率不是保證，歷史不代表未來。所有內容僅供參考，"
    "非投資建議 — 資金管理永遠是你自己的責任。")


def welcome_text(names: list) -> str:
    """Short greeting for new members — points at the full /guide."""
    who = "、".join(n for n in names if n)[:80] or "新朋友"
    return (f"👋 歡迎 {who}！\n"
            "這裡是自動化交易訊號群 — 訊號附進場/停損/目標，"
            "成績每週公開結算，輸單也照貼。\n"
            "先看 /guide 了解各主題頻道，常用指令在 /help。\n"
            "⚠️ 訊號僅供參考，非投資建議。")


HELP = ("🤖 指令列表\n"
        "/guide — 📖 群組導覽（新朋友從這裡開始）\n"
        "/price [幣] — 即時報價（預設 BTC ETH SOL BNB）\n"
        "/winrate — 真實 Binance + Bybit 帳戶的勝率報告\n"
        "/positions — 兩個帳戶的未平倉部位\n"
        "/signals — 最近的 S2 訊號（含進場/停損/目標）\n"
        "/alerts — 目前設定的到價提醒\n"
        "/link — 🔗 網站儀表板連結（重啟後自動更新公告）\n"
        "/report — 今日帳戶+市場日報（擁有者專用, 只私訊回覆）\n"
        "/tw — 最新台股掃描（大盤狀態 + 設定）\n"
        "/twnow — 台股即時: TAIEX + 漲跌幅前三 + 追蹤設定現價\n"
        "/us — 🇺🇸 昨夜美股收盤摘要（指數 + 台積電 ADR + 費半）\n"
        "/liq — BTC/ETH 清算: 24h統計 + 最近清算價 + 🧲清算地圖\n"
        "/whale — 🐳 巨鯨追蹤: 每個地址的即時持倉（Hyperliquid）\n"
        "/whaletop — 🐳 巨鯨候選名單: 官方排行榜篩出的大戶（已排除做市商/空投戶）\n"
        "/whaleadd <0x地址> [名稱] · /whalerm <地址> — 管理追蹤清單（限管理員）\n"
        "/whalesync [dry] — 自動加入排行榜前段的新巨鯨（限管理員）\n"
        "/s3 [標的] — S3 目前為什麼有／沒有部位（擁有者專用, 只私訊回覆）\n"
        "/s4 — S4 永續掃描目前的設定（擁有者專用, 只私訊回覆）\n"
        "/outcomes — 訊號成績單: 每個訊號 48h 後的真實結果\n"
        "/mom — ETH 14 日動能紙上前測戰績\n"
        "/paper — S1 前測戰績（紙上模擬, 無真實下單）: 完整版 vs 只做多版\n"
        "/resume — 解除 S3 熔斷（限管理員）· /halt [原因] — 手動熔斷\n"
        "/clean [小時] — 刪除 bot 超過 N 小時的舊訊息（預設 24, 上限 47, 限管理員）\n"
        "/cleanall — 一次清掉記錄功能上線前的全部舊訊息（限管理員, 需確認）\n"
        "/help — 顯示這份清單\n"
        "\n💡 /positions /price /signals /winrate /alerts /liq /whale /twnow /paper "
        "/s3 /s4 的回覆下方有 🔄 按鈕，點一下就能直接更新，不用重打指令")


# ── /price — quick quotes (Binance spot public REST, no key) ────────────────
PRICE_DEFAULT = ("BTC", "ETH", "SOL", "BNB")
PRICE_MAX = 6


def _fetch_price(base: str):
    """One spot 24h ticker → {last, pct} or None (unknown symbol / blip)."""
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
                         params={"symbol": f"{base}USDT"}, timeout=8)
        if not r.ok:
            return None
        d = r.json()
        return {"last": float(d["lastPrice"]),
                "pct": float(d["priceChangePercent"])}
    except Exception:  # noqa: BLE001 — a dead quote is a per-symbol miss
        return None


def fmt_price_reply(rows: list) -> str:
    """rows = [(base, {last, pct} | None), ...] → aligned quote table."""
    import tg_format
    cells = []
    for base, q in rows:
        if not q:
            cells.append((base, "查無此幣", ""))
            continue
        cells.append((base, tg_format.fmt_price(q["last"]), tg_format.pct(q["pct"])))
    return "💲 即時報價（現貨 24h）\n" + tg_format.pre_table(cells, align="lrr")


def handle_price(args: str) -> str:
    bases = [b.strip().upper() for b in args.split() if b.strip()][:PRICE_MAX]
    bases = bases or list(PRICE_DEFAULT)
    return fmt_price_reply([(b, _fetch_price(b)) for b in bases])


def fmt_clean(summary: dict, hours: float) -> str:
    lines = [f"🧹 清理完成 (超過 {hours:g}h 的訊息)",
             f"已刪除 {summary['deleted']} 則"]
    if summary["too_old"]:
        lines.append(f"{summary['too_old']} 則超過48h — Telegram 不允許 bot 刪除，"
                     f"只能手動清")
    if summary["failed"]:
        lines.append(f"{summary['failed']} 則刪除失敗 (下次 /clean 會重試)")
    lines.append(f"{summary['kept']} 則未到時限，保留")
    lines.append("(只刪有記錄的訊息 — 此功能上線後 bot 發的訊息才有記錄)")
    return "\n".join(lines)


def fmt_stale(entries: list, in_flight: bool = False) -> str:
    """'/restart check' — what is running old code, and what changed."""
    if in_flight:
        return "⏳ 重啟進行中，等它跑完再看。"
    if not entries:
        return "✅ 每個程序跑的都是硬碟上最新的程式，不需要重啟。"
    lines = ["🆕 有新程式碼還沒生效:"]
    for e in entries:
        files = e["changed"]
        shown = "、".join(files[:4]) + (f" 等 {len(files)} 個檔" if len(files) > 4 else "")
        lines.append(f"• {e['label']} — {shown}")
    lines.append("")
    lines.append("按下面的按鈕或輸入 /restart 就會重啟 (先跑測試，沒過就不動)。")
    return "\n".join(lines)


GUIDE_CMDS = ("guide", "about", "intro")


def _should_pin_guide(state: dict, cmd: str, dest_chat, delivered: bool, mid) -> bool:
    """True when a just-delivered reply should become the pinned /guide:
    haven't pinned one yet this deployment, it's a guide command, it landed
    in the actual group (a DM has no 'pin for everyone' worth bothering
    with), and its message_id is known."""
    return bool(delivered and mid and cmd in GUIDE_CMDS
               and str(dest_chat) == str(config.TELEGRAM_GROUP_CHAT_ID)
               and not state.get("guide_pinned_mid"))


# ── command dispatch ─────────────────────────────────────────────────────────
def handle(cmd: str, args: str = "", owner: bool = False) -> str:
    """Command name (+ raw args) → reply text. Import-inside so one broken
    dependency degrades that command, not the whole bot. owner=True unlocks
    the private research detail inside otherwise-public replies."""
    if cmd in GUIDE_CMDS:
        return GUIDE
    if cmd in ("price", "p"):
        return handle_price(args)
    if cmd in ("picks", "top", "top3"):
        import top_picks
        return top_picks.as_text()
    if cmd in ("winrate", "stats", "wr"):
        import executor
        import strategy3_exec
        return fmt_winrate(executor.realized_pnl_summary(),
                           strategy3_exec.closed_pnl_summary(), owner=owner)
    if cmd in ("positions", "pos"):
        import executor
        import strategy3_exec
        return fmt_positions(executor.account_snapshot(),
                             strategy3_exec.account_snapshot())
    if cmd in ("signals", "sig"):
        try:
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:  # noqa: BLE001
            payload = {}
        return fmt_signals(payload)
    if cmd == "alerts":
        import price_alerts
        return fmt_alerts(price_alerts.load_alerts())
    if cmd == "report":
        import daily_report
        return daily_report.build_report(daily_report._gather(),
                                         datetime.now(daily_report.TZ))
    if cmd in ("mute", "unmute", "alerts", "notify"):
        # 🔕 Runtime notification switches. Muting stops the Telegram message
        # only — the scan, the record and the web page are untouched.
        import alert_prefs
        # `args` is the raw string after the command, not a list — args[0]
        # would take the first CHARACTER, so "/mute mover" would look for a
        # kind called "m" and report it unknown.
        target = (args or "").strip().lower().split(" ")[0]
        if not target:
            return alert_prefs.status_text()
        if target not in alert_prefs.KINDS:
            import tg_format as _F
            return ("不認得 <code>" + _F.esc(target) + "</code>。\n\n"
                    + alert_prefs.status_text())
        if cmd == "unmute":
            alert_prefs.unmute(target)
            return f"🔔 已開啟 <code>{target}</code> —— {alert_prefs.KINDS[target]}"
        alert_prefs.mute(target)
        return (f"🔕 已靜音 <code>{target}</code> —— {alert_prefs.KINDS[target]}\n"
                f"偵測和網頁照常，只是不再發 Telegram。"
                f"用 <code>/unmute {target}</code> 開回來。")
    if cmd in ("link", "url", "web"):
        import site_link
        return site_link.link_reply()
    if cmd in ("liq", "liquidations"):
        import liq_alerts
        return liq_alerts.build_report()
    if cmd in ("whale", "whales"):
        import whale_tracker
        return whale_tracker.build_report()
    if cmd in ("whaletop", "whalefind"):
        import whale_discover
        return whale_discover.build_report()
    if cmd == "whalesync":
        import whale_discover
        return whale_discover.sync(count=6, dry_run="dry" in args.lower())
    if cmd == "whaleadd":
        import whale_tracker
        parts = args.strip().split(None, 1)
        if not parts:
            return "用法: /whaleadd <0x地址> [名稱]"
        return whale_tracker.add_address(parts[0], parts[1] if len(parts) > 1 else "")
    if cmd == "whalerm":
        import whale_tracker
        if not args.strip():
            return "用法: /whalerm <0x地址>"
        return whale_tracker.remove_address(args.strip())
    if cmd in ("tw", "twstocks"):
        import tw_stocks
        st = tw_stocks._load_state()
        return (st.get("last_digest_text")
                or "尚未有台股掃描 — 每個交易日 14:00 (台北) 自動發送。")
    if cmd in ("us", "usmarket"):
        import us_market
        st = us_market._load_state()
        return (st.get("last_text")
                or "尚未有美股收盤摘要 — 每個交易日 08:00 (台北) 自動發送。")
    if cmd in ("twnow", "twlive"):
        import tw_intraday
        return tw_intraday.snapshot_text()
    if cmd == "outcomes":
        import signal_outcomes
        # Members see the scorecard; the exit-rule / cohort research is the
        # owner's to read and to decide whether to publish.
        return signal_outcomes.report(owner=owner)
    if cmd == "mom":
        import eth_mom
        return eth_mom.report()
    if cmd == "paper":
        import paper_tracker
        return paper_tracker.report_tg()
    if cmd in ("s4", "stockperp"):
        import strategy4
        return strategy4.report_tg()
    if cmd in ("s3", "xaut"):
        # "why is S3 not in a position right now" — the question that needed a
        # throwaway diagnostic script on 2026-08-08.
        import strategy3_status
        return strategy3_status.report_tg(args.strip() or None)
    if cmd == "resume":
        import strategy3_risk
        if strategy3_risk.clear_halt():
            return "▶️ S3 circuit breaker 已解除 — 下一個訊號恢復進場。"
        return "S3 沒有被熔斷，不需要解除。"
    if cmd == "halt":
        import strategy3_risk
        strategy3_risk.set_halt(args.strip() or "manual halt via /halt")
        return "🛑 S3 已手動熔斷 — 停止新倉 (已開倉位照常管理)。/resume 解除。"
    if cmd in ("clean", "clear", "purge"):
        try:
            hours = min(max(float(args), 1.0), 47.0) if args.strip() else 24.0
        except ValueError:
            hours = 24.0
        return fmt_clean(telegram_utils.clean_old_messages(hours), hours)
    if cmd == "cleanall":
        # Pre-ledger backfill: /clean can only see recorded messages, so the
        # backlog from before the ledger existed needs this one-time sweep.
        if args.strip().lower() != "yes":
            return ("⚠️ /cleanall 會把『記錄功能上線前』的舊訊息全部刪除 — "
                    "不分幾小時、包含群組成員的訊息 (Telegram 只允許刪 48h 內的，"
                    "更舊的會自動略過)。\n確定請輸入: /cleanall yes")
        chat = config.TELEGRAM_GROUP_CHAT_ID
        mids = [e["mid"] for e in telegram_utils._load_sent()
                if str(e.get("chat")) == str(chat)]
        if not mids:
            return "ledger 是空的，找不到基準訊息 id — 先讓 bot 發過訊息再試。"
        s = telegram_utils.deep_clean(chat, min(mids))
        return (f"🧹 深度清理完成\n已刪除 {s['deleted']} 則舊訊息\n"
                f"略過 {s['skipped']} (不存在 / 超過48h / 無權限)\n"
                f"共掃描 {s['tried']} 個訊息 id\n"
                f"之後用 /clean 24 做日常清理即可。")
    if cmd in ("restart", "reboot"):
        import restart_ctl
        if args.strip().lower() in ("check", "status", "?"):
            return fmt_stale(restart_ctl.stale(), restart_ctl.in_flight())
        return restart_ctl.request("telegram /restart")[1]
    if cmd in ("help", "start"):
        return HELP
    return None                                   # unknown command → stay silent


# ── Telegram plumbing ────────────────────────────────────────────────────────
def _api(method: str, *, http_timeout: float = 30, **params):
    """Telegram's getUpdates has its OWN `timeout` param (long-poll seconds),
    so the HTTP timeout must live under a different, keyword-only name — a
    positional `timeout` here collided with params['timeout'] and broke every
    poll (caught live 2026-07-11)."""
    r = requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}",
                      data=params, timeout=http_timeout)
    r.raise_for_status()
    return r.json()


def _reply(chat_id, thread_id, text, parse_mode=None, reply_markup=None):
    """Chunked, 429-aware reply. Returns (ok, last_mid) — ok is whether every
    part was delivered (a rate-limited reply used to be dropped silently
    while the log said 'answered', the same trap fixed in telegram_utils on
    2026-07-12); last_mid is the final chunk's message_id (for callers that
    need to pin it or attach a keyboard to), or None if nothing sent
    successfully. reply_markup (e.g. refresh_keyboard()) rides on the LAST
    chunk only — same reasoning as LINE's quick replies."""
    payload = {"chat_id": chat_id}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id:
        payload["message_thread_id"] = thread_id
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    ok = True
    last_mid = None
    parts = telegram_utils._chunks_of(text)
    if parse_mode == "HTML" and len(parts) > 1:
        parts = telegram_utils.balance_pre(parts)
    for i, part in enumerate(parts):
        part_payload = {**payload, "text": part}
        if reply_markup and i == len(parts) - 1:
            part_payload["reply_markup"] = json.dumps(reply_markup)
        for attempt in (0, 1):
            telegram_utils._pace()   # share the process-wide send spacing —
            # this thread posts to the same group as the scanner's senders
            r = requests.post(url, data=part_payload, timeout=15)
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = float((r.json().get("parameters") or {})
                                 .get("retry_after") or 5)
                except Exception:  # noqa: BLE001
                    wait = 5.0
                time.sleep(min(wait + 0.5, 35.0))
                continue
            if r.ok:
                try:
                    mid = (r.json().get("result") or {}).get("message_id")
                except Exception:  # noqa: BLE001
                    mid = None
                telegram_utils._record_sent(chat_id, mid, bot="main")
                if mid:
                    last_mid = mid
            ok = ok and r.ok
            break
    return ok, last_mid


def _edit_message(chat_id, message_id, text, parse_mode=None, reply_markup=None) -> bool:
    """editMessageText for the 🔄 Refresh callback — updates the existing
    message in place instead of sending a new one. 'message is not modified'
    (the data genuinely hasn't changed since last tap) is treated as success,
    not an error — the user just tapped refresh and nothing new happened."""
    payload = {"chat_id": chat_id, "message_id": message_id,
              "text": telegram_utils._chunks_of(text)[0]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        telegram_utils._pace()
        r = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/editMessageText",
            data=payload, timeout=15)
        if r.ok:
            return True
        try:
            desc = (r.json() or {}).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        if "not modified" in desc.lower():
            return True
        print(f"[tgcmd] edit failed: {r.text[:200]}")
        return False
    except requests.RequestException as exc:
        print(f"[tgcmd] edit error: {exc}")
        return False


def _answer_callback(callback_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": text}, timeout=15)
    except requests.RequestException as exc:  # noqa: BLE001
        print(f"[tgcmd] answerCallbackQuery error: {exc}")


# 👋 Auto-welcome: a new member's first impression of the group. Rate-limited
# so a join wave (or Telegram redelivering a service message) can't spam —
# one greeting per WELCOME_GAP_SEC covers the whole burst.
WELCOME_GAP_SEC = int(os.getenv("TG_WELCOME_GAP_SEC", "120"))
_last_welcome = 0.0


def _maybe_welcome(chat_id, thread_id, joiners) -> bool:
    global _last_welcome
    now = time.time()
    if now - _last_welcome < WELCOME_GAP_SEC:
        return False
    _last_welcome = now
    names = [(m.get("first_name") or m.get("username") or "").strip()
             for m in joiners]
    try:
        ok, _mid = _reply(chat_id, thread_id, welcome_text(names))
        print(f"[tgcmd] welcomed {len(joiners)} new member(s)")
        return ok
    except Exception as exc:  # noqa: BLE001 — a greeting must never kill the loop
        print(f"[tgcmd] welcome failed: {exc}")
        return False


# 🔄 Refresh taps: a callback_query from an inline keyboard button, not a new
# /command message. The reply is EDITED in place (not resent) so the button
# stays attached and the chat doesn't fill up with repeat copies.
_left_chats: set = set()


def _leave_foreign_chat(chat_id) -> None:
    """Walk out of a group that isn't ours. Tried once per chat per process —
    a failure (already gone, kicked, no rights) must not retry every poll."""
    key = str(chat_id)
    if key in _left_chats:
        return
    _left_chats.add(key)
    try:
        _api("leaveChat", chat_id=chat_id)
        print(f"[tgcmd] left unauthorised chat {key}")
    except Exception as exc:  # noqa: BLE001 — never kill the poll loop
        print(f"[tgcmd] leaveChat {key} failed: {telegram_utils.redact(exc)}")


def _handle_action(cb: dict, cb_id, action: str, chat_id, message_id) -> None:
    """Fire a one-shot action button. The button is removed BEFORE the action
    runs — /restart kills this very process a few seconds later, so anything
    left until after would simply never happen, and the button would still be
    sitting there tappable in the new session's chat history."""
    user_id = (cb.get("from") or {}).get("id")
    if not allowed(chat_id) or str(user_id) not in OWNER_IDS:
        _answer_callback(cb_id, "⛔ 這是擁有者專用")
        return
    if message_id:
        _edit_message(chat_id, message_id,
                      (msg_text(cb) + "\n\n👉 已按下重啟").strip(), None, None)
    _answer_callback(cb_id, "🔄 重啟中…")
    try:
        import restart_ctl
        _, note = restart_ctl.request(f"telegram button ({action})")
    except Exception as exc:  # noqa: BLE001 — a failed tap must still answer
        note = f"⚠️ 重啟啟動失敗: {str(exc)[:200]}"
    telegram_utils.send_message(note, force=True, channel="private")


def msg_text(cb: dict) -> str:
    return ((cb.get("message") or {}).get("text") or "").strip()


def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    action = parse_action_callback(data)
    if action:
        _handle_action(cb, cb_id, action, chat_id, message_id)
        return

    parsed = parse_refresh_callback(data)
    if not (parsed and chat_id and message_id):
        _answer_callback(cb_id)
        return
    cmd, args = parsed
    if not allowed(chat_id) or cmd not in REFRESHABLE_CMDS:
        _answer_callback(cb_id, "⛔ 無權限")
        return
    # The button lives in the message forever and ANY member can tap it, so it
    # must re-check the sender — gating the typed command alone would leave a
    # tappable back door to the same data.
    if not authorized(cmd, (cb.get("from") or {}).get("id")):
        _answer_callback(cb_id, "⛔ 這是擁有者專用的私人資訊")
        return
    try:
        # a refresh tap must resolve the same owner view as the original reply,
        # or the per-strategy split silently vanishes on refresh
        reply = handle(cmd, args, owner=str(
            (cb.get("from") or {}).get("id")) in OWNER_IDS)
    except Exception as exc:  # noqa: BLE001 — a broken handler must answer, not die
        reply = f"⚠ {cmd} failed: {str(exc)[:200]}"
    if reply:
        mode = "HTML" if ("<pre>" in reply or "<a href" in reply) else None
        _edit_message(chat_id, message_id, reply, mode, refresh_keyboard(cmd, args))
    _answer_callback(cb_id, "✅ 已更新")


def _poll_loop() -> None:
    state = _load_state()
    offset = state.get("offset")
    if offset is None:
        # First ever run: skip any backlog silently (old /commands must not
        # trigger a reply storm months later).
        try:
            updates = _api("getUpdates", http_timeout=20, timeout=0).get("result") or []
            offset = (updates[-1]["update_id"] + 1) if updates else 0
        except Exception:  # noqa: BLE001
            offset = 0
        state["offset"] = offset
        _save_state(state)
        print(f"[tgcmd] seeded update offset {offset}")

    while True:
        try:
            updates = _api("getUpdates", http_timeout=POLL_TIMEOUT + 20,
                           offset=offset, timeout=POLL_TIMEOUT,
                           allowed_updates='["message","callback_query"]').get("result") or []
        except Exception as exc:  # noqa: BLE001 — network blip: back off, retry
            print(f"[tgcmd] poll error: {telegram_utils.redact(exc)}")
            time.sleep(10)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            if "callback_query" in up:
                try:
                    _handle_callback(up["callback_query"])
                except Exception as exc:  # noqa: BLE001 — one bad tap must not kill the loop
                    print(f"[tgcmd] callback error: {exc}")
                continue
            msg = up.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            # 👋 join events (service messages) — greet humans, in the group only
            joiners = [m for m in (msg.get("new_chat_members") or [])
                       if not m.get("is_bot")]
            if joiners and str(chat_id) == str(config.TELEGRAM_GROUP_CHAT_ID):
                _maybe_welcome(chat_id, msg.get("message_thread_id"), joiners)
                continue
            # Someone adding the bot to THEIR group must not get a working
            # bot. Commands were already ignored there, but the bot would sit
            # in the chat looking usable — walk out instead. Private chats are
            # left alone (leaveChat does not apply, and a stranger DMing the
            # bot already gets nothing).
            if chat_id and not allowed(chat_id):
                if (msg.get("chat") or {}).get("type") in ("group", "supergroup", "channel"):
                    _leave_foreign_chat(chat_id)
                continue
            parsed = parse_command(msg.get("text") or "")
            if not parsed:
                continue
            cmd, args = parsed
            # remember the user's /command message too, so /clean sweeps it
            telegram_utils._record_sent(chat_id, msg.get("message_id"), bot="main")
            if not authorized(cmd, (msg.get("from") or {}).get("id")):
                denial = (f"⛔ /{cmd} 是擁有者專用的私人指令"
                          if cmd in OWNER_ONLY_COMMANDS
                          else f"⛔ /{cmd} 只有群組管理員可以使用")
                _reply(chat_id, msg.get("message_thread_id"), denial)
                print(f"[tgcmd] denied /{cmd} from non-admin")
                continue
            try:
                reply = handle(cmd, args, owner=str(
                    (msg.get("from") or {}).get("id")) in OWNER_IDS)
            except Exception as exc:  # noqa: BLE001 — a broken handler must answer, not die
                reply = f"⚠ {cmd} failed: {str(exc)[:200]}"
            if reply:
                # Private-reply commands answer in the owner's DM, never in
                # the group — the report holds real balances/P&L.
                dest_chat, dest_thread = chat_id, msg.get("message_thread_id")
                if cmd in PRIVATE_REPLY_COMMANDS and config.CHAT_ID:
                    dest_chat, dest_thread = config.CHAT_ID, None
                    if str(chat_id) != str(config.CHAT_ID):
                        _reply(chat_id, msg.get("message_thread_id"),
                               "📈 日報是私人資訊 — 已私訊給你")
                try:
                    # replies built in the house style carry <pre>/<a> markup —
                    # those need HTML parse mode; plain replies stay plain
                    mode = "HTML" if ("<pre>" in reply or "<a href" in reply) else None
                    markup = refresh_keyboard(cmd, args) if cmd in REFRESHABLE_CMDS else None
                    delivered, mid = _reply(dest_chat, dest_thread, reply, mode, markup)
                    print(f"[tgcmd] {'answered' if delivered else 'REPLY DROPPED'} /{cmd}")
                    # 📌 Pin the /guide reply once, ever — so new members find
                    # it without scrolling. If pinning fails (bot isn't a
                    # group admin yet), state stays unset and the NEXT /guide
                    # retries — self-healing once admin rights are granted.
                    if _should_pin_guide(state, cmd, dest_chat, delivered, mid):
                        if telegram_utils.pin_message(dest_chat, mid):
                            state["guide_pinned_mid"] = mid
                            print(f"[tgcmd] pinned /guide (mid={mid})")
                except Exception as exc:  # noqa: BLE001
                    print(f"[tgcmd] reply failed: {exc}")
        if updates:
            state["offset"] = offset
            _save_state(state)


_started = False


def start() -> bool:
    """Spawn the polling thread once. No-op without a bot token."""
    global _started
    if _started or not config.BOT_TOKEN:
        return False
    _started = True
    t = threading.Thread(target=_poll_loop, name="tg-commands", daemon=True)
    t.start()
    print(f"[tgcmd] command bot listening — chats {sorted(ALLOWED_CHATS)}")
    return True
