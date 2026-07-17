"""
Event Radar — big market-moving events → the Telegram "events" channel.

Three detectors, all free / no API key, run once per Strategy-2 sweep (~5 min):

  1. Breaking news  — high-impact keyword match over the Market-Intel RSS pool
     plus two general-market CNBC feeds: Fed/FOMC/CPI prints, war/geopolitics,
     SEC/regulation, hacks/insolvencies, whale flows.
  2. Macro calendar — one pre-alert ~45 min before each high-impact USD event
     (FOMC, CPI, NFP…) from the ForexFactory weekly file market_intel already
     pulls for the /market page.
  3. Market shock   — BTC/ETH 5m candles: a 15-minute move beyond ±1.5% (or 1h
     beyond ±2.5%) alerts immediately. This is the honest no-key proxy for
     whale dumps/pumps and for news that hasn't reached the feeds yet.

Every alert punches through quiet mode (force=True) on channel="events" — the
🌍 Events topic in the group. State (seen headlines, alerted calendar events,
shock cooldowns) persists in event_radar_state.json so a restart never
re-alerts old news; the very first run only SEEDS the seen-set silently.
"""
import json
import os
import re
import time

import market_intel
import telegram_utils

STATE_FILE = os.path.join(os.path.dirname(__file__), "event_radar_state.json")

# ── tunables (env-overridable) ───────────────────────────────────────────────
FRESH_SEC = int(os.getenv("EVENT_FRESH_SEC", "7200"))           # headline max age
MAX_PER_TICK = int(os.getenv("EVENT_MAX_PER_TICK", "4"))        # news-alert storm cap
CAL_LEAD_SEC = int(os.getenv("EVENT_CAL_LEAD_SEC", "2700"))     # calendar pre-alert (45 min)
SHOCK_15M_PCT = float(os.getenv("EVENT_SHOCK_15M_PCT", "1.5"))  # |15m %| on BTC/ETH
SHOCK_1H_PCT = float(os.getenv("EVENT_SHOCK_1H_PCT", "2.5"))    # |1h %| on BTC/ETH
SHOCK_COOLDOWN_SEC = int(os.getenv("EVENT_SHOCK_COOLDOWN_SEC", "7200"))
SHOCK_SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
SEEN_RETAIN_SEC = 3 * 86400

# General-market feeds on top of the crypto pool — Fed/war headlines usually
# hit these before the crypto press rewrites them. Parsed with the same
# market_intel RSS parser; a dead feed is skipped silently.
EXTRA_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
]

# ── high-impact headline categories (first match wins) ──────────────────────
# Deliberately conservative: word-boundary regexes over clearly market-moving
# terms. The RSS pool is finance-only, so these fire a few times a day at most;
# MAX_PER_TICK + the seen-set keep a hot news day from becoming a TG storm.
_CATEGORIES = [
    ("🏛 聯準會 / 總經", re.compile(
        r"\b(fed|fomc|powell|rate (cut|hike|decision)|interest rates?|cpi|"
        r"inflation (data|report|print|surges?|cools?)|nonfarm|nfp|jobs report|"
        r"unemployment|recession|gdp|tariffs?|trade war|debt ceiling|"
        r"government shutdown|treasury yields?|quantitative (easing|tightening)|"
        r"stimulus|rate decision)\b", re.I)),
    ("⚔️ 地緣政治", re.compile(
        r"\b(war|invasion|invades?|missiles?|airstrikes?|air strikes?|nuclear|"
        r"sanctions?|ceasefire|military (action|operation|strike)|escalation|"
        r"coup|state of emergency|martial law)\b", re.I)),
    ("⚖️ 監管政策", re.compile(
        r"\b(sec (approves?|rejects?|sues?|charges?|delays?)|etf "
        r"(approval|approved|rejected|denied|launch)|cftc|doj |lawsuit against|"
        r"crypto (ban|bill|law|act)|stablecoin (bill|act|law)|executive order|"
        r"crackdown|mica)\b", re.I)),
    ("🚨 駭客 / 風險", re.compile(
        r"\b(hacked?|exploits?|exploited|stolen|drained|breach|rug pull|"
        r"insolven(t|cy)|bankruptc?y|defaults? on|halts? withdrawals|"
        r"depegs?|depegged)\b", re.I)),
    ("🐋 巨鯨動向", re.compile(
        r"\b(whales?|dormant wallet|mt\.? gox|large (transfer|holders?)|"
        r"(buys?|adds?|sells?|dumps?) \$?\d+[\d,.]* ?(billion|million|[bm]) "
        r"(in |of |worth )?(btc|bitcoin|eth|ethereum)|microstrategy (buys|adds)|"
        r"treasury (buys|adds) (btc|bitcoin))\b", re.I)),
]


def categorize(title: str):
    """First matching high-impact category label for a headline, else None."""
    for label, rx in _CATEGORIES:
        if rx.search(title or ""):
            return label
    return None


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = start fresh
        return {}


def _save_state(state: dict) -> None:
    cutoff = time.time() - SEEN_RETAIN_SEC
    state["seen"] = {k: v for k, v in (state.get("seen") or {}).items() if v >= cutoff}
    state["cal"] = {k: v for k, v in (state.get("cal") or {}).items() if v >= cutoff}
    # Sent alerts, newest first — the /market page's "Recent events" card
    # mirrors these so the web shows what Telegram got.
    state["recent"] = [r for r in (state.get("recent") or [])
                       if r.get("ts", 0) >= cutoff][:30]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _headline_key(item: dict) -> str:
    # Title-based (not link-based): the same story syndicated by two feeds
    # should still only alert once.
    return re.sub(r"\W+", "", (item.get("title") or "").lower())[:120]


# ── detectors (pure given their inputs — unit-testable without network) ─────
def _news_alerts(items: list, state: dict, now: float) -> list:
    """New, fresh, high-impact headlines → alert strings. Mutates state."""
    seen = state.setdefault("seen", {})
    seeding = not state.get("seeded")
    out = []
    for it in items:
        key = _headline_key(it)
        if not key or key in seen:
            continue
        seen[key] = now
        if seeding:
            continue                        # first ever run: learn, don't alert
        cat = categorize(it.get("title") or "")
        if not cat:
            continue
        pub = market_intel._pub_ts(it)
        if pub and pub < now - FRESH_SEC:
            continue                        # feed backfill, not breaking news
        out.append(f"🌍 重大事件 · {cat}\n"
                   f"{it.get('title')}\n"
                   f"來源 {it.get('source')} · {it.get('link')}")
        if len(out) >= MAX_PER_TICK:
            break
    state["seeded"] = True
    return out


def _calendar_alerts(events: list, state: dict, now: float) -> list:
    """One pre-alert per high-impact USD event, CAL_LEAD_SEC before it."""
    done = state.setdefault("cal", {})
    out = []
    for ev in events:
        key = f"{ev.get('title')}|{ev.get('date')}"
        if key in done:
            continue
        ts = market_intel._pub_ts({"published": ev.get("date")})
        if not ts or not (0 < ts - now <= CAL_LEAD_SEC):
            continue
        done[key] = now
        mins = int((ts - now) // 60)
        extra = " · ".join(x for x in (
            f"預測 {ev['forecast']}" if ev.get("forecast") else "",
            f"前值 {ev['previous']}" if ev.get("previous") else "") if x)
        out.append(f"🗓 總經數據 · {mins} 分鐘後 · {ev.get('title')}（美國）\n"
                   + (extra + "\n" if extra else "")
                   + "高影響數據即將公布 — 有槓桿部位的注意波動。")
    return out


def _shock_alerts(client, state: dict, now: float) -> list:
    """BTC/ETH sudden-move detector on closed 5m candles."""
    cool = state.setdefault("shock", {})
    out = []
    for sym in SHOCK_SYMBOLS:
        if now - cool.get(sym, 0) < SHOCK_COOLDOWN_SEC:
            continue
        try:
            o = client.call("fetch_ohlcv", sym, "5m", None, 16)
        except Exception:  # noqa: BLE001 — one symbol failing must not kill the tick
            continue
        closes = [float(c[4]) for c in (o[:-1] if o else [])]   # closed candles only
        if len(closes) < 13:
            continue
        chg_15m = (closes[-1] / closes[-4] - 1) * 100
        chg_1h = (closes[-1] / closes[-13] - 1) * 100
        if abs(chg_15m) >= SHOCK_15M_PCT or abs(chg_1h) >= SHOCK_1H_PCT:
            cool[sym] = now
            base = sym.split("/")[0]
            arrow = "🟢" if (chg_15m + chg_1h) > 0 else "🔴"
            out.append(f"💥 行情劇變 · {arrow} {base}\n"
                       f"15 分鐘 {chg_15m:+.1f}% · 1 小時 {chg_1h:+.1f}% "
                       f"@ {closes[-1]:,.6g}\n"
                       f"突發劇烈波動 — 留意新聞面／清算連鎖，槓桿部位先檢查。")
    return out


# ── orchestrator (called once per scanner sweep) ─────────────────────────────
def _fetch_news_items() -> list:
    items = list(market_intel.news().get("items") or [])
    for source, url in EXTRA_FEEDS:
        try:
            xml_text = market_intel._get_text(url, timeout=8.0)
            items.extend(market_intel._parse_rss(source, xml_text, 10))
        except Exception:  # noqa: BLE001 — a dead general feed is not an error
            continue
    return items


def tick(client) -> int:
    """Run all detectors once; returns how many alerts were sent."""
    now = time.time()
    state = _load_state()
    alerts = []
    try:
        alerts += _news_alerts(_fetch_news_items(), state, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[events] news detector error: {exc}")
    try:
        alerts += _calendar_alerts(market_intel.econ_calendar().get("events") or [],
                                   state, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[events] calendar detector error: {exc}")
    try:
        alerts += _shock_alerts(client, state, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[events] shock detector error: {exc}")

    sent = 0
    for msg in alerts:
        try:
            if telegram_utils.send_message(msg, force=True, channel="events"):
                sent += 1
            print(f"[events] {msg.splitlines()[0]}")
        except Exception as exc:  # noqa: BLE001 — an alert must never kill the loop
            print(f"[events] send failed: {exc}")
        state.setdefault("recent", []).insert(0, {"ts": now, "text": msg})
    _save_state(state)
    return sent
