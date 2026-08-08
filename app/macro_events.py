"""
大事件 — the market-moving calendar, worded once for every report that shows it.

2026-08-08: the owner asked for "big events that will affect market" in the
daily report, and the report had in fact been printing 「無 — 平靜的總經日」every
day because its only calendar feed (ForexFactory) had been answering HTTP 429
for an unknown length of time. An empty fetch rendered as "nothing scheduled"
is a fabricated fact — the same trap as nz() on a missing reading.

So this module sits on market_intel.upcoming_macro() (three sources, disk
cache, stale-if-error) and owns two jobs the data layer should not:

  1. 中文 naming — the feeds are English ("Inflation Rate YoY"); the reports
     are 中文, and 「通膨年增率」is what the owner actually reads.
  2. Collapsing — one CPI release arrives as four rows (YoY/MoM × core), all
     at the same minute. Four lines of near-identical text is not more
     information, it is less readable. Same timestamp + same family = one line.

Both the private daily report and the public morning brief render through
here, so the two can never describe the same week differently.
"""
import re

# Ordered: the FIRST pattern that matches wins, so put families before members.
_ZH = (
    (r"fomc\s*(rate|interest|decision|利率)|fomc$", "FOMC 利率決議", "fomc"),
    (r"fomc\s*minutes", "FOMC 會議紀要", "fomc_min"),
    (r"core\s*inflation|core\s*cpi", "核心 CPI 通膨", "cpi"),
    (r"inflation\s*rate|consumer\s*price|cpi", "CPI 通膨", "cpi"),
    (r"\bppi\b|producer\s*price", "PPI 生產者物價", "ppi"),
    (r"non.?farm|nfp|employment\s*change", "非農就業", "nfp"),
    (r"unemployment\s*rate", "失業率", "nfp"),
    (r"initial\s*jobless", "初領失業金", "jobless"),
    (r"\bgdp\b", "GDP 成長率", "gdp"),
    (r"\bpce\b", "PCE 物價指數", "pce"),
    (r"retail\s*sales", "零售銷售", "retail"),
    (r"michigan", "密大消費者信心", "sentiment"),
    (r"consumer\s*confidence", "消費者信心", "sentiment"),
    (r"ism\s*manufacturing", "ISM 製造業", "ism"),
    (r"ism\s*services|ism\s*non", "ISM 服務業", "ism"),
    (r"housing\s*starts|building\s*permits", "房屋開工/建照", "housing"),
    (r"home\s*sales", "成屋銷售", "housing"),
    (r"durable\s*goods", "耐久財訂單", "durable"),
    (r"trade\s*balance", "貿易帳", "trade"),
)

# What a print is worth knowing about, roughly. Used only for ordering when
# several land in the same window — never to hide anything.
_WEIGHT = {"fomc": 100, "cpi": 90, "nfp": 85, "pce": 80, "ppi": 70, "gdp": 70,
           "fomc_min": 65, "retail": 55, "jobless": 45, "ism": 45,
           "sentiment": 35, "housing": 30, "durable": 30, "trade": 25}


def classify(title: str) -> tuple:
    """('CPI 通膨', 'cpi', weight) — English feed title → how we say it."""
    t = (title or "").strip().lower()
    for pat, zh, family in _ZH:
        if re.search(pat, t):
            return zh, family, _WEIGHT.get(family, 20)
    return (title or "").strip(), "", 10


# Which member of a multi-row release carries the number people quote. Without
# this, a collapsed CPI row either shows core-MoM's figures under a headline
# label (wrong) or no figures at all (useless) — the four rows genuinely
# disagree, so the group has to CHOOSE one and be labelled accordingly.
_HEADLINE = {
    "cpi": (r"^inflation rate yoy", "CPI 通膨年增"),
    "ppi": (r"^ppi (mom|yoy)", "PPI 生產者物價"),
    "nfp": (r"non.?farm", "非農就業"),
    "pce": (r"core pce.*yoy", "核心 PCE 年增"),
    "gdp": (r"gdp growth rate", "GDP 成長率"),
    "housing": (r"housing starts", "房屋開工"),
}


def collapse(events: list, tz) -> list:
    """[{when, zh, family, weight, forecast, previous, n}] — one row per
    release. Four CPI rows at the same minute become one, labelled and
    numbered by that family's HEADLINE member (see _HEADLINE); if the headline
    member isn't in the batch, the group keeps the first member's own label and
    numbers, never one member's figure under another member's name."""
    groups = {}
    for e in events or []:
        zh, family, weight = classify(e.get("title"))
        when = e["when"].astimezone(tz)
        key = (when.strftime("%Y-%m-%d %H:%M"), family or zh)
        g = groups.get(key)
        if g is None:
            groups[key] = {"when": when, "zh": zh, "family": family,
                           "weight": weight, "forecast": e.get("forecast"),
                           "previous": e.get("previous"), "n": 1,
                           "_headline": False}
            g = groups[key]
        else:
            g["n"] += 1
        pat, label = _HEADLINE.get(family, (None, None))
        if pat and not g["_headline"] and re.search(pat, (e.get("title") or "").lower()):
            g.update(zh=label, forecast=e.get("forecast"),
                     previous=e.get("previous"), _headline=True)
    rows = list(groups.values())
    rows.sort(key=lambda r: r["when"])
    return rows


def _fp(row: dict) -> str:
    f, p = row.get("forecast"), row.get("previous")
    if f is None and p is None:
        return ""
    bits = []
    if f is not None:
        bits.append(f"預估 {f}")
    if p is not None:
        bits.append(f"前值 {p}")
    return "（" + " · ".join(bits) + "）"


def lines(now, tz, days: int = 7, limit: int = 6, indent: str = "  ") -> list:
    """Display lines for the next `days`, or an HONEST line when the calendar
    could not be read. Never claims a quiet week it cannot verify."""
    try:
        import market_intel
        data = market_intel.upcoming_macro(now, days=days, limit=40)
    except Exception as exc:  # noqa: BLE001 — a report must still send
        return [f"{indent}總經行事曆讀取失敗（{str(exc)[:60]}）"]

    if not data.get("ok"):
        return [f"{indent}⚠️ 總經行事曆暫時讀不到 — 這不代表沒有事件，請自行確認"]

    rows = collapse(data.get("events") or [], tz)[:limit]
    if not rows:
        return [f"{indent}今日無高影響美國數據" if days <= 1
                else f"{indent}未來 {days} 天沒有高影響美國數據"]

    out = []
    today = now.astimezone(tz).date()
    for r in rows:
        d = r["when"].date()
        when = ("今天" if d == today else
                "明天" if (d - today).days == 1 else
                r["when"].strftime("%m/%d"))
        star = "🔴 " if r["weight"] >= 85 else ""
        out.append(f"{indent}{star}{when} {r['when'].strftime('%H:%M')} "
                   f"{r['zh']}{_fp(r)}")
    if data.get("stale"):
        out.append(f"{indent}⚠️ 行事曆來源暫時無法更新，以上為最後一次成功取得的資料")
    return out


def today_lines(now, tz, indent: str = "  ") -> list:
    """Only what lands today — the 'do not hold 4x into this' line."""
    return lines(now, tz, days=1, limit=4, indent=indent)
