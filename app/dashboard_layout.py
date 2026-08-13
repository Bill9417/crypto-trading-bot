"""
🧩 Dashboard layout — per-user row order and visibility for the home page.

The dashboard grew to thirteen rows and the order was whatever I happened to
add things in. This lets the owner drag the rows they actually read to the top
and hide the ones they never open, and keeps that choice.

STORED SERVER-SIDE, PER USER, not in localStorage. The same account is read on
a phone and a Mac, and a layout that only exists in one browser is a layout you
have to redo on the other — plus it evaporates on a cache clear, which is
exactly the kind of small loss that makes a feature feel broken.

APPLIED WITH CSS `order`, NOT BY MOVING DOM NODES. The rows are literal HTML in
a 2,800-line template, and reordering them server-side would mean cutting the
page into thirteen includes. Flex `order` needs one style block, which the
template renders inline — so the saved layout is correct in the FIRST paint.
Reordering in JavaScript after load would show the default order and then jump,
on every single page view.

THE NORMALISE RULE THAT MATTERS: a saved layout is a PREFERENCE, never a
whitelist. Cards the user has never seen — anything added after they last saved
— are appended, not dropped. Get this backwards and shipping a new panel makes
it invisible to precisely the people who have used the feature, which is an
unfindable bug: the page looks fine and the new thing simply is not there.
"""
import json
import os
import threading

STORE_FILE = os.path.join(os.path.dirname(__file__), "dashboard_layout.json")

# The rows, in the order a fresh account sees them. The id is a stable key that
# lives in the template as data-card and MUST NOT be renamed — a rename is
# indistinguishable from a deletion, so every saved layout would silently drop
# that row to the bottom.
CARDS = (
    ("pulse", "⚡ Market Pulse"),
    ("breadth", "📊 Market Breadth"),
    ("s4", "📊 S4 · Perp Setups"),
    ("entries", "🎯 Top Entry Candidates"),
    ("coins", "◈ Main Coins"),
    ("briefing", "📊 Today's Briefing"),
    ("radar", "🚀 Pump Radar"),
    ("flips", "🚀 壓力翻支撐"),
    ("oi", "🐋 OI 異常"),
    ("whale", "🐳 Whale Positioning"),
    ("liqmap", "💥 Liquidation Map"),
    ("news", "📰 Market Headlines"),
    ("signals", "📡 Signal Pulse"),
    ("hunting", "🎯 Hunting Signals"),
)

CARD_IDS = tuple(c[0] for c in CARDS)
CARD_TITLE = dict(CARDS)

_lock = threading.Lock()


def default_layout() -> dict:
    return {"order": list(CARD_IDS), "hidden": []}


def normalise(saved: dict) -> dict:
    """Turn anything that came off disk (or off the wire) into a usable layout.

    Four things go wrong here and all four are silent:
      · an id that no longer exists  → dropped (a removed panel)
      · a known id that is missing   → APPENDED (a panel added since the save)
      · the same id twice            → first wins
      · hidden listing everything    → allowed; an empty dashboard is a choice,
                                        and the edit bar is outside the rows
    """
    saved = saved if isinstance(saved, dict) else {}
    raw = saved.get("order")
    order, seen = [], set()
    for cid in raw if isinstance(raw, list) else []:
        if cid in CARD_TITLE and cid not in seen:
            order.append(cid)
            seen.add(cid)
    # Appended in their DEFAULT position relative to each other, so adding two
    # panels at once does not scramble them into save order.
    for cid in CARD_IDS:
        if cid not in seen:
            order.append(cid)
    raw_hidden = saved.get("hidden")
    hidden = [c for c in (raw_hidden if isinstance(raw_hidden, list) else [])
              if c in CARD_TITLE]
    return {"order": order, "hidden": sorted(set(hidden), key=CARD_IDS.index)}


def _key(user_id) -> str:
    """The stable half of a user identity.

    flask_login's get_id() here returns "<id>|<session_token>", and that token
    is ROTATED — new_session_token() runs on a password change and on
    log-out-everywhere. Keyed on the whole string, changing your password would
    silently orphan your saved layout and the dashboard would quietly revert to
    default with nothing to explain it.

    Splitting on "|" also folds any row already written under the long form
    onto the right bucket, so this heals rather than abandoning them.
    """
    return str(user_id).split("|")[0]


def _read_all() -> dict:
    try:
        with open(STORE_FILE, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Fold legacy "<id>|<token>" keys onto the stable id. Later keys win, which
    # for a rotated token means the most recently written layout survives.
    out = {}
    for k, v in raw.items():
        out[_key(k)] = v
    return out


def load(user_id) -> dict:
    """One user's layout, always valid."""
    return normalise(_read_all().get(_key(user_id)) or {})


def save(user_id, layout: dict) -> dict:
    """Persist and return what was actually stored (normalised)."""
    clean = normalise(layout)
    with _lock:
        allof = _read_all()
        allof[_key(user_id)] = clean
        tmp = f"{STORE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allof, f)
        os.replace(tmp, STORE_FILE)
    return clean


def reset(user_id) -> dict:
    with _lock:
        allof = _read_all()
        allof.pop(_key(user_id), None)
        tmp = f"{STORE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allof, f)
        os.replace(tmp, STORE_FILE)
    return default_layout()


def style_block(layout: dict) -> str:
    """The inline CSS that puts the rows where the user wants them.

    Emitted into the template so the first paint is already correct. `order`
    is 1-based with the hidden rows still numbered — leaving gaps is harmless
    and keeps the number equal to the row's rank, which makes this readable in
    devtools when something looks wrong.
    """
    layout = normalise(layout)
    hidden = set(layout["hidden"])
    out = []
    for i, cid in enumerate(layout["order"], start=1):
        out.append(f'.page-wrap > [data-card="{cid}"]{{order:{i}}}')
        if cid in hidden:
            out.append(f'.page-wrap > [data-card="{cid}"]{{display:none}}')
    return "".join(out)


def cards_for_editor(layout: dict) -> list:
    """[{id, title, hidden}] in the user's own order — what the edit bar lists."""
    layout = normalise(layout)
    hidden = set(layout["hidden"])
    return [{"id": c, "title": CARD_TITLE[c], "hidden": c in hidden}
            for c in layout["order"]]
