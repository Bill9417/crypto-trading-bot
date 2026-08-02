"""
🐳 Whale position tracker — Hyperliquid public positions into the 清算 topic.

Follows a curated list of Hyperliquid addresses (the huge-size traders the user
watches on Coinglass) and alerts the moment one OPENS, CLOSES, FLIPS or
materially RESIZES a position — the "follow smart money" idea Coinglass sells,
built off Hyperliquid's free, fully public on-chain state (no API key).

Honest scope (same discipline as liq_alerts):
  • Position, size, entry, leverage, liquidation price and live unrealised PnL
    are REAL on-chain data, read straight from clearinghouseState.
  • Win rate is deliberately NOT computed. A whale's fills are full of TWAP
    sub-executions and partial closes, so a naive fills win rate is garbage
    (a test address printed a fake 100%). The alert links to the address's
    Coinglass page — that's where the curated "high win rate" claim lives, and
    the label is yours to annotate. We don't invent a number we can't stand behind.

Alerts are INFORMATION, not entry signals. A tracked whale can be very wrong:
the seed address was $6M underwater on a 50k-ETH short when this was written.

Change detection is state-diff, not time-based: a move alerts once, the new
state is saved, and it stays quiet until the position changes again. First time
an address is seen its positions are recorded silently (no backfill flood).

Commands: /whale shows every tracked whale's live book; /whaleadd <addr> [label]
and /whalerm <addr> curate the list (admin only). Address list persists in
whale_addresses.json, per-address last-state in whale_state.json.
"""
import json
import os
import re
import time

import requests

HL_API = "https://api.hyperliquid.xyz/info"
_DIR = os.path.dirname(os.path.abspath(__file__))
ADDR_FILE = os.path.join(_DIR, "whale_addresses.json")
STATE_FILE = os.path.join(_DIR, "whale_state.json")

# Only positions at/above this USD notional are worth an alert (dust filter).
MIN_NOTIONAL_USD = float(os.getenv("WHALE_MIN_NOTIONAL_USD", "250000"))
# A resize alerts only when the actual position SIZE (szi, not price-driven
# notional) moves at least this fraction vs the last alerted size.
RESIZE_FRAC = float(os.getenv("WHALE_RESIZE_FRAC", "0.35"))
# Don't poll faster than this (positions don't change by the second; be a good
# Hyperliquid citizen). The sweep calls tick() every pass; this self-paces it.
POLL_SEC = int(os.getenv("WHALE_POLL_SEC", "300"))
# Cap alerts per poll so a whale opening a whole basket at once can't burst.
ALERTS_PER_POLL = int(os.getenv("WHALE_ALERTS_PER_POLL", "6"))
HTTP_TIMEOUT = float(os.getenv("WHALE_HTTP_TIMEOUT", "12"))
# /whale report: a whale can hold 80+ tiny positions — show only the biggest
# few ≥ this floor so the report stays readable and under Telegram's limit.
REPORT_MAX_POS = int(os.getenv("WHALE_REPORT_MAX_POS", "6"))
REPORT_MIN_POS_USD = float(os.getenv("WHALE_REPORT_MIN_POS_USD", "250000"))
# /whale details this many books in full (biggest first) and folds the rest into
# a one-line tail. The consensus header already counts every tracked whale.
REPORT_MAX_WHALES = int(os.getenv("WHALE_REPORT_MAX_WHALES", "8"))

# Seed list — the address the user referenced PLUS eight validated whales picked
# from Hyperliquid's own leaderboard (stats-data.hyperliquid.xyz/Mainnet/
# leaderboard, 2026-07-17): all had a top-tier all-time PnL, a live position
# ≥$500k and were confirmed against clearinghouseState when added. Labels are
# memorable nicknames, NOT win-rate claims (we don't measure that — see the
# module docstring). Users curate freely with /whaleadd · /whalerm.
SEED_ADDRESSES = [
    {"address": "0x0ddf9bae2af4b874b96d287a5ad42eb47138a902", "label": "ETH 空頭巨鯨"},
    {"address": "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00", "label": "🏆 全時冠軍鯨"},
    {"address": "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36", "label": "巨鯨 · 大戶"},
    {"address": "0x7fdafde5cfb5465924316eced2d3715494c517d1", "label": "ETH 巨鯨"},
    {"address": "0xd47587702a91731dc1089b5db0932cf820151a91", "label": "活躍多倉鯨"},
    {"address": "0xa312114b5795dff9b8db50474dd57701aa78ad1e", "label": "波段巨鯨"},
    {"address": "0x8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05", "label": "ZEC 巨鯨"},
    {"address": "0x77375a8c9d13bf79afb2a87f1b0ac1dfd5f5bf66", "label": "AAVE 巨鯨"},
    {"address": "0x082e843a431aef031264dc232693dd710aedca88", "label": "HYPE 多單巨鯨"},
]

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_last_poll = 0.0


# ── address list ─────────────────────────────────────────────────────────────
def _norm(addr: str) -> str:
    return str(addr or "").strip().lower()


def valid_address(addr: str) -> bool:
    return bool(_ADDR_RE.match(_norm(addr)))


def load_addresses() -> list:
    """Curated whale list; seeds the file on first use so it's never empty."""
    try:
        with open(ADDR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except Exception:  # noqa: BLE001 — missing/corrupt → reseed
        pass
    _save_addresses(SEED_ADDRESSES)
    return list(SEED_ADDRESSES)


def _save_addresses(rows: list) -> None:
    tmp = ADDR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ADDR_FILE)


def add_address(address: str, label: str = "") -> str:
    if not valid_address(address):
        return f"⛔ 地址格式錯誤：{address}\n需要 0x 開頭的 42 字元 EVM 地址。"
    rows = load_addresses()
    a = _norm(address)
    if any(_norm(r.get("address")) == a for r in rows):
        return f"ℹ️ 已在追蹤清單：{_short(a)}"
    rows.append({"address": a, "label": (label or "").strip() or _short(a)})
    _save_addresses(rows)
    return f"✅ 已加入追蹤：{label or _short(a)}\n{_coinglass(a)}"


def remove_address(address: str) -> str:
    a = _norm(address)
    rows = load_addresses()
    kept = [r for r in rows if _norm(r.get("address")) != a]
    if len(kept) == len(rows):
        return f"找不到這個地址：{_short(a)}"
    _save_addresses(kept)
    # forget its saved state too, so re-adding starts clean
    st = _load_state()
    st.pop(a, None)
    _save_state(st)
    return f"🗑️ 已移除：{_short(a)}"


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ── Hyperliquid ──────────────────────────────────────────────────────────────
def fetch_positions(address: str):
    """(positions, account_value) for one address, or (None, 0.0) on any error.
    positions = {coin: {side, szi, entry, notional, upnl, lev, liq}}."""
    try:
        r = requests.post(HL_API, json={"type": "clearinghouseState",
                                        "user": _norm(address)}, timeout=HTTP_TIMEOUT)
        d = r.json() or {}
    except Exception:  # noqa: BLE001 — Hyperliquid down ≠ sweep down
        return None, 0.0
    out = {}
    for ap in d.get("assetPositions", []) or []:
        p = ap.get("position") or {}
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0:
            continue
        out[p.get("coin")] = {
            "side": "long" if szi > 0 else "short",
            "szi": szi,
            "entry": _f(p.get("entryPx")),
            "notional": _f(p.get("positionValue")),
            "upnl": _f(p.get("unrealizedPnl")),
            "lev": (p.get("leverage") or {}).get("value"),
            "liq": _f(p.get("liquidationPx")) or None,
        }
    acct = _f((d.get("marginSummary") or {}).get("accountValue"))
    return out, acct


# ── change detection ─────────────────────────────────────────────────────────
def diff_positions(prev: dict, cur: dict) -> list:
    """List of change events between a stored and a live position map. Each:
    {coin, kind, side, prev_side}. kind ∈ open/close/flip/add/trim."""
    events = []
    prev = prev or {}
    for coin, c in cur.items():
        if c["notional"] < MIN_NOTIONAL_USD and coin not in prev:
            continue                                   # ignore new dust
        p = prev.get(coin)
        if not p:
            events.append({"coin": coin, "kind": "open", "side": c["side"]})
        elif p.get("side") != c["side"]:
            events.append({"coin": coin, "kind": "flip",
                           "side": c["side"], "prev_side": p.get("side")})
        else:
            ps = abs(_f(p.get("szi")) or 0.0)
            cs = abs(c["szi"])
            if ps > 0 and abs(cs - ps) / ps >= RESIZE_FRAC:
                events.append({"coin": coin, "side": c["side"],
                               "kind": "add" if cs > ps else "trim"})
    for coin, p in prev.items():
        if coin not in cur:
            events.append({"coin": coin, "kind": "close", "side": p.get("side")})
    return events


# ── formatting ───────────────────────────────────────────────────────────────
def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _short(addr: str) -> str:
    a = _norm(addr)
    return f"{a[:6]}…{a[-4:]}" if len(a) >= 12 else a


def _coinglass(addr: str) -> str:
    return f"https://www.coinglass.com/hyperliquid/{_norm(addr)}"


def _usd(x) -> str:
    x = _f(x)
    a = abs(x)
    if a >= 1e9:
        return f"${x / 1e9:.2f}B"
    if a >= 1e6:
        return f"${x / 1e6:.2f}M"
    if a >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def _signed_usd(x) -> str:
    x = _f(x)
    return ("+" if x >= 0 else "−") + _usd(abs(x))


def _px(x) -> str:
    x = _f(x)
    if x == 0:
        return "—"
    if x >= 1000:
        s = f"{x:,.1f}"
    elif x >= 1:
        s = f"{x:,.3f}"
    else:
        return f"{x:.6g}"
    return s.rstrip("0").rstrip(".") if "." in s else s


_SIDE_ZH = {"long": "做多", "short": "做空"}
_KIND = {
    "open":  ("🟢", "新開"),
    "close": ("⚪", "平倉"),
    "flip":  ("🔄", "反手"),
    "add":   ("➕", "加倉"),
    "trim":  ("➖", "減倉"),
}


def build_alert(label: str, address: str, ev: dict, pos: dict, acct: float) -> str:
    """One 中文 whale-move card for the 清算 topic, in the shared house style
    (tg_format DIV + a monospace-aligned <pre> block). HTML parse_mode."""
    import tg_format
    coin = ev["coin"]
    icon, verb = _KIND.get(ev["kind"], ("🐳", ev["kind"]))
    side_zh = _SIDE_ZH.get(ev.get("side"), ev.get("side") or "")
    if ev["kind"] == "flip":
        action = (f"{icon} {verb} "
                  f"{_SIDE_ZH.get(ev.get('prev_side'), '?')}→{side_zh} {coin}")
    else:
        action = f"{icon} {verb} {side_zh} {coin}"

    lines = [f"🐳 {label}", action]
    p = pos.get(coin)
    if p and ev["kind"] != "close":
        r1 = f"部位 {_usd(p['notional'])}"
        if p.get("lev"):
            r1 += f" · {int(p['lev'])}x"
        r2 = f"進場 {_px(p['entry'])}"
        if p.get("liq"):
            r2 += f" · 清算 {_px(p['liq'])}"
        r3 = f"損益 {_signed_usd(p['upnl'])}"
        if acct:
            r3 += f" · 帳戶 {_usd(acct)}"
        lines += [tg_format.DIV, "<pre>" + "\n".join((r1, r2, r3)) + "</pre>",
                  tg_format.DIV]
    elif acct:
        lines.append(f"帳戶淨值 {_usd(acct)}")
    lines.append(f'📊 <a href="{_coinglass(address)}">Coinglass ↗</a>')
    lines.append("⚠️ 追蹤資訊，非投資建議")
    return "\n".join(lines)


def whale_consensus(books: list) -> list:
    """Aggregate net positioning across whale books → the coins ≥2 tracked whales
    hold, biggest net exposure first. `books` = [(label, addr, pos, acct), …].
    Returns display lines (empty if no coin has ≥2 whales on it)."""
    agg = {}   # coin -> [long_ct, short_ct, net_notional (long +, short −)]
    for _, _, pos, _ in books:
        for coin, p in (pos or {}).items():
            if p["notional"] < REPORT_MIN_POS_USD:       # ignore dust in the tally
                continue
            a = agg.setdefault(coin, [0, 0, 0.0])
            if p["side"] == "long":
                a[0] += 1
                a[2] += p["notional"]
            else:
                a[1] += 1
                a[2] -= p["notional"]
    shared = [(c, v) for c, v in agg.items() if v[0] + v[1] >= 2]
    shared.sort(key=lambda cv: -abs(cv[1][2]))
    out = []
    for coin, (lc, sc, net) in shared[:6]:
        lean = "淨多" if net > 0 else "淨空" if net < 0 else "對半"
        parts = []
        if lc:
            parts.append(f"{lc}多")
        if sc:
            parts.append(f"{sc}空")
        out.append(f"  {coin} — {' '.join(parts)} · {lean} {_usd(abs(net))}")
    return out


def build_report() -> str:
    """/whale — a whale-consensus header + every tracked whale's live book."""
    whales = load_addresses()
    books = []
    for w in whales:
        addr = w.get("address")
        label = (w.get("label") or "").strip() or _short(addr)
        pos, acct = fetch_positions(addr)
        books.append((label, addr, pos, acct))

    import tg_format
    lines = [f"🐳 巨鯨追蹤 · 共 {len(whales)} 個地址"]
    consensus = whale_consensus(books)
    if consensus:
        lines.append("\n🧭 巨鯨共識 (≥2 個地址同標的)")
        lines.append("<pre>" + tg_format.esc("\n".join(r.strip() for r in consensus))
                     + "</pre>")
    lines.append("")
    # Biggest book first, and cap the detail: the consensus block above is the
    # part that generalises, and 15 full books blow past Telegram's 4096 chars
    # (chunking would split one /whale reply into two messages).
    books.sort(key=lambda b: -sum(p["notional"] for p in (b[2] or {}).values()))
    hidden = books[REPORT_MAX_WHALES:]
    for label, addr, pos, acct in books[:REPORT_MAX_WHALES]:
        cg = f'📊 <a href="{_coinglass(addr)}">Coinglass ↗</a>'
        if pos is None:
            lines.append(f"• {tg_format.esc(label)} — 查詢失敗 · {cg}")
            continue
        if not pos:
            lines.append(f"• {tg_format.esc(label)} — 空手 (淨值 {_usd(acct)}) · {cg}")
            continue
        lines.append(f"• {tg_format.esc(label)} — 淨值 {_usd(acct)} · {cg}")
        big = [(c, p) for c, p in pos.items() if p["notional"] >= REPORT_MIN_POS_USD]
        big.sort(key=lambda cp: -cp[1]["notional"])
        rows = []
        for coin, p in big[:REPORT_MAX_POS]:
            arrow = "🟢" if p["side"] == "long" else "🔴"
            rows.append((f"{arrow} {_SIDE_ZH[p['side']]}", coin,
                         _usd(p["notional"]), f"@ {_px(p['entry'])}",
                         f"{int(p['lev'])}x" if p.get("lev") else "",
                         _signed_usd(p["upnl"])))
        if rows:
            lines.append(tg_format.pre_table(rows, align="llrlrr"))
        extra = len(big) - REPORT_MAX_POS
        if extra > 0:
            lines.append(f"…還有 {extra} 個 ≥{_usd(REPORT_MIN_POS_USD)} 部位")
        elif not big:
            lines.append("(無 ≥$250k 部位，多為小倉)")
    if hidden:
        rest = _usd(sum(p["notional"] for _, _, pos, _ in hidden
                        for p in (pos or {}).values()))
        lines.append(f"\n…另外 {len(hidden)} 個較小的巨鯨（合計 {rest}）已計入上方共識")
    lines.append("\n⚠️ 追蹤資訊，非投資建議")
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────
def tick(client=None) -> int:
    """Poll every tracked whale, alert on changes. Self-paced to POLL_SEC.
    Returns how many alerts were sent. Never raises (sweep must survive)."""
    global _last_poll
    now = time.time()
    if now - _last_poll < POLL_SEC:
        return 0
    _last_poll = now

    import telegram_utils
    whales = load_addresses()
    state = _load_state()
    sent = 0
    for w in whales:
        addr = _norm(w.get("address"))
        if not valid_address(addr):
            continue
        label = (w.get("label") or "").strip() or _short(addr)
        pos, acct = fetch_positions(addr)
        if pos is None:                       # API blip — keep old state, retry next tick
            continue
        # entry/lev/liq ride along so the dashboard can show a whale's cost
        # basis and compute a LIVE P&L without spending a Hyperliquid call.
        # Only side and szi drive change detection (see diff_positions) — the
        # extra keys are inert to it.
        cur = {c: {"side": v["side"], "szi": v["szi"], "entry": v.get("entry"),
                   "lev": v.get("lev"), "liq": v.get("liq")}
               for c, v in pos.items()}

        if addr not in state:                 # first sight → seed silently, no flood
            state[addr] = cur
            continue

        events = diff_positions(state[addr], pos)
        for ev in events:
            if sent >= ALERTS_PER_POLL:
                break
            msg = build_alert(label, addr, ev, pos, acct)
            if telegram_utils.send_message(msg, parse_mode="HTML",
                                           force=True, channel="liq"):
                sent += 1
                print(f"[whale] {label} {ev['kind']} {ev.get('side','')} {ev['coin']}")
        state[addr] = cur                     # advance baseline whether or not we alerted

    _save_state(state)
    return sent
