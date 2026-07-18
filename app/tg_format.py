"""
Shared Telegram message house style — ONE tidy, aligned look for every outward
signal message (S1 copy-trade feed, S2 ⭐ premium, whale tracker) so the promo
channel reads clean and consistent instead of three different layouts.

House style (中文為主, HTML parse_mode):
  <headline>                       ← icon · 方向 · 標的 · 週期
  <meta>                           ← 信心 / 槓桿 / context, ' · ' separated
  ━━━━━━━━━━
  <pre>進場  3,500                 ← monospace-aligned plan, uniform 2-char
  停損  3,430   −2.0%                labels (進場/停損/目標/終標) so columns
  目標  3,553   +1.5% · 0.75R        actually line up; prices right-justified
  終標  3,640   +4.0% · 2R</pre>
  ━━━━━━━━━━
  💱 Bybit 3,501 · 下單 ↗          ← always a TAPPABLE link (never a bare URL)
  ⚠️ 非投資建議

Every helper is failure-safe: a Bybit hiccup degrades to a Binance reference
price + chart link, never a crash.
"""

import html as _html
import unicodedata as _ud

DIV = "━━━━━━━━━━"
DISCLAIMER = "⚠️ 訊號僅供參考，非投資建議"


def esc(s) -> str:
    """HTML-escape arbitrary text for parse_mode='HTML' messages — one bare
    '&' or '<' in a news title / exception string 400s the whole send."""
    return _html.escape(str(s), quote=False)


def disp_w(s: str) -> int:
    """Display width of a string in a monospace cell: CJK/fullwidth chars
    render two columns wide, everything else one."""
    return sum(2 if _ud.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def pad(s, width: int) -> str:
    """Left-align `s` inside `width` display columns (CJK-aware)."""
    s = str(s)
    return s + " " * max(0, width - disp_w(s))


def rpad(s, width: int) -> str:
    """Right-align `s` inside `width` display columns (CJK-aware)."""
    s = str(s)
    return " " * max(0, width - disp_w(s)) + s


def pre_table(rows, *, align=None, gap: str = "  ") -> str:
    """The aligned-columns workhorse: rows of cells → ONE <pre> block whose
    columns line up perfectly (CJK-aware padding, so 中文 labels and latin
    digits share a grid). align: per-column 'l'/'r' string, default first
    column left + the rest right (label + numbers). Cells are HTML-escaped.
    Empty/None cells are fine; short rows are fine. '' when rows is empty."""
    rows = [[("" if c is None else str(c)) for c in r] for r in rows if r]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    widths = [0] * ncol
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], disp_w(c))
    spec = (align or ("l" + "r" * (ncol - 1))).ljust(ncol, "r")
    out = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            cells.append(pad(c, widths[i]) if spec[i] == "l" else rpad(c, widths[i]))
        out.append(gap.join(cells).rstrip())
    return "<pre>" + "\n".join(esc(ln) for ln in out) + "</pre>"


def dir_zh(direction, *, arrow: bool = True) -> str:
    """'🟢 做多' / '🔴 做空' (arrow=False → just '做多'/'做空')."""
    is_long = str(direction).lower() in ("long", "buy", "多", "做多")
    word = "做多" if is_long else "做空"
    if not arrow:
        return word
    return ("🟢 " if is_long else "🔴 ") + word


def fmt_price(v) -> str:
    """Compact price: thousands-grouped, precision by magnitude, trailing
    zeros trimmed so 3500.00→'3,500' and 3552.50→'3,552.5'."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 1000:
        s = f"{v:,.2f}"
    elif a >= 1:
        s = f"{v:,.4f}"
    elif a >= 0.01:
        s = f"{v:.6f}"
    else:
        s = f"{v:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def signed_pct(entry, target, is_long) -> str:
    """Signed % move entry→target, favourable-positive, with a real minus glyph:
    '+1.5%' / '−2.0%'."""
    if not entry:
        return ""
    raw = (float(target) - float(entry)) / float(entry) * 100.0
    v = raw if is_long else -raw
    return f"+{v:.1f}%" if v >= 0 else f"−{abs(v):.1f}%"


def pct(v, decimals: int = 1, *, plus: bool = True) -> str:
    """A raw percentage with a real minus glyph so up/down reads consistently
    everywhere: 2.5 → '+2.5%', -2.0 → '−2.0%'. plus=False drops the leading +."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    body = f"{abs(v):.{decimals}f}%"
    if v < 0:
        return "−" + body
    return ("+" + body) if plus else body


def headline(*bits) -> str:
    """The standard first line: non-empty pieces joined with ' · '. Keeps every
    message's header shaped the same (icon · 標的 · context)."""
    return " · ".join(str(b) for b in bits if b not in (None, ""))


def mono_plan(entry, sl, tp1, tp2, *, is_long) -> str:
    """The monospace-aligned <pre> plan block. R multiples are computed from the
    levels themselves (target distance ÷ stop distance). Returns '' if the plan
    is incomplete or the stop equals entry."""
    vals = (entry, sl, tp1, tp2)
    if any(v is None for v in vals) or entry == sl:
        return ""
    risk = abs(float(entry) - float(sl))

    def rr(t):
        return abs(float(t) - float(entry)) / risk if risk else None

    rows = [
        ("進場", entry, None),
        ("停損", sl, None),
        ("目標", tp1, rr(tp1)),
        ("終標", tp2, rr(tp2)),
    ]
    prices = [fmt_price(p) for _, p, _ in rows]
    w = max(len(s) for s in prices)
    out = []
    for (label, p, r), ps in zip(rows, prices, strict=True):
        line = f"{label} {ps.rjust(w)}"
        extras = []
        if label != "進場":
            extras.append(signed_pct(entry, p, is_long))
        if r:
            extras.append(f"{r:g}R")
        if extras:
            line += "   " + " · ".join(extras)
        out.append(line)
    return "<pre>" + "\n".join(out) + "</pre>"


def bybit_line(base, ref_price=None) -> str:
    """'💱 Bybit 3,501 · 下單 ↗' — a TAPPABLE Bybit link when the coin trades
    there, else a Binance reference price + a TradingView chart link. The label
    stays honest ('參考價' when Bybit doesn't list it)."""
    px = url = None
    try:
        import bybit_data
        px = bybit_data.last_price(base)
        url = bybit_data.trade_url(base)
    except Exception:  # noqa: BLE001 — a Bybit blip must never break an alert
        pass
    if px is not None:
        label = f"💱 Bybit {fmt_price(px)}"
    elif ref_price:
        label = f"💱 參考價 {fmt_price(ref_price)}"
    else:
        label = "💱 Bybit"
    if url:
        return f'{label} · <a href="{url}">下單 ↗</a>'
    tv = f"https://www.tradingview.com/chart/?symbol=BINANCE:{str(base).upper()}USDT.P"
    return f'{label} · <a href="{tv}">看圖 ↗</a>'
