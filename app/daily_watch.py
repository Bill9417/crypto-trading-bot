"""
👀 每日觀察清單 — the five coins that kept showing up today.

DIFFERENT FROM 綜合前三名, and the difference is the point. That board is a
LIVE snapshot: it changes every sweep, so a coin can be on it at 09:00 and
gone by 09:20 without anything having happened. This one ACCUMULATES over a
台北 day and is stable: a coin earns its place by being flagged repeatedly, by
different engines, across hours — which is what "worth watching today" means.

RANKED BY BREADTH, NOT BY SCORE. The unit is "how many INDEPENDENT engines
flagged this, on how many separate occasions". No weighting, no composite
number: this repo has measured every one of these engines individually and
found them somewhere between negative and indistinguishable from zero, so a
weighted blend of them would be a made-up number with a decimal point.
Agreement between things that disagree often is the only signal here.

AN OBSERVE LIST, NOT A TRADE LIST. Nothing here has an entry, a stop or a
target attached, on purpose — the engines that produce those have their own
boards with their own measured records beside them.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ_DISPLAY", "Asia/Taipei"))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "daily_watch.json")
TOP_N = int(os.getenv("WATCH_TOP_N", "5"))
# Keep a couple of days so a morning look still shows yesterday's list while
# today's is still thin.
KEEP_DAYS = int(os.getenv("WATCH_KEEP_DAYS", "3"))

SRC_ZH = {"s2": "S2 訊號", "flip": "壓力翻支撐", "zone": "供需區",
          "oi": "OI 異常", "s4": "S4 掃描"}


def today_str(now=None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = start today fresh
        return {}


def _save(state: dict) -> None:
    try:
        tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — a watchlist must not break a page
        print(f"[watch] save failed: {exc}")


def _sightings() -> dict:
    """{base: {source: note}} from whatever each engine currently holds.

    Every read is a FILE read — no exchange call — so this can run on a page
    load without touching the rate budget. A source that fails contributes
    nothing rather than taking the list down with it.
    """
    out: dict = {}

    def note(base, src, text, side=None):
        """side is 'long' / 'short' / None. None means the engine does not
        express a direction (OI anomalies do not), which is NOT the same as
        agreeing with whatever else flagged the coin."""
        b = (base or "").upper()
        if b:
            out.setdefault(b, {})[src] = {"note": text, "side": side}

    try:
        import json as _j
        d = _j.load(open(os.path.join(os.path.dirname(STATE_FILE),
                                      "strategy2_signals.json"), encoding="utf-8"))
        for s in (d.get("signals") or []):
            side = "long" if s.get("direction") == "long" else "short"
            dirn = "做多" if side == "long" else "做空"
            note(s.get("base"), "s2", f"{dirn}·信心 {s.get('score')}", side)
    except Exception:  # noqa: BLE001
        pass

    for mod, src in (("flip_outcomes", "flip"), ("zone_outcomes", "zone")):
        try:
            m = __import__(mod)
            st = m.load()
            rows = list((st.get("open") or {}).values()) + (st.get("recent") or [])
            for r in rows:
                side = r.get("side") or "long"
                note(r.get("base"), src, "做多" if side == "long" else "做空", side)
        except Exception:  # noqa: BLE001
            pass

    try:
        import json as _j
        d = _j.load(open(os.path.join(os.path.dirname(STATE_FILE),
                                      "crowd_radar_state.json"), encoding="utf-8"))
        for r in (d.get("recent") or [])[:60]:
            base = (r.get("symbol") or r.get("base") or "").replace("USDT", "")
            note(base, "oi", f"持倉 {r.get('oi_pct', 0):+.1f}%")
    except Exception:  # noqa: BLE001
        pass

    try:
        import json as _j
        d = _j.load(open(os.path.join(os.path.dirname(STATE_FILE),
                                      "strategy4_signals.json"), encoding="utf-8"))
        for s in (d.get("signals") or []):
            side = s.get("side") or "long"
            note(s.get("base"), "s4", "做多" if side == "long" else "做空", side)
    except Exception:  # noqa: BLE001
        pass
    return out


# How often the tally may advance. Without this the count measures PAGE LOADS,
# not time: leaving the dashboard open would inflate every coin on it, and two
# people looking would double it. At a fixed cadence a hit means "still there
# five minutes later", which is the persistence the ranking is claiming.
REFRESH_SEC = float(os.getenv("WATCH_REFRESH_SEC", "300"))


def refresh(now=None, force: bool = False) -> dict:
    """Fold this moment's sightings into today's tally.

    Rate-limited: a call inside the cadence window returns today's tally
    unchanged rather than counting again.
    """
    now = now or datetime.now(TZ)
    day = today_str(now)
    state = _load()
    today = state.get(day) or {"coins": {}, "refreshes": 0}
    wall = int(time.time())
    if not force and wall - int(today.get("updated") or 0) < REFRESH_SEC:
        return today
    for base, srcs in _sightings().items():
        rec = today["coins"].setdefault(base, {"srcs": {}, "hits": 0, "first": wall})
        for src, info in srcs.items():
            text, side = info["note"], info.get("side")
            # One count per refresh per engine. That is guaranteed by the
            # SHAPE of _sightings() — a dict keyed by base, then by source, so
            # a pair cannot appear twice in one pass — not by a check here.
            # There used to be a `last != stamp` guard; it could never fire,
            # and an untested branch that implies a protection it does not
            # provide is worse than no branch.
            s = rec["srcs"].setdefault(src, {"n": 0, "note": text, "side": side})
            s["n"] += 1
            s["note"] = text
            s["side"] = side
        rec["hits"] = sum(v["n"] for v in rec["srcs"].values())
        rec["last"] = wall
    today["refreshes"] += 1
    today["updated"] = wall
    state[day] = today
    for old in sorted(state)[:-KEEP_DAYS]:
        state.pop(old, None)
    _save(state)
    return today


def top(n: int = None, now=None, do_refresh: bool = True) -> dict:
    n = TOP_N if n is None else n
    now = now or datetime.now(TZ)
    day = today_str(now)
    today = refresh(now) if do_refresh else ((_load().get(day)) or
                                             {"coins": {}, "refreshes": 0})
    rows = []
    for base, rec in (today.get("coins") or {}).items():
        srcs = rec.get("srcs") or {}
        sides = {v.get("side") for v in srcs.values() if v.get("side")}
        rows.append({
            "base": base,
            # Two engines pointing OPPOSITE ways is not two engines agreeing.
            # Presenting it as "2 個引擎" was a confluence claim the data does
            # not support, and it is exactly the row a reader would act on.
            "conflict": len(sides) > 1,
            "side": (next(iter(sides)) if len(sides) == 1 else None),
            # Breadth first, then persistence. A coin three engines flagged
            # once beats one engine that flagged the same coin thirty times.
            "engines": len(srcs),
            "hits": rec.get("hits", 0),
            "last": rec.get("last", 0),
            "why": [{"src": s, "label": SRC_ZH.get(s, s),
                     "note": v.get("note"), "n": v.get("n", 0)}
                    for s, v in sorted(srcs.items(), key=lambda kv: -kv[1]["n"])],
        })
    # Breadth, then persistence, then RECENCY. Alphabetical was the original
    # last resort and it put 0G and A at the top of a five-way tie on the
    # first refresh of a day — an ordering that means nothing and looks like
    # one that does.
    # Agreeing coins first: a conflicted pair ranks below a clean single.
    rows.sort(key=lambda r: (r["conflict"], -r["engines"], -r["hits"],
                             -r["last"], r["base"]))
    return {"date": day, "refreshes": today.get("refreshes", 0),
            "updated": today.get("updated"),
            "tracked": len(today.get("coins") or {}),
            "top": rows[:n]}
