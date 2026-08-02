"""
🐳 Whale discovery — find followable Hyperliquid whales instead of hand-picking.

whale_tracker follows a curated address list. Curating it by hand was the
bottleneck: addresses had to come from screenshots or third-party sites, and a
wrong address silently reports someone else's positions.

Hyperliquid publishes its own leaderboard as a plain GET:
    https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
~41k rows of {ethAddress, accountValue, windowPerformances[day/week/month/
allTime] = {pnl, roi, vlm}}. (It only answers GET — POSTing to the info API
with {"type":"leaderboard"} returns 422, which is what made this look
unavailable earlier.)

Ranking by PnL alone picks the WRONG accounts. Two failure modes, both real and
both filtered here:

  1. Market makers. The #1 account by equity ($59M) has $119B of all-time volume
     and a NEGATIVE $3M all-time PnL. It quotes both sides all day; its position
     is inventory, not a view. Following it is noise.
     → filtered by edge = allTime.pnl / allTime.vlm. A MM sits at ~0.

  2. Airdrop holders. Accounts with ~$0 volume and $445M "PnL" — that money came
     from holding HYPE, not from trading. They show edge ratios in the millions
     of percent and never trade again.
     → filtered by a minimum volume AND an edge CEILING.

What survives is an account that (a) has real money at risk, (b) actually
trades, (c) made money doing it, and (d) is still active this month. Then every
candidate is verified against clearinghouseState — no address is ever suggested
without confirming it holds a live position right now, because an address with
no positions produces no alerts.

What this deliberately does NOT do: claim a win rate, or claim these traders
will keep winning. Past PnL on a leaderboard is survivorship-heavy — the same
40k rows contain everyone who blew up and stopped trading. This ranks who is
big, active and historically profitable. That is a watchlist, not a signal.
"""
import json
import os
import time

import requests

import whale_tracker

LEADERBOARD_URL = os.getenv(
    "WHALE_LEADERBOARD_URL",
    "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_DIR, "whale_candidates.json")

# The payload is ~33MB, so it is fetched rarely and only the distilled shortlist
# is cached. Discovery is a manual/periodic action, never part of the hot loop.
CACHE_TTL = int(os.getenv("WHALE_DISCOVER_TTL", str(6 * 3600)))
HTTP_TIMEOUT = float(os.getenv("WHALE_DISCOVER_TIMEOUT", "90"))

MIN_EQUITY = float(os.getenv("WHALE_MIN_EQUITY", "1000000"))      # skin in the game
MIN_VOLUME = float(os.getenv("WHALE_MIN_VOLUME", "50000000"))     # actually trades
MIN_EDGE = float(os.getenv("WHALE_MIN_EDGE", "0.002"))            # 0.2% — above MM noise
MAX_EDGE = float(os.getenv("WHALE_MAX_EDGE", "0.50"))             # 50% — below "not trading PnL"
# A live book worth alerting on. Matches whale_tracker's own dust filter.
MIN_LIVE_USD = float(os.getenv("WHALE_MIN_LIVE_USD", "250000"))
# How many of the ranked shortlist to verify against clearinghouseState. Each is
# one API call; verifying all 300+ would hammer Hyperliquid for no benefit.
VERIFY_TOP = int(os.getenv("WHALE_VERIFY_TOP", "60"))


def _window(row, name):
    for entry in row.get("windowPerformances") or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[0] == name:
            return entry[1] or {}
    return {}


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch_leaderboard(timeout=None):
    """Raw leaderboard rows, or [] if Hyperliquid is unreachable."""
    try:
        r = requests.get(LEADERBOARD_URL, timeout=timeout or HTTP_TIMEOUT)
        rows = (r.json() or {}).get("leaderboardRows")
    except Exception:  # noqa: BLE001 — discovery is optional, never fatal
        return []
    return rows if isinstance(rows, list) else []


def distill(rows):
    """Leaderboard rows → ranked directional-trader candidates.

    Every filter here exists to exclude a specific kind of account that looks
    good on a PnL sort but is useless to follow (see module docstring)."""
    out = []
    for row in rows or []:
        addr = str(row.get("ethAddress") or "").strip().lower()
        if not whale_tracker.valid_address(addr):
            continue
        all_t, month = _window(row, "allTime"), _window(row, "month")
        equity = _f(row.get("accountValue"))
        pnl, volume = _f(all_t.get("pnl")), _f(all_t.get("vlm"))
        month_vlm, month_pnl = _f(month.get("vlm")), _f(month.get("pnl"))
        if equity < MIN_EQUITY or volume < MIN_VOLUME or pnl <= 0:
            continue
        if month_vlm <= 0:          # dormant — would never fire an alert
            continue
        edge = pnl / volume
        if not (MIN_EDGE <= edge <= MAX_EDGE):
            continue
        out.append({
            "address": addr,
            "equity": equity,
            "pnl": pnl,
            "volume": volume,
            "edge": edge,
            "month_pnl": month_pnl,
            "month_volume": month_vlm,
            "display_name": row.get("displayName") or "",
        })
    out.sort(key=lambda c: -c["pnl"])
    return out


def verify(candidate):
    """Attach the candidate's live book. Mutates and returns the dict."""
    positions, account = whale_tracker.fetch_positions(candidate["address"])
    book = []
    if positions:
        for coin, p in positions.items():
            notional = abs(_f(p.get("notional")))
            if notional >= MIN_LIVE_USD:
                book.append({"coin": coin, "side": p.get("side"),
                             "notional": notional})
    book.sort(key=lambda b: -b["notional"])
    candidate["book"] = book
    candidate["live_usd"] = sum(b["notional"] for b in book)
    candidate["live_equity"] = account or candidate["equity"]
    return candidate


_SIDE_ZH = {"long": "多", "short": "空"}


def suggest_label(candidate):
    """A descriptive label from real numbers — never a win-rate claim.

    Names the account's largest live position because that is what the user
    will actually recognise in an alert, with equity as the size marker."""
    name = (candidate.get("display_name") or "").strip()
    equity = candidate.get("live_equity") or candidate.get("equity") or 0
    size = f"${equity / 1e6:.0f}M" if equity >= 1e6 else f"${equity / 1e3:.0f}K"
    if name:
        return f"{name} · {size}"
    book = candidate.get("book") or []
    if book:
        side = _SIDE_ZH.get(book[0].get("side"), "")
        return f"{book[0]['coin']}{side}巨鯨 · {size}"
    return f"巨鯨 · {size}"


def discover(limit=12, verify_top=None, rows=None, use_cache=True):
    """Top verified candidates, biggest live book first.

    Only addresses confirmed to hold a live position are returned."""
    cached = _read_cache() if use_cache else None
    if cached is not None and rows is None:
        ranked = cached
    else:
        ranked = distill(rows if rows is not None else fetch_leaderboard())
        top = ranked[:(verify_top or VERIFY_TOP)]
        ranked = [c for c in (verify(c) for c in top) if c.get("live_usd", 0) > 0]
        ranked.sort(key=lambda c: -c["live_usd"])
        if use_cache:
            _write_cache(ranked)
    tracked = {whale_tracker._norm(r.get("address"))
               for r in whale_tracker.load_addresses()}
    for c in ranked:
        c["tracked"] = c["address"] in tracked
    return ranked[:limit]


def _read_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        if time.time() - float(blob.get("ts") or 0) < CACHE_TTL:
            return blob.get("rows") or []
    except Exception:  # noqa: BLE001 — missing/corrupt cache → refetch
        pass
    return None


def _write_cache(rows):
    tmp = f"{CACHE_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "rows": rows}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Telegram surfaces ────────────────────────────────────────────────────────
def build_report(limit=10):
    """/whaletop — ranked candidates with their live books."""
    try:
        rows = discover(limit=limit)
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return "🐳 目前抓不到排行榜資料（Hyperliquid 可能暫時無回應），稍後再試。"
    out = ["🐳 <b>巨鯨候選名單</b>（Hyperliquid 官方排行榜）", ""]
    for i, c in enumerate(rows, 1):
        mark = "✅" if c.get("tracked") else f"{i}."
        book = " · ".join(
            f"{b['coin']}{_SIDE_ZH.get(b.get('side'), '')} "
            f"{whale_tracker._usd(b['notional'])}"
            for b in (c.get("book") or [])[:3]) or "—"
        out.append(
            f"{mark} <code>{whale_tracker._short(c['address'])}</code> "
            f"淨值 {whale_tracker._usd(c.get('live_equity') or c['equity'])} · "
            f"歷史損益 {whale_tracker._usd(c['pnl'])}")
        out.append(f"    {book}")
    out += ["", "✅ = 已在追蹤清單。用 /whaleadd &lt;地址&gt; 加入。",
            "⚠️ 這是「誰大、誰活躍、誰過去賺錢」的名單，不是勝率、也不是進場訊號。"]
    return "\n".join(out)


def sync(count=6, dry_run=False):
    """Add the top untracked verified candidates to the watch list."""
    rows = discover(limit=100)
    fresh = [c for c in rows if not c.get("tracked")][:count]
    if not fresh:
        return "🐳 沒有新的候選 —— 排行榜前段都已經在追蹤清單裡了。"
    lines = []
    for c in fresh:
        label = suggest_label(c)
        if dry_run:
            lines.append(f"• {whale_tracker._short(c['address'])} → {label}")
        else:
            lines.append(whale_tracker.add_address(c["address"], label).split("\n")[0])
    head = "🐳 預覽（未寫入）：" if dry_run else f"🐳 已加入 {len(fresh)} 個巨鯨："
    return head + "\n" + "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover — operator tool
    import sys
    if "--sync" in sys.argv:
        print(sync(count=int(os.getenv("N", "6")), dry_run="--dry" in sys.argv))
    else:
        for c in discover(limit=int(os.getenv("N", "20"))):
            book = " ".join(f"{b['coin']}{b['side']}${b['notional'] / 1e6:.1f}M"
                            for b in c["book"][:4])
            flag = "✓" if c["tracked"] else " "
            print(f"{flag} {c['address']} eq=${c['live_equity'] / 1e6:7.1f}M "
                  f"pnl=${c['pnl'] / 1e6:7.1f}M edge={100 * c['edge']:5.2f}% | {book}")
