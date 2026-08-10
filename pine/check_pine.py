"""Pine forward-reference checker — the compile error you cannot see locally.

Pine resolves identifiers TOP-DOWN: a name must be declared above every line
that reads it. Get it wrong and TradingView says `Undeclared identifier "x"
(CE10272)` — which is only discoverable by pasting the file into the browser.

2026-08-04: adding an S/R column to the All-in-One MTF panel put an input.int()
below the function that read it. Eight compile errors, seven of them cascades.
This script pointed at the exact line the browser did.

    python pine/check_pine.py pine/indicators/All-in-One_ULTIMATE.pine
    python pine/check_pine.py pine/indicators/*.pine

Scope: top-level declarations only. Function parameters and for-loop variables
are local bindings and are excluded — judging them by file order is what made
the first version of this script emit 78 false positives.

It does NOT type-check, balance blocks, or validate Pine semantics. A clean run
means "no name is used before it exists", nothing more.
"""
import re, sys
if len(sys.argv) < 2:
    print(__doc__)
    raise SystemExit(2)

BUILTIN = re.compile(r"^(ta|math|str|array|color|line|label|box|table|input|request|"
                     r"timeframe|syminfo|chart|barstate|strategy|plot|na|nz|time|open|"
                     r"high|low|close|volume|bar_index|last_bar_index|last_bar_time|hl2|"
                     r"hlc3|ohlc4|position|size|shape|location|extend|xloc|yloc|format|"
                     r"text|order|display|scale|barmerge|session|adjustment|dayofweek|"
                     r"currency|alert|log|matrix|map|switch|for|if|else|while|and|or|not|"
                     r"var|varip|true|false|int|float|bool|string|type|method|export|import)$")


def _strip(line):
    """Drop comments and string literals so their contents never look like code."""
    if line.lstrip().startswith("//"):
        return ""
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
    return line.split("//")[0]


def check(path):
    raw = open(path, encoding="utf-8").read().split("\n")
    code = [_strip(l) for l in raw]

    # Names bound LOCALLY — function parameters and for-loop variables. They are
    # not top-level declarations and must never be judged by file order.
    #
    # A signature may wrap across lines, and Pine is perfectly happy with that.
    # Matching only single-line signatures left every parameter of a wrapped one
    # unregistered, so each use inside the body looked like a forward reference
    # to whatever global shared its name — 12 false positives on the first
    # multi-line function in the tree. Continuation lines are joined until the
    # parentheses balance, then the parameters are read off the whole thing.
    local = set()
    for i, l in enumerate(code):
        if not re.match(r"^[A-Za-z_]\w*\s*\(", l):
            continue
        sig, depth = "", 0
        for cont in code[i:i + 12]:            # a signature is not 12 lines long
            sig += cont
            depth += cont.count("(") - cont.count(")")
            if depth <= 0:
                break
        m = re.match(r"^[A-Za-z_]\w*\s*\((.*)\)\s*=>", sig, re.S)
        if m:
            for part in m.group(1).split(","):
                w = re.findall(r"[A-Za-z_]\w*", part)
                if w:
                    local.add(w[-1])

    for l in code:
        for m in re.finditer(r"\bfor\s+\[?\s*([A-Za-z_]\w*)(?:\s*,\s*([A-Za-z_]\w*))?", l):
            local.update(g for g in m.groups() if g)

    decl = {}
    for i, l in enumerate(code, 1):
        if not l.strip() or l[:1].isspace():   # indented = inside a block
            continue
        m = re.match(r"^([A-Za-z_]\w*)\s*\(", l)
        if m and "=>" in l:
            decl.setdefault(m.group(1), i); continue
        m = re.match(r"^\[([^\]]+)\]\s*=", l)
        if m:
            for n in re.findall(r"[A-Za-z_]\w*", m.group(1)):
                decl.setdefault(n, i)
            continue
        m = re.match(r"^(?:var|varip)?\s*(?:float|int|bool|string|color|line|label|box|"
                     r"table|array<\w+>)?\s*([A-Za-z_]\w*)\s*(?::=|=)[^=]", l)
        if m:
            decl.setdefault(m.group(1), i); continue
        m = re.match(r"^type\s+([A-Za-z_]\w*)", l)
        if m:
            decl.setdefault(m.group(1), i)

    bad, seen = [], set()
    for i, l in enumerate(code, 1):
        if not l.strip():
            continue
        for n in re.findall(r"\b([A-Za-z_]\w*)\b", l):
            if BUILTIN.match(n) or n in local or n not in decl:
                continue
            if decl[n] > i and (i, n) not in seen:
                seen.add((i, n))
                bad.append((i, n, decl[n], raw[i - 1].strip()[:70]))
    return bad


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    fail = 0
    for path in sys.argv[1:]:
        bad = check(path)
        name = path.split("/")[-1]
        if bad:
            fail = 1
            print(f"\u274c {name}: {len(bad)} forward reference(s)")
            for i, n, d, txt in bad:
                print(f"   L{i}: uses '{n}', declared at L{d}")
                print(f"        {txt}")
        else:
            print(f"\u2705 {name}")
    raise SystemExit(fail)
