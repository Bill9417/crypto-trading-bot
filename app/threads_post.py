"""
🧵 Meta Threads — one public market post per day, automatically.

Same content discipline as morning_brief: this is PUBLIC marketing, so it
carries prices, sentiment, the calendar and the scanner's own measured results,
and it must never carry an account number. A test enforces that.

── How Meta's API actually works (verified against the docs, not from memory) ──
Publishing is TWO calls, not one:
  1. POST {GRAPH}/{user-id}/threads          media_type=TEXT&text=…  → creation_id
  2. POST {GRAPH}/{user-id}/threads_publish  creation_id=…           → post id
Meta recommends waiting ~30s between them. Sleeping 30s inside the S2 sweep
would stall every other job in that pass, so the two steps run on SEPARATE
ticks: the container is created, its id is persisted, and the next sweep
(300s later, comfortably past the recommendation) publishes it. A container
that never gets published just expires on Meta's side — no orphan post.

LENGTH: Meta documents "500 characters", and separately that "emojis are
counted as the number of UTF-8 bytes". So a 4-byte emoji costs 4, not 1. We
measure by that rule and refuse to send anything over — a rejected post is a
silent no-show, and this runs unattended.

TOKENS: the OAuth code gives a SHORT-lived token (1h). It is immediately
exchanged for a long-lived one (60 days), which must then be refreshed before
it expires — a token not refreshed within 60 days is dead and needs the whole
browser flow again. We refresh every 7 days (Meta requires a token be ≥24h old
to qualify), so eight consecutive failures would have to pass unnoticed before
anything breaks.

── TWO MODES, and the easy one is the default ──────────────────────────────
Registering a Meta app is the only hard part of this whole feature, and all it
buys is ~20 seconds a day. So it is optional:

  DRAFT (no setup at all): set THREADS_ENABLED=true and nothing else. Each
  morning the finished post arrives in your own Telegram DM inside a
  tap-to-copy block; you paste it into Threads. The URL is placed INLINE
  because manual posting has no link_attachment parameter — Threads builds its
  preview from the first URL in the body.

  API (fully automatic): connect a Meta app and tick() publishes by itself.
  The switch is automatic — the day /threads/connect succeeds, drafts stop.

Meta app setup, if you want the automatic mode:
  1. developers.facebook.com → create an app → add the "Threads API" use case
  2. add the Threads tester (your own account) and accept the invite
  3. Valid OAuth Redirect URI:  {PUBLIC_BASE_URL}/threads/callback
  4. put THREADS_APP_ID / THREADS_APP_SECRET in .env
  5. visit /threads/connect on the dashboard (admin) and approve

Commands: python threads_post.py --url · --preview · --post (send now, once).
"""
import json
import os
import time
from datetime import datetime

import requests

import config

GRAPH = "https://graph.threads.net/v1.0"
AUTHORIZE_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
EXCHANGE_URL = "https://graph.threads.net/access_token"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
SCOPES = "threads_basic,threads_content_publish"

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DIR, "threads_state.json")

APP_ID = os.getenv("THREADS_APP_ID", "").strip()
APP_SECRET = os.getenv("THREADS_APP_SECRET", "").strip()
ENABLED = os.getenv("THREADS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# 09:00 local — an hour after the Telegram brief, so the two don't collide and
# the post lands after the Asian open rather than in the middle of the night.
POST_HOUR = int(os.getenv("THREADS_POST_HOUR", "9"))
MAX_LEN = 500
# Meta: "wait on average 30 seconds before publishing". We wait a whole sweep.
PUBLISH_DELAY_SEC = int(os.getenv("THREADS_PUBLISH_DELAY", "30"))
REFRESH_EVERY_DAYS = 7          # Meta needs the token ≥24h old; 60d is the wall
HTTP_TIMEOUT = float(os.getenv("THREADS_HTTP_TIMEOUT", "20"))
# Where the post's preview card points. The Telegram invite is the funnel the
# owner wants to grow, and link_attachment costs no characters (a raw t.me URL
# in the body would). Set THREADS_LINK to the /welcome page instead to make
# people read the live proof before they join.
CTA_TEXT = os.getenv("THREADS_CTA", "免費即時訊號在 Telegram 群 👇")
# Publishing a hit rate to strangers is off by default — see build_post().
SHOW_OUTCOMES = os.getenv("THREADS_SHOW_OUTCOMES", "false").strip().lower() \
    in ("1", "true", "yes")
# Bybit referral. It pays the owner a commission when someone signs up and
# trades, which makes it branded content under Meta's policy and an ad under
# most fair-trading rules — so it is labelled. Undisclosed affiliate links get
# posts down-ranked or pulled, which costs more reach than the label does
# characters. THREADS_REF_LABEL="" removes it if you disclose another way.
REF_URL = (os.getenv("BYBIT_REF_URL") or "").strip()
REF_TEXT = os.getenv("THREADS_REF_TEXT", "開 Bybit 帳戶（推薦連結）👇")
TZ = getattr(config, "TZ", None)
_WD = "一二三四五六日"


# ── state ────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt → start clean
        return {}


def _save_state(state: dict) -> None:
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def configured() -> bool:
    return bool(APP_ID and APP_SECRET)


def connected() -> bool:
    st = _load_state()
    return bool(st.get("access_token") and st.get("user_id"))


# ── length, by Meta's documented rule ────────────────────────────────────────
def threads_len(text: str) -> int:
    """Meta counts 500 CHARACTERS, but counts each emoji as its UTF-8 BYTES.

    So "🚀" costs 4, not 1. Plain text (including CJK) counts as one per
    character. Getting this wrong means a post that is silently rejected."""
    total = 0
    for ch in text or "":
        cp = ord(ch)
        emoji = (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                 or cp in (0xFE0F, 0x200D) or 0x1F1E6 <= cp <= 0x1F1FF)
        total += len(ch.encode("utf-8")) if emoji else 1
    return total


# ── OAuth ────────────────────────────────────────────────────────────────────
def redirect_uri() -> str:
    base = (os.getenv("PUBLIC_BASE_URL")
            or getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/threads/callback"


def post_link() -> str:
    """The preview card's target: the Telegram group by default (that is the
    funnel being grown), the public /welcome page if there is no invite."""
    explicit = (os.getenv("THREADS_LINK") or "").strip()
    if explicit:
        return explicit
    invite = (os.getenv("TELEGRAM_INVITE_URL")
              or getattr(config, "TELEGRAM_INVITE_URL", "") or "").strip()
    if invite:
        return invite
    base = (os.getenv("PUBLIC_BASE_URL")
            or getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/welcome" if base else ""


def auth_url(state: str = "") -> str:
    from urllib.parse import urlencode
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": APP_ID, "redirect_uri": redirect_uri(),
        "scope": SCOPES, "response_type": "code", "state": state or "wolf"})


def exchange_code(code: str) -> dict:
    """Authorization code → long-lived token, persisted. {ok, error, username}."""
    if not configured():
        return {"ok": False, "error": "THREADS_APP_ID / THREADS_APP_SECRET missing"}
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": APP_ID, "client_secret": APP_SECRET,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri(),
            "code": code}, timeout=HTTP_TIMEOUT)
        short = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"token exchange failed: {str(exc)[:160]}"}
    if not short.get("access_token"):
        return {"ok": False, "error": _err(short)}
    # A 1-hour token is useless to a daemon — trade it up immediately.
    try:
        r2 = requests.get(EXCHANGE_URL, params={
            "grant_type": "th_exchange_token", "client_secret": APP_SECRET,
            "access_token": short["access_token"]}, timeout=HTTP_TIMEOUT)
        long_t = r2.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"long-lived exchange failed: {str(exc)[:160]}"}
    if not long_t.get("access_token"):
        return {"ok": False, "error": _err(long_t)}
    st = _load_state()
    st.update({"access_token": long_t["access_token"],
               "user_id": str(short.get("user_id") or ""),
               "obtained_at": time.time(),
               "expires_in": long_t.get("expires_in")})
    _save_state(st)
    return {"ok": True, "username": _username(st) or ""}


def refresh_if_due(force: bool = False) -> dict:
    """Long-lived tokens die at 60 days; refresh weekly so a run of failures
    can't quietly walk off the cliff. Meta requires the token be ≥24h old."""
    st = _load_state()
    tok = st.get("access_token")
    if not tok:
        return {"ok": False, "error": "not connected"}
    age = time.time() - float(st.get("obtained_at") or 0)
    if not force and age < REFRESH_EVERY_DAYS * 86400:
        return {"ok": True, "skipped": True}
    if age < 86400:                       # Meta refuses a token younger than 24h
        return {"ok": True, "skipped": True}
    try:
        r = requests.get(REFRESH_URL, params={"grant_type": "th_refresh_token",
                                              "access_token": tok},
                         timeout=HTTP_TIMEOUT)
        d = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}
    if not d.get("access_token"):
        return {"ok": False, "error": _err(d)}
    st["access_token"] = d["access_token"]
    st["obtained_at"] = time.time()
    st["expires_in"] = d.get("expires_in")
    _save_state(st)
    print("[threads] access token refreshed (+60d)")
    return {"ok": True, "refreshed": True}


def _err(payload: dict) -> str:
    e = (payload or {}).get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e)[:200]
    return str(e or payload)[:200]


def _username(st: dict) -> str:
    try:
        r = requests.get(f"{GRAPH}/me", params={
            "fields": "username", "access_token": st.get("access_token")},
            timeout=HTTP_TIMEOUT)
        return (r.json() or {}).get("username") or ""
    except Exception:  # noqa: BLE001 — cosmetic only
        return ""


# ── publishing (two calls, two ticks) ────────────────────────────────────────
def create_container(text: str, link: str = "") -> dict:
    """Step 1 — returns {ok, creation_id, error}. Nothing is public yet."""
    st = _load_state()
    if not st.get("access_token") or not st.get("user_id"):
        return {"ok": False, "error": "not connected — visit /threads/connect"}
    n = threads_len(text)
    if n > MAX_LEN:
        return {"ok": False, "error": f"post is {n}/{MAX_LEN} — refusing to send"}
    data = {"media_type": "TEXT", "text": text,
            "access_token": st["access_token"]}
    if link:
        data["link_attachment"] = link
    try:
        r = requests.post(f"{GRAPH}/{st['user_id']}/threads", data=data,
                          timeout=HTTP_TIMEOUT)
        d = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    if not d.get("id"):
        return {"ok": False, "error": _err(d)}
    return {"ok": True, "creation_id": str(d["id"])}


def publish_container(creation_id: str) -> dict:
    """Step 2 — returns {ok, id, error}. This is what makes it public."""
    st = _load_state()
    try:
        r = requests.post(f"{GRAPH}/{st.get('user_id')}/threads_publish",
                          data={"creation_id": creation_id,
                                "access_token": st.get("access_token")},
                          timeout=HTTP_TIMEOUT)
        d = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    if not d.get("id"):
        return {"ok": False, "error": _err(d)}
    return {"ok": True, "id": str(d["id"])}


# ── the daily post (pure given `data` — unit-testable, no network) ───────────
def build_post(data: dict, now: datetime, inline_link: str = "",
               ref_link: str = "") -> str:
    """≤500 by Meta's rule, and ACCOUNT-FREE: prices, sentiment, calendar and
    the scanner's own measured results only. Never a balance or a position."""
    import tg_format
    lines = [f"☀️ {now.strftime('%m/%d')} 加密市場早報（週{_WD[now.weekday()]}）"]

    px = data.get("prices") or {}
    bits = []
    for base in ("BTC", "ETH", "SOL"):
        t = px.get(base) or {}
        if t.get("last"):
            chg = f" {tg_format.pct(t['pct'])}" if t.get("pct") is not None else ""
            bits.append(f"{base} {tg_format.fmt_price(t['last'])}{chg}")
    if bits:
        lines += [""] + bits

    mood = []
    fng = data.get("fng") or {}
    if fng.get("value") is not None:
        import morning_brief
        mood.append(f"恐懼貪婪 {fng['value']}（{morning_brief._fng_zh(fng.get('label'))}）")
    dom = (data.get("global") or {}).get("btc_dominance")
    if dom is not None:
        mood.append(f"BTC 佔比 {dom:.1f}%")
    if mood:
        lines += ["", "🌡 " + " · ".join(mood)]

    cal = data.get("today_events") or []
    if cal:
        # The Telegram brief lists them all; 500 chars buys one headline. This
        # is the ONLY unbounded input in the post, so capping it here is what
        # makes the total length predictable instead of hoping a trim loop
        # catches it later.
        head = str(cal[0]).lstrip("• ").strip()
        lines += ["", "🗓 " + (head[:39] + "…" if len(head) > 40 else head)]

    sig = data.get("signals") or {}
    if sig.get("n"):
        s = f"📊 24h 掃出 {sig['n']} 個訊號"
        if sig.get("premium"):
            s += f"（⭐{sig['premium']}）"
        lines += ["", s]
    # The 7-day "先到目標" rate stays OFF by default. morning_brief's tracker
    # counts tp1→sl as a hit — a signal that tagged the first target and then
    # ran to the stop — so the number is softer than a win rate, and a win rate
    # is already not an edge (this project's own walk-forwards say so). Inside
    # the group a reader can type /outcomes and see the breakdown; a stranger
    # scrolling Threads reads it as "56% win rate" and cannot check anything.
    oc = data.get("outcomes") or {}
    if SHOW_OUTCOMES and (oc.get("n") or 0) >= 5:
        lines.append(f"📋 近 7 日結算 {oc['n']} 個 · 先到目標 {oc['hit_pct']:.0f}%")

    # Posting by hand has no link_attachment parameter — Threads builds its
    # preview from the first URL in the body, so a DRAFT must carry the URL
    # inline. The API path leaves it out and passes link_attachment instead,
    # which costs no characters.
    # Tail is built in BLOCKS, not lines, so the overflow guard below can never
    # strand a call-to-action whose link it just removed.
    blocks = [[CTA_TEXT] + ([inline_link] if inline_link else [])]
    # The referral goes SECOND on purpose. Threads previews only the FIRST URL
    # in the body, and a cold reader who has seen nothing yet converts far
    # better on "join the group" than on "open a trading account".
    if ref_link:
        blocks.append(["", REF_TEXT, ref_link])
    blocks.append(["#加密貨幣 #比特幣 #以太幣 #量化交易"])

    def _render(bs):
        return "\n".join(lines + [""] + [ln for b in bs for ln in b])

    text = _render(blocks)
    # Hashtags go first, then the whole referral block; the group link is last
    # out because it is the one the post exists for. A referral URL alone runs
    # ~98 characters, so this guard is no longer purely theoretical.
    while threads_len(text) > MAX_LEN and len(blocks) > 1:
        blocks.pop()
        text = _render(blocks)
    return text


# ── scheduling ───────────────────────────────────────────────────────────────
def _due(state: dict, now: datetime) -> bool:
    return now.hour >= POST_HOUR and state.get("last_post") != now.strftime("%Y-%m-%d")


def send_draft(text: str, now: datetime) -> bool:
    """DRAFT MODE — no Meta app, no OAuth, no review. The post is written for
    you and delivered to your own Telegram DM in a tap-to-copy block; you paste
    it into Threads. Registering a Meta app is the only hard part of this
    feature and it buys ~20 seconds a day, so it is optional, not required.

    Upgrades itself: the day /threads/connect succeeds, tick() publishes
    through the API instead and this stops firing."""
    import telegram_utils
    import tg_format
    msg = (f"🧵 <b>今天的 Threads 貼文</b>（{now.strftime('%m/%d')}）\n"
           f"點下面整塊即可複製，貼到 Threads 就好。\n\n"
           f"<pre>{tg_format.esc(text)}</pre>\n"
           f"— {threads_len(text)}/{MAX_LEN} 字\n"
           f"（想改成全自動發文：/threads/connect）")
    return bool(telegram_utils.send_message(msg, parse_mode="HTML",
                                            force=True, channel="private"))


def tick(client=None) -> bool:
    """Called once per S2 sweep. True only when today's post went out.

    Two paths, chosen automatically: with the Meta app connected the container
    is created on one pass and published on the next (so Meta's ~30s delay
    costs this loop nothing); without it, the finished text is sent to the
    owner's DM to paste by hand. Never raises."""
    if not ENABLED:
        return False
    try:
        now = datetime.now(TZ) if TZ else datetime.now()
        st = _load_state()

        if not (configured() and connected()):
            if not _due(st, now):
                return False
            import morning_brief
            text = build_post(morning_brief._gather(client, now), now,
                              inline_link=post_link(), ref_link=REF_URL)
            if not send_draft(text, now):
                return False                  # Telegram blip → retry next sweep
            st["last_post"] = now.strftime("%Y-%m-%d")
            st["last_mode"] = "draft"
            _save_state(st)
            print(f"[threads] draft sent for {st['last_post']} (paste by hand)")
            return True

        pend = st.get("pending")
        if pend and time.time() - float(pend.get("ts") or 0) >= PUBLISH_DELAY_SEC:
            res = publish_container(pend.get("creation_id"))
            st.pop("pending", None)
            if res.get("ok"):
                st["last_post"] = pend.get("date")
                st["last_post_id"] = res["id"]
                _save_state(st)
                print(f"[threads] posted {res['id']} for {pend.get('date')}")
                return True
            # Drop the container and let the next day try again — retrying a
            # stale creation_id just re-fails, and double-posting is worse.
            _save_state(st)
            print(f"[threads] publish failed: {res.get('error')}")
            return False
        if pend:
            return False

        refresh_if_due()
        if not connected() or not _due(st, now):
            return False
        import morning_brief
        text = build_post(morning_brief._gather(client, now), now,
                          ref_link=REF_URL)
        res = create_container(text, post_link())
        if not res.get("ok"):
            print(f"[threads] container failed: {res.get('error')}")
            return False
        st = _load_state()
        st["pending"] = {"creation_id": res["creation_id"], "ts": time.time(),
                         "date": now.strftime("%Y-%m-%d")}
        _save_state(st)
        print(f"[threads] container {res['creation_id']} ready — publishing next sweep")
        return False
    except Exception as exc:  # noqa: BLE001 — a marketing post never kills the sweep
        print(f"[threads] tick error: {exc}")
        return False


if __name__ == "__main__":  # pragma: no cover — operator tool
    import sys
    if "--url" in sys.argv:
        print("1) 在 Meta App 後台把這個網址加進 Valid OAuth Redirect URIs：")
        print("   ", redirect_uri())
        print("2) 用瀏覽器打開這個授權網址：")
        print("   ", auth_url())
    elif "--preview" in sys.argv:
        import morning_brief
        now = datetime.now(TZ) if TZ else datetime.now()
        auto = configured() and connected()
        body = build_post(morning_brief._gather(None, now), now,
                          inline_link="" if auto else post_link(),
                          ref_link=REF_URL)
        print(body)
        print(f"\n— {threads_len(body)}/{MAX_LEN} (Meta 規則) · "
              f"模式：{'API 自動發文' if auto else '草稿（發到你的 Telegram 私訊）'}")
    elif "--draft" in sys.argv:
        import morning_brief
        now = datetime.now(TZ) if TZ else datetime.now()
        text = build_post(morning_brief._gather(None, now), now,
                          inline_link=post_link(), ref_link=REF_URL)
        print("sent:", send_draft(text, now))
    elif "--post" in sys.argv:
        import morning_brief
        now = datetime.now(TZ) if TZ else datetime.now()
        c = create_container(build_post(morning_brief._gather(None, now), now,
                                        ref_link=REF_URL))
        print("container:", c)
        if c.get("ok"):
            time.sleep(PUBLISH_DELAY_SEC)
            print("publish:", publish_container(c["creation_id"]))
    else:
        print(f"configured={configured()} connected={connected()} enabled={ENABLED}")
        print(f"mode: {'API' if configured() and connected() else 'draft → Telegram DM'}")
        print("usage: threads_post.py --preview | --draft | --url | --post")
