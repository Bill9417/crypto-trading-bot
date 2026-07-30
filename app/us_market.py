"""🇺🇸 美股收盤 — last night's US session, in Chinese, before the TWSE open.

The companion to tw_stocks.py (14:00 台股 scan) at the other end of the day:
one message per TWSE trading morning describing the US session that just
closed, because that is what actually sets the tone for 台股's 09:00 open.

WHEN IT FIRES. The US closes 16:00 New York = 04:00 台北 (05:00 in winter) —
a notification nobody wants. So the data is not captured at the close; it is
simply READ inside a window starting at SEND_HOUR (08:00–13:00 台北, i.e. before
the TWSE open), by which point Yahoo's `regularMarketPrice` still holds the
final regular-session close: 08:00 台北 is 20:00 New York, comfortably after
the close and long before the next pre-market opens at 04:00 ET.

That window is BOUNDED at both ends, and `session_complete()` re-checks the
data itself before anything is sent. Both exist because of one incident: a
scanner restarted at 23:44 台北 on 2026-07-30 hit an unbounded `hour >= 8`
gate and published live 11:44 ET intraday prices under a 美股收盤 header.
Yahoo's `regularMarketTime` tracks 'now' while a market is open, so nothing in
the PRICES themselves reveals that they are mid-session — only the timestamp
does. Never widen the window past ~16:00 台北; New York pre-market opens then.

WHAT IT SAYS. Four indices + VIX + the 10-year yield, then the two things a
Taiwanese reader actually opens the message for: 台積電's ADR (1 ADR = 5 台股
shares, so it prints an 約當台股 price and the premium/discount to yesterday's
2330 close) and 費半 (SOX) relative to the S&P, which is the cleanest read on
whether 台股電子 has a tailwind. Breadth and the biggest movers come from the
same US100 list the /stocks page already caches.

The 💡 line is assembled from thresholds on those numbers — descriptive, never
predictive, and never a "buy" call. Same honesty rule as every other outward
message here.

Which session? Whatever Yahoo reports as the last regular close, keyed by its
New York date. That makes US holidays self-handling: no new session date means
no new numbers, and the message says 休市 instead of repeating yesterday's.
Monday morning 台北 therefore reports Friday's close, which is correct — it is
the freshest US read before Monday's TW open.

Failure-safe throughout: a Yahoo outage logs and retries on the next sweep,
never raises into the scanner loop.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Taipei")
TZ_NY = ZoneInfo("America/New_York")

STATE_FILE = os.path.join(os.path.dirname(__file__), "us_market_state.json")

SEND_HOUR = int(os.getenv("US_CLOSE_SEND_HOUR", "8"))   # 台北 local, pre-open
# The window is BOUNDED on purpose — see _due(). 08:00–13:00 台北 by default:
# late enough that last night's close is final, early enough to still be
# useful before the TWSE close, and nowhere near a live New York session.
SEND_WINDOW_HOURS = int(os.getenv("US_CLOSE_SEND_WINDOW_HOURS", "5"))
CLOSE_HOUR_NY = 16              # 16:00 ET — the regular-session close
RETRY_SEC = 900                 # min gap between failed fetch attempts
_TIMEOUT = 15
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"}

_WD = "一二三四五六日"

# (Yahoo symbol, 中文 label) — order is the order printed.
INDICES = [
    ("^DJI", "道瓊"),
    ("^GSPC", "標普500"),
    ("^IXIC", "那斯達克"),
    ("^SOX", "費城半導體"),
]
VIX = "^VIX"
TNX = "^TNX"            # CBOE 10-year yield index — already a percentage
ADR = "TSM"             # 台積電 ADR
TSMC = "2330.TW"
FX = "TWD=X"            # USD/TWD
ADR_RATIO = 5           # 1 TSM ADR = 5 ordinary 2330 shares

ALL_SYMBOLS = [s for s, _ in INDICES] + [VIX, TNX, ADR, TSMC, FX]

MAX_MOVERS = 3          # 領漲/領跌 shown per side


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
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── formatting helpers (pure) ────────────────────────────────────────────────
def _num(v) -> str:
    """Index-level number: no decimals in the thousands, more as it gets small."""
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _arrow(v) -> str:
    if v is None:
        return "・"
    return "▲" if v > 0 else ("▼" if v < 0 else "―")


def _chg_pct(price, prev):
    if not price or not prev:
        return None
    return round((price - prev) / prev * 100, 2)


# ── fetch ────────────────────────────────────────────────────────────────────
def fetch_quotes(symbols=None) -> dict:
    """{symbol: {'price', 'prev', 'ts', 'chg_pct'}} from ONE Yahoo spark call.

    Indices, an ADR, a TW listing and an FX pair all live on the same endpoint,
    so the whole message costs a single request. Symbols Yahoo omits are simply
    absent from the result — every consumer below treats missing as unknown
    rather than assuming zero.
    """
    symbols = symbols or ALL_SYMBOLS
    r = requests.get(
        "https://query1.finance.yahoo.com/v7/finance/spark",
        params={"symbols": ",".join(symbols), "range": "1d", "interval": "15m"},
        headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    out = {}
    for item in (r.json().get("spark", {}) or {}).get("result") or []:
        meta = ((item.get("response") or [{}])[0] or {}).get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        out[item.get("symbol")] = {
            "price": price,
            "prev": prev,
            "ts": meta.get("regularMarketTime"),
            "chg_pct": _chg_pct(price, prev),
        }
    return out


def session_date(quotes: dict) -> str:
    """New York date ('YYYY-MM-DD') of the regular close being reported, from
    the S&P's own quote timestamp. '' when unavailable — the caller then sends
    nothing rather than guessing which session it is describing."""
    ts = (quotes.get("^GSPC") or {}).get("ts")
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, TZ_NY).strftime("%Y-%m-%d")


def session_complete(quotes: dict, now_ny=None) -> bool:
    """True only when the session being quoted has actually FINISHED.

    Yahoo's `regularMarketTime` tracks 'now' while a market is open and pins to
    the close once it isn't, so a mid-session read is indistinguishable from a
    close by PRICE alone. This is the check that tells them apart, and it is
    load-bearing: on 2026-07-30 a scanner restart at 23:44 台北 published live
    11:44 ET prices under a 美股收盤 header.

    It asks "has 16:00 ET on the quote's own date passed in real time?" rather
    than "is the timestamp itself at/after 16:00?". The latter would wrongly
    reject early closes (the day after Thanksgiving, 24 December), which stamp
    13:00 ET and are final regardless.
    """
    ts = (quotes.get("^GSPC") or {}).get("ts")
    if not ts:
        return False
    now_ny = now_ny or datetime.now(TZ_NY)
    close = datetime.fromtimestamp(ts, TZ_NY).replace(
        hour=CLOSE_HOUR_NY, minute=0, second=0, microsecond=0)
    return now_ny >= close


def breadth(rows: list) -> dict:
    """Advance/decline + the biggest movers across the cached US100 list.
    Rows with no change_pct (a symbol Yahoo dropped) are excluded from BOTH
    the counts and the movers so the two never disagree.

    領漲/領跌 are filtered by SIGN, not just sliced off each end of the ranking:
    on a day where everything rose, the bottom three are still gainers, and
    slicing would print them under 領跌."""
    scored = [r for r in (rows or []) if r.get("change_pct") is not None]
    ranked = sorted(scored, key=lambda r: r["change_pct"], reverse=True)
    return {
        "up": sum(1 for r in scored if r["change_pct"] > 0),
        "down": sum(1 for r in scored if r["change_pct"] < 0),
        "flat": sum(1 for r in scored if r["change_pct"] == 0),
        "total": len(scored),
        "gainers": [r for r in ranked if r["change_pct"] > 0][:MAX_MOVERS],
        "losers": [r for r in reversed(ranked) if r["change_pct"] < 0][:MAX_MOVERS],
    }


def adr_view(quotes: dict) -> dict:
    """台積電 ADR translated into 台股 terms: 約當台股價 = ADR × USD/TWD ÷ 5,
    and its premium/discount to 2330's own last close. {} when any of the three
    legs is missing — a half-computed premium would be worse than none."""
    adr = quotes.get(ADR) or {}
    fx = (quotes.get(FX) or {}).get("price")
    tw = (quotes.get(TSMC) or {}).get("price")
    if not (adr.get("price") and fx and tw):
        return {}
    equiv = adr["price"] * fx / ADR_RATIO
    return {
        "adr": adr["price"],
        "adr_pct": adr.get("chg_pct"),
        "equiv": equiv,
        "tw_close": tw,
        "premium_pct": round((equiv - tw) / tw * 100, 1),
    }


def build_snapshot(quotes: dict, us_rows: list) -> dict:
    """Everything the two message builders need, computed once."""
    idx = []
    for sym, label in INDICES:
        q = quotes.get(sym) or {}
        idx.append({"symbol": sym, "label": label,
                    "price": q.get("price"), "chg_pct": q.get("chg_pct")})
    vix = quotes.get(VIX) or {}
    tnx = quotes.get(TNX) or {}
    return {
        "session": session_date(quotes),
        "indices": idx,
        "vix": {"price": vix.get("price"), "chg_pct": vix.get("chg_pct")},
        # The 10-year is quoted IN percent, so its day-over-day move is stated
        # in percentage POINTS (+0.06) — a "+1.3%" here would read as a yield.
        "tnx": {"price": tnx.get("price"),
                "chg_pp": (round(tnx["price"] - tnx["prev"], 3)
                           if tnx.get("price") and tnx.get("prev") else None)},
        "adr": adr_view(quotes),
        "breadth": breadth(us_rows),
    }


# ── the 💡 read (deterministic, descriptive) ─────────────────────────────────
def read_line(snap: dict) -> str:
    """One plain-Chinese sentence assembled from thresholds on the numbers
    above. Deliberately describes what HAPPENED (and what 費半 historically
    leads for 台股電子) — it never forecasts, never says buy or sell, and never
    quotes a hit rate. Returns '' when the S&P itself is missing."""
    by_sym = {i["symbol"]: i for i in snap.get("indices", [])}
    spx = (by_sym.get("^GSPC") or {}).get("chg_pct")
    sox = (by_sym.get("^SOX") or {}).get("chg_pct")
    if spx is None:
        return ""

    if spx >= 1.0:
        head = f"美股收紅、漲勢明顯（標普 {spx:+.2f}%）"
    elif spx >= 0.3:
        head = f"美股收紅（標普 {spx:+.2f}%）"
    elif spx > -0.3:
        head = f"美股平盤震盪（標普 {spx:+.2f}%）"
    elif spx > -1.0:
        head = f"美股收黑（標普 {spx:+.2f}%）"
    else:
        head = f"美股收黑、跌幅較重（標普 {spx:+.2f}%）"
    parts = [head]

    if sox is not None:
        diff = sox - spx
        if diff >= 0.5:
            parts.append(f"費半 {sox:+.2f}% 強於大盤，台股電子今日氣氛偏強")
        elif diff <= -0.5:
            parts.append(f"費半 {sox:+.2f}% 弱於大盤，台股電子今日恐承壓")
        else:
            parts.append(f"費半 {sox:+.2f}%，與大盤同步")

    vix, vix_chg = snap["vix"]["price"], snap["vix"]["chg_pct"]
    if vix is not None:
        if vix >= 25:
            parts.append(f"VIX {vix:.1f} 偏高，波動仍大")
        elif vix_chg is not None and vix_chg <= -5:
            parts.append(f"VIX 回落至 {vix:.1f}，情緒轉穩")
        elif vix_chg is not None and vix_chg >= 5:
            parts.append(f"VIX 升至 {vix:.1f}，避險情緒升溫")

    return "；".join(parts) + "。"


# ── messages ─────────────────────────────────────────────────────────────────
def _session_label(snap: dict, now) -> str:
    """'07-29（週三）' for the NY session, falling back to the 台北 date."""
    sess = snap.get("session")
    if not sess:
        return now.strftime("%m-%d")
    d = datetime.strptime(sess, "%Y-%m-%d")
    return f"{d.strftime('%m-%d')}（週{_WD[d.weekday()]}）"


def build_plain(snap: dict, now) -> str:
    """LINE version — plain text, no <pre> tables (LINE renders a proportional
    font, so column padding just looks crooked), everyday Chinese."""
    lines = [f"🇺🇸 美股收盤 {_session_label(snap, now)}", ""]

    for i in snap["indices"]:
        if i["price"] is None:
            continue
        lines.append(f"{i['label']} {_num(i['price'])}"
                     f"　{_arrow(i['chg_pct'])} {_pct(i['chg_pct'])}")
    vix = snap["vix"]
    if vix["price"] is not None:
        lines.append(f"VIX 波動 {vix['price']:.2f}"
                     f"　{_arrow(vix['chg_pct'])} {_pct(vix['chg_pct'])}")
    tnx = snap["tnx"]
    if tnx["price"] is not None:
        pp = "" if tnx["chg_pp"] is None else f"　{_arrow(tnx['chg_pp'])} {tnx['chg_pp']:+.2f}"
        lines.append(f"美10年公債 {tnx['price']:.2f}%{pp}")

    adr = snap["adr"]
    if adr:
        sign = "溢價" if adr["premium_pct"] >= 0 else "折價"
        lines += ["",
                  f"🇹🇼 台積電 ADR {adr['adr']:,.2f}"
                  f"　{_arrow(adr['adr_pct'])} {_pct(adr['adr_pct'])}",
                  f"　約當台股 {adr['equiv']:,.0f} 元"
                  f"（2330 昨收 {adr['tw_close']:,.0f}，{sign} {abs(adr['premium_pct']):.1f}%）"]

    b = snap["breadth"]
    if b["total"]:
        lines += ["", f"📊 美股百大：上漲 {b['up']} / 下跌 {b['down']}"]
        if b["gainers"]:
            lines.append("　領漲 " + "、".join(
                f"{r['ticker']} {r['change_pct']:+.1f}%" for r in b["gainers"]))
        if b["losers"]:
            lines.append("　領跌 " + "、".join(
                f"{r['ticker']} {r['change_pct']:+.1f}%" for r in b["losers"]))

    read = read_line(snap)
    if read:
        lines += ["", f"💡 {read}"]
    lines += ["", "⚠️ 資訊參考，非投資建議"]
    return "\n".join(lines)


def build_telegram(snap: dict, now) -> str:
    """Telegram version — same numbers in the shared house style, with the
    monospace table Telegram can actually align (unlike LINE)."""
    import tg_format as tf

    rows = [[i["label"], _num(i["price"]),
             f"{_arrow(i['chg_pct'])} {_pct(i['chg_pct'])}"]
            for i in snap["indices"] if i["price"] is not None]
    vix = snap["vix"]
    if vix["price"] is not None:
        rows.append(["VIX", f"{vix['price']:.2f}",
                     f"{_arrow(vix['chg_pct'])} {_pct(vix['chg_pct'])}"])
    tnx = snap["tnx"]
    if tnx["price"] is not None:
        rows.append(["10年公債", f"{tnx['price']:.2f}%",
                     "—" if tnx["chg_pp"] is None
                     else f"{_arrow(tnx['chg_pp'])} {tnx['chg_pp']:+.2f}"])

    out = [f"🇺🇸 <b>美股收盤</b> · {tf.esc(_session_label(snap, now))}", tf.DIV]
    if rows:
        # 'lrl': prices right-aligned into a column, but the ▲/▼ column LEFT —
        # right-aligning it pads the shorter '+0.06' cell and knocks that row's
        # arrow out of line with the rest.
        out.append(tf.pre_table(rows, align="lrl"))

    adr = snap["adr"]
    if adr:
        sign = "溢價" if adr["premium_pct"] >= 0 else "折價"
        out += [tf.DIV,
                f"🇹🇼 台積電 ADR <b>{adr['adr']:,.2f}</b> "
                f"{_arrow(adr['adr_pct'])} {_pct(adr['adr_pct'])}",
                f"約當台股 <b>{adr['equiv']:,.0f}</b>（2330 昨收 {adr['tw_close']:,.0f}"
                f" · {sign} {abs(adr['premium_pct']):.1f}%）"]

    b = snap["breadth"]
    if b["total"]:
        out += [tf.DIV, f"📊 美股百大 上漲 {b['up']} / 下跌 {b['down']}"]
        if b["gainers"]:
            out.append("領漲 " + " · ".join(
                f"{tf.esc(r['ticker'])} {r['change_pct']:+.1f}%" for r in b["gainers"]))
        if b["losers"]:
            out.append("領跌 " + " · ".join(
                f"{tf.esc(r['ticker'])} {r['change_pct']:+.1f}%" for r in b["losers"]))

    read = read_line(snap)
    if read:
        out += [tf.DIV, f"💡 {tf.esc(read)}"]
    out.append("⚠️ 資訊參考，非投資建議")
    return "\n".join(out)


def build_holiday_plain(now) -> str:
    """US holiday: say so in one line rather than reprinting yesterday's close
    as if it were new. Costs one push (~9 US holidays a year) and prevents the
    'did it break?' question a silent morning would raise."""
    return (f"🇺🇸 美股休市 {now.strftime('%m-%d')}\n\n"
            "昨夜美國股市休市，沒有新的收盤資訊。\n"
            "⚠️ 資訊參考，非投資建議")


# ── orchestration ────────────────────────────────────────────────────────────
def _due(state: dict, now) -> bool:
    """One send per TWSE trading morning, inside a BOUNDED window. Weekends are
    skipped: there is no 台股 open to prepare for, and Friday's US close is
    reported on Monday morning instead.

    The upper bound is the point. This gate started as `now.hour >= SEND_HOUR`,
    copied from tw_stocks._due where that shape is correct — the 13:30 TW close
    is final, so every later hour reads the same closing bar. For the US it is
    not: 23:00 台北 is the middle of the New York session. A scanner restarted
    at 23:44 台北 passed `hour >= 8` and fired immediately on live intraday
    prices. Missing a morning because the stack was down beats mislabelling a
    live session as a close.
    """
    if now.weekday() >= 5:
        return False
    if not SEND_HOUR <= now.hour < SEND_HOUR + SEND_WINDOW_HOURS:
        return False
    return state.get("last_run_date") != now.strftime("%Y-%m-%d")


def _us_rows() -> list:
    """US100 rows from the /stocks page cache — shared, so an open page and
    this digest never double-fetch. [] if the source is down; breadth then
    just omits itself from the message."""
    try:
        import stocks_data
        return stocks_data.us_quote_rows()
    except Exception as exc:  # noqa: BLE001 — breadth is a bonus, never fatal
        print(f"[usmarket] breadth unavailable: {exc}")
        return []


def tick() -> bool:
    """Called every scanner sweep; sends one message per TWSE trading morning.
    Returns True when something was sent."""
    now = datetime.now(TZ)
    state = _load_state()
    if not _due(state, now):
        return False
    if time.time() - state.get("last_attempt", 0) < RETRY_SEC:
        return False
    state["last_attempt"] = time.time()

    try:
        quotes = fetch_quotes()
    except Exception as exc:  # noqa: BLE001 — retry on the next sweep
        print(f"[usmarket] quote fetch failed: {exc}")
        _save_state(state)
        return False

    sess = session_date(quotes)
    if not sess:
        print("[usmarket] no S&P timestamp — cannot identify the session, retrying")
        _save_state(state)
        return False
    # Independent of the clock gate above, and deliberately so: _due() decides
    # WHEN we look, this decides whether what we found is actually a close.
    # Either one alone would have prevented the 2026-07-30 mislabel; a wrong
    # SEND_HOUR would still be caught here.
    if not session_complete(quotes):
        print(f"[usmarket] {sess} still trading — not a close yet, retrying")
        _save_state(state)
        return False

    today = now.strftime("%Y-%m-%d")
    if sess == state.get("last_session_date"):
        # No new close since the last message → US holiday (or a Yahoo stall).
        plain, msg = build_holiday_plain(now), None
        print(f"[usmarket] {today}: no new US session since {sess} — holiday notice")
    else:
        snap = build_snapshot(quotes, _us_rows())
        plain = build_plain(snap, now)
        msg = build_telegram(snap, now)
        state["last_session_date"] = sess

    import telegram_utils
    sent = telegram_utils.send_message(msg or plain, parse_mode="HTML" if msg else None,
                                       force=True, channel="twstocks")
    import line_push
    if line_push.enabled():                # 爸爸的 LINE — plain text, pre-open
        line_push.send(plain)

    state["last_run_date"] = today
    state["last_plain"] = plain            # served by the LINE 「美股」 command
    state["last_text"] = msg or plain      # served by /us on Telegram
    _save_state(state)
    print(f"[usmarket] {today}: session={sess} sent={sent}")
    return bool(sent)
