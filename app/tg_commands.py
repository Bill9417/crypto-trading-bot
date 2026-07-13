"""
Telegram command bot — ask the group for stats and it answers.

A daemon thread inside the Strategy-2 scanner long-polls getUpdates on the
main bot token and answers slash-commands, replying in whatever topic thread
the command was typed in:

    /winrate    win-rate report from REAL exchange records (Binance + Bybit)
    /positions  open positions on both accounts, with unrealized P&L
    /signals    last fired S2 signals with their Entry/SL/TP plans
    /alerts     price alerts currently armed (dashboard 🔔 card)
    /report     today's daily report, on demand
    /tw         latest 台股 scan (TAIEX regime + TW50 setups)
    /twnow      live 台股 snapshot: TAIEX, TW50 leaders, tracked setups
    /liq        BTC/ETH liquidations: 24h tallies, recent prints with
                prices, and the estimated 🧲 liquidation map
    /clean [h]  ADMIN-ONLY: delete the bot's messages older than h hours
                (default 24; Telegram forbids deleting anything older than
                48h, and only messages sent since the ledger exists are
                tracked). Sender must be a group admin or the owner.
    /cleanall   ADMIN-ONLY, needs "/cleanall yes": one-time backfill sweep of
                everything sent BEFORE the ledger existed (sequential-id brute
                force below the first recorded message; 48h wall still applies)
    /help       this list

Safety: commands are only honoured from the configured group / owner chats —
anything else is silently ignored. The bot only ever READS (exchange
snapshots, state files); no command places or closes an order. First run
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
ADMIN_COMMANDS = {"clean", "clear", "purge", "cleanall"}
OWNER_IDS = {str(c) for c in (config.CHAT_ID, config.ALERTS_CHAT_ID) if c}
ADMIN_CACHE_SEC = 300
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
    admins / the owner only."""
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


def fmt_winrate(binance: dict, bybit: dict) -> str:
    """The /winrate report — REAL exchange records, fees included, not the
    simulated tracker. Sections degrade to the error text on an API blip."""
    lines = ["🎯 WIN RATE — real account trades\n"]
    for icon, name, s in (("🟨", "Binance (S1/S2)", binance),
                          ("🟧", "Bybit (S3)", bybit)):
        lines.append(f"{icon} {name}")
        if not (s or {}).get("ok"):
            lines.append(f"  unavailable ({(s or {}).get('error', 'no data')})\n")
            continue
        n = s.get("n_trades") or 0
        if not n:
            lines.append("  no closed trades yet\n")
            continue
        pf = s.get("profit_factor")
        streak = f"{s.get('streak_type') or ''}{s.get('streak') or 0}"
        week = sum(d.get("net") or 0.0 for d in (s.get("daily") or [])[-7:])
        lines += [
            f"  trades {n} · {s.get('wins')}W / {s.get('losses')}L → "
            f"{_n(s.get('win_rate'), 1)}%",
            f"  net {_pnl(s.get('net'))} USDT (fees in) · "
            f"PF {pf if pf is not None else '—'}",
            f"  avg win {_pnl(s.get('avg_win'))} · avg loss {_pnl(s.get('avg_loss'))}",
            f"  7d {_pnl(week)} · max DD {_pnl(s.get('max_drawdown'))} · "
            f"streak {streak}",
            "",
        ]
    lines.append("⚠ win rate alone means nothing — a 90% strategy loses money "
                 "when the 10% are big. Read it WITH profit factor and net.")
    return "\n".join(lines)


def fmt_positions(binance: dict, bybit: dict) -> str:
    lines = ["📌 OPEN POSITIONS\n"]
    for icon, name, snap in (("🟨", "Binance", binance), ("🟧", "Bybit", bybit)):
        lines.append(f"{icon} {name}")
        if not (snap or {}).get("ok", True) and not (snap or {}).get("positions"):
            lines.append(f"  unavailable ({(snap or {}).get('error', 'no data')})\n")
            continue
        poss = (snap or {}).get("positions") or []
        if not poss:
            lines.append("  flat\n")
            continue
        for p in poss[:10]:
            base = (p.get("symbol") or "?").split("/")[0]
            pct = p.get("pnl_pct")
            lines.append(f"  ▸ {base} {p.get('side')} · entry {_n(p.get('entry'))} "
                         f"· uPnL {_pnl(p.get('unrealized_pnl'))}"
                         + (f" ({_pnl(pct)}%)" if pct is not None else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_signals(payload: dict, limit: int = 5) -> str:
    sigs = (payload or {}).get("signals") or []
    if not sigs:
        return "No S2 signals fired in the last 24h."
    lines = [f"📊 LAST {min(limit, len(sigs))} S2 SIGNALS ({payload.get('timeframe', '15m')})\n"]
    for s in sigs[:limit]:
        age_min = int((time.time() - (s.get("ts") or 0)) / 60)
        age = f"{age_min}m" if age_min < 120 else f"{age_min // 60}h"
        arrow = "🟢" if s.get("direction") == "long" else "🔴"
        lines.append(f"{arrow} {s.get('base')} {str(s.get('direction', '')).upper()} · "
                     f"{s.get('score')}/100 · {age} ago")
        if s.get("entry") and s.get("sl") and s.get("tp2"):
            lines.append(f"   entry {s['entry']:,.6g} · SL {s['sl']:,.6g} · "
                         f"TP1 {s.get('tp1', 0):,.6g} · TP2 {s['tp2']:,.6g}")
    return "\n".join(lines)


def fmt_alerts(alerts: list) -> str:
    active = [a for a in alerts if not a.get("triggered")]
    fired = [a for a in alerts if a.get("triggered")]
    if not alerts:
        return "🔔 No price alerts set — add them on the dashboard."
    lines = ["🔔 PRICE ALERTS\n"]
    for a in active:
        lines.append(f"  ▸ {a['base']} {'▲ above' if a['direction'] == 'above' else '▼ below'} "
                     f"{a['price']:,.6g}")
    for a in fired[:5]:
        lines.append(f"  ✓ {a['base']} fired @ {a.get('triggered_price', 0):,.6g}")
    return "\n".join(lines)


HELP = ("🤖 Commands\n"
        "/winrate — win-rate report from real Binance + Bybit records\n"
        "/positions — open positions on both accounts\n"
        "/signals — last fired S2 signals with Entry/SL/TP\n"
        "/alerts — price alerts currently armed\n"
        "/report — today's account+market report now\n"
        "/tw — latest 台股 scan (大盤 regime + setups)\n"
        "/twnow — 台股即時: TAIEX + 漲跌幅前三 + 追蹤設定現價\n"
        "/liq — BTC/ETH 清算: 24h統計 + 最近清算價 + 🧲清算地圖\n"
        "/clean [小時] — 刪除 bot 超過N小時的舊訊息 (預設24, 上限47, 限管理員)\n"
        "/cleanall — 一次清掉記錄功能上線前的全部舊訊息 (限管理員, 需確認)\n"
        "/help — this list")


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


# ── command dispatch ─────────────────────────────────────────────────────────
def handle(cmd: str, args: str = "") -> str:
    """Command name (+ raw args) → reply text. Import-inside so one broken
    dependency degrades that command, not the whole bot."""
    if cmd in ("winrate", "stats", "wr"):
        import executor
        import strategy3_exec
        return fmt_winrate(executor.realized_pnl_summary(),
                           strategy3_exec.closed_pnl_summary())
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
    if cmd in ("liq", "liquidations"):
        import liq_alerts
        return liq_alerts.build_report()
    if cmd in ("tw", "twstocks"):
        import tw_stocks
        st = tw_stocks._load_state()
        return (st.get("last_digest_text")
                or "尚未有台股掃描 — 每個交易日 14:00 (台北) 自動發送。")
    if cmd in ("twnow", "twlive"):
        import tw_intraday
        return tw_intraday.snapshot_text()
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


def _reply(chat_id, thread_id, text) -> bool:
    """Chunked, 429-aware reply. Returns whether every part was delivered —
    a rate-limited reply used to be dropped silently while the log said
    'answered' (the same trap fixed in telegram_utils on 2026-07-12)."""
    payload = {"chat_id": chat_id}
    if thread_id:
        payload["message_thread_id"] = thread_id
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    ok = True
    for part in telegram_utils._chunks_of(text):
        for attempt in (0, 1):
            r = requests.post(url, data={**payload, "text": part}, timeout=15)
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
            ok = ok and r.ok
            break
    return ok


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
                           allowed_updates='["message"]').get("result") or []
        except Exception as exc:  # noqa: BLE001 — network blip: back off, retry
            print(f"[tgcmd] poll error: {exc}")
            time.sleep(10)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            msg = up.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            parsed = parse_command(msg.get("text") or "")
            if not parsed or not allowed(chat_id):
                continue
            cmd, args = parsed
            # remember the user's /command message too, so /clean sweeps it
            telegram_utils._record_sent(chat_id, msg.get("message_id"), bot="main")
            if not authorized(cmd, (msg.get("from") or {}).get("id")):
                _reply(chat_id, msg.get("message_thread_id"),
                       f"⛔ /{cmd} 只有群組管理員可以使用")
                print(f"[tgcmd] denied /{cmd} from non-admin")
                continue
            try:
                reply = handle(cmd, args)
            except Exception as exc:  # noqa: BLE001 — a broken handler must answer, not die
                reply = f"⚠ {cmd} failed: {str(exc)[:200]}"
            if reply:
                try:
                    delivered = _reply(chat_id, msg.get("message_thread_id"), reply)
                    print(f"[tgcmd] {'answered' if delivered else 'REPLY DROPPED'} /{cmd}")
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
