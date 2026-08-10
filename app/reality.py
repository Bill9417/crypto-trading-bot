"""Reality Check — what the measured record actually says, not what it feels like.

signal_outcomes.py has been quietly evaluating every S2 signal against six exit
rules for months; at the time of writing that is 19,295 scored outcomes. All of
it was reachable only as Telegram text (/outcomes, /winrate), which is the worst
possible surface for a number you are supposed to *sit with* before sizing up.

This module turns that tally into a page. It computes nothing new — the numbers
are already on disk — but it does add the two things the Telegram tables never
had: a confidence interval, and an explicit verdict about whether the sample is
big enough to mean anything at all.

── ON THE CONFIDENCE INTERVAL ───────────────────────────────────────────────
The lifetime tally stores n, sum, wins, gain and loss. It does NOT store a sum
of squares, so the true per-trade variance is not recoverable from it, and an
exact CI is therefore impossible for historical data. Inventing one would be
worse than having none — a fabricated error bar on a page built to stop
self-deception is a contradiction.

So this computes the variance of the MINIMUM-VARIANCE distribution consistent
with the stored figures: every win sitting exactly at the average win, every
loss exactly at the average loss. Within-class spread can only add variance, so
that is a strict lower bound, and the interval it produces is the NARROWEST any
honest interval could be.

That asymmetry cuts ONE way, and it does not care about the sign of the result:

  · zero INSIDE the narrowest possible interval  → conclusively inconclusive.
    The real interval is wider, so it contains zero too. This is a verdict.
  · zero OUTSIDE it                              → proves nothing, in EITHER
    direction. The real interval is wider and may still contain zero. A
    negative result gets no more benefit of the doubt here than a positive one.

There is no bound in the other direction to lean on: `_settle` marks positions
still open at the end of the window to the last close, so R has no upper limit
and a Popoviciu-style maximum-variance bound does not exist.

`_accumulate` in signal_outcomes.py now also records `sumsq`, so exact
intervals become available as new outcomes land; `ci()` uses them the moment
a cohort has enough of them and says which basis it used.

── WHAT HAPPENED vs WHAT IT PREDICTS ────────────────────────────────────────
Keeping those two apart is the point of the page, because only one of them
needs statistics:

  · net R, win rate and profit factor are FACTS about trades that already
    happened. PF 0.87 means gross losses exceeded gross gains. No interval,
    no assumption, nothing to argue with.
  · expectancy is a PREDICTION about the next trade, and that is the number
    that needs an interval before it deserves to be believed.

So the board reports the realised record plainly and hedges only the forecast.
"""
import json
import math
import os

import signal_outcomes as SO

STATE_FILE = SO.STATE_FILE

# Below this many outcomes, no expectancy number deserves a verdict at all.
MIN_N_FOR_VERDICT = 30

# Plain-English names. The Telegram tables are Chinese; the web page carries
# both, because the rule names are jargon in either language.
RULE_LABEL = {
    "hold":       ("Hold to TP2/SL", "抱到底"),
    "be":         ("Stop to breakeven at TP1", "TP1 後移到成本"),
    "partial":    ("Take 50% at TP1", "TP1 減半"),
    "partial_be": ("50% at TP1 + breakeven", "減半 + 保本"),
    "tp1_only":   ("Close all at TP1", "TP1 全出"),
    "trail_1r":   ("Trail after 1R", "1R 後移動停利"),
}
COHORT_LABEL = {
    "all":     ("Every signal", "全部"),
    "long":    ("Longs only", "做多"),
    "short":   ("Shorts only", "做空"),
    "hc":      ("High conviction", "高信心"),
    "premium": ("Premium ⭐", "⭐ 精選"),
}


def load(path: str = None) -> dict:
    """The raw state file, or an empty shell when it does not exist yet."""
    try:
        with open(path or STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


# ── the statistics ───────────────────────────────────────────────────────────
def ci(bucket: dict, z: float = 1.96):
    """95% CI for mean R, plus the basis it was computed on.

    Returns (lo, hi, basis) where basis is "exact" when a stored sum of squares
    made a real variance available, and "floor" when it is the minimum-variance
    bound described in the module docstring. None when n is too small to say
    anything.
    """
    n = bucket.get("n") or 0
    if n < 2:
        return None
    mean = (bucket.get("sum") or 0.0) / n

    # "exact" requires the sum of squares to cover EVERY trade in the bucket.
    # sumsq started being recorded on 2026-08-10, so for a long time it will
    # describe a recent slice of a much older tally; mixing that slice's
    # variance with the whole bucket's mean and n would produce a confident
    # interval computed from data that does not match. Coverage is tracked
    # explicitly rather than inferred, and anything short of complete falls
    # through to the lower bound.
    sumsq = bucket.get("sumsq")
    covered = bucket.get("sumsq_n") or 0
    if isinstance(sumsq, (int, float)) and sumsq > 0 and covered >= n:
        var = (sumsq - n * mean * mean) / (n - 1)
        basis = "exact"
    else:
        wins = bucket.get("wins") or 0
        losses = n - wins
        gain = bucket.get("gain") or 0.0
        loss = bucket.get("loss") or 0.0
        if wins <= 0 or losses <= 0:
            return None                       # degenerate: one class only
        avg_w, avg_l = gain / wins, -loss / losses
        ss = wins * (avg_w - mean) ** 2 + losses * (avg_l - mean) ** 2
        var = ss / (n - 1)
        basis = "floor"

    if var <= 0:
        return None
    half = z * math.sqrt(var / n)
    return (mean - half, mean + half, basis)


def trades_needed(bucket: dict, z: float = 1.96):
    """How many outcomes it would take for THIS effect size to clear zero.

    Answers the question that actually matters when a result is inconclusive:
    is it 200 more trades away, or 200,000? Uses the same variance basis as
    ci(), so on 'floor' data it is an optimistic lower bound — the real number
    is larger. None when the effect is zero (no sample size ever settles it).
    """
    n = bucket.get("n") or 0
    if n < 2:
        return None
    mean = (bucket.get("sum") or 0.0) / n
    if mean == 0:
        return None
    got = ci(bucket, z)
    if not got:
        return None
    lo, hi, _ = got
    half = (hi - lo) / 2.0
    sd_over_sqrt_n = half / z
    sd = sd_over_sqrt_n * math.sqrt(n)
    need = (z * sd / abs(mean)) ** 2
    return int(math.ceil(need))


def row(bucket: dict, key: str, labels: dict) -> dict:
    """One scoreboard line: the numbers plus the honest reading of them."""
    n = bucket.get("n") or 0
    en, zh = labels.get(key, (key, key))
    out = {"key": key, "label": en, "label_zh": zh, "n": n}
    if n <= 0:
        return {**out, "exp": None, "wr": None, "pf": None, "net": None,
                "ci": None, "basis": None, "verdict": "no data"}
    total = bucket.get("sum") or 0.0
    loss = bucket.get("loss") or 0.0
    out.update({
        "exp": total / n,
        "net": total,
        "wr": 100.0 * (bucket.get("wins") or 0) / n,
        "pf": ((bucket.get("gain") or 0.0) / loss) if loss > 0 else None,
    })
    got = ci(bucket)
    if got:
        lo, hi, basis = got
        out["ci"] = [lo, hi]
        out["basis"] = basis
        out["straddles_zero"] = lo <= 0 <= hi
    else:
        out["ci"] = None
        out["basis"] = None
        out["straddles_zero"] = None
    out["need"] = trades_needed(bucket)
    out["verdict"] = verdict(out)
    out["tag"] = tag(out)
    out["realised"] = realised(out)
    return out


def tag(r: dict) -> str:
    """Two or three words for the table cell.

    The full verdict() sentence is correct but identical on every row when the
    whole board points the same way, and eleven copies of the same paragraph is
    noise that hides the numbers — on a phone it was clipped mid-word. The page
    prints the sentence ONCE, under the table, and each row carries only what
    actually differs between rows.
    """
    n, exp = r.get("n") or 0, r.get("exp")
    if exp is None or n < MIN_N_FOR_VERDICT:
        return "too few"
    if r.get("straddles_zero"):
        return "no edge"
    exact = r.get("basis") == "exact"
    if exp < 0:
        return "negative" if exact else "probably negative"
    return "positive" if exact else "probably positive"


def basis_note(rows: list) -> str:
    """The single sentence the table's tags all mean, chosen from what is on
    the board so it never claims more than the rows do."""
    if not rows:
        return ""
    if any(r.get("basis") == "floor" for r in rows):
        return ("“Probably” is doing real work here: the lifetime tally records "
                "totals but not a sum of squares, so the range shown is the "
                "narrowest one consistent with the data. The true range is wider. "
                "That is enough to rule an edge OUT when the range already "
                "touches zero, and never enough to rule one IN. Exact ranges "
                "start appearing as new outcomes are scored.")
    return ("Ranges are exact — computed from the recorded variance of every "
            "trade in the bucket.")


def realised(r: dict) -> str:
    """What ALREADY happened, stated without a hedge because it is not a
    forecast. PF < 1 means gross losses exceeded gross gains over these exact
    trades — an arithmetic fact, not an inference."""
    n, net, pf = r.get("n") or 0, r.get("net"), r.get("pf")
    if not n or net is None:
        return ""
    verb = "lost" if net < 0 else "made"
    pf_txt = f" · PF {pf:.2f}" if pf else ""
    return f"{verb} {abs(net):,.0f}R over {n:,} trades{pf_txt}"


def verdict(r: dict) -> str:
    """What the FORECAST is worth — deliberately not a summary of the P&L.

    The realised record (net, win rate, PF) is stated as fact elsewhere and
    needs no hedging. This hedges only the claim about the next trade.

    A 'floor' interval licenses exactly one conclusion — "inconclusive" when it
    already contains zero — and it licenses it regardless of sign. A negative
    result gets no free pass here: an interval that is the narrowest possible
    cannot establish significance in either direction, so a losing rule reads
    as "probably losing", not "proven losing".
    """
    n, exp = r.get("n") or 0, r.get("exp")
    if exp is None or n < MIN_N_FOR_VERDICT:
        return "too few trades to forecast anything"
    if r.get("straddles_zero"):
        return "no measurable edge — even the narrowest range contains zero"
    exact = r.get("basis") == "exact"
    if exp < 0:
        return ("negative, and the interval clears zero" if exact
                else "probably negative — lost money over the sample, but the "
                     "interval shown is the narrowest possible")
    return ("positive, and the interval clears zero" if exact
            else "probably positive — but the interval shown is the narrowest "
                 "possible, so this is not yet proven")


# ── the board ────────────────────────────────────────────────────────────────
def rules_board(state: dict, cohort: str = "all") -> list:
    """Every exit rule, on one cohort, worst-to-best by expectancy."""
    totals = ((state.get("totals") or {}).get(cohort) or {})
    rows = [row(totals.get(k) or {}, k, RULE_LABEL) for k in SO.EXIT_RULES]
    rows = [r for r in rows if r["n"] > 0]
    rows.sort(key=lambda r: (r["exp"] is None, -(r["exp"] or 0)))
    return rows


def cohorts_board(state: dict, rule: str = SO.BASE_RULE) -> list:
    """Every cohort, on one exit rule, best-to-worst by expectancy."""
    totals = state.get("totals") or {}
    rows = [row((totals.get(c) or {}).get(rule) or {}, c, COHORT_LABEL)
            for c in COHORT_LABEL]
    rows = [r for r in rows if r["n"] > 0]
    rows.sort(key=lambda r: (r["exp"] is None, -(r["exp"] or 0)))
    return rows


def win_rate_lesson(rules: list) -> dict:
    """The single most expensive lesson in this repo, stated from live data.

    48 mean-reversion configs, a 5-year TW backtest and a Bybit account have all
    made the same point: the highest win rate in a set is routinely NOT the rule
    that makes money. If the measured record says it again, say it out loud —
    with both rules named, so it reads as evidence rather than a slogan.
    """
    scored = [r for r in rules if r.get("wr") is not None and r.get("exp") is not None]
    if len(scored) < 2:
        return {}
    best_wr = max(scored, key=lambda r: r["wr"])
    best_exp = max(scored, key=lambda r: r["exp"])
    if best_wr["key"] == best_exp["key"]:
        return {"agree": True, "rule": best_wr["label"],
                "wr": best_wr["wr"], "exp": best_wr["exp"]}
    return {
        "agree": False,
        "wr_rule": best_wr["label"], "wr_wr": best_wr["wr"], "wr_exp": best_wr["exp"],
        "exp_rule": best_exp["label"], "exp_wr": best_exp["wr"], "exp_exp": best_exp["exp"],
    }


def headline(state: dict, rules: list) -> dict:
    """The one thing to read if you read nothing else."""
    all_tot = ((state.get("totals") or {}).get("all") or {})
    base = all_tot.get(SO.BASE_RULE) or {}
    n = base.get("n") or 0
    positive = [r for r in rules if (r.get("exp") or 0) > 0]
    proven = [r for r in positive if r.get("straddles_zero") is False
              and r.get("basis") == "exact"]
    return {
        "n": n,
        "signals": len(state.get("evaluated") or {}),
        "since": state.get("totals_since"),
        "rules_tested": len(rules),
        "rules_positive": len(positive),
        "rules_proven": len(proven),
        "any_edge": bool(proven),
        "best": rules[0] if rules else None,
    }


def board(path: str = None, cohort: str = "all", rule: str = SO.BASE_RULE) -> dict:
    """Everything /reality renders, in one JSON-safe dict."""
    state = load(path)
    rules = rules_board(state, cohort)
    cohorts = cohorts_board(state, rule)
    return {
        "ok": True,
        "cohort": cohort,
        "rule": rule,
        "rules": rules,
        "cohorts": cohorts,
        "headline": headline(state, rules),
        "lesson": win_rate_lesson(rules),
        "basis_note": basis_note(rules),
        "rule_keys": list(SO.EXIT_RULES),
        "cohort_keys": list(COHORT_LABEL),
        "min_n": MIN_N_FOR_VERDICT,
    }


def _fmt_env() -> str:
    return os.path.basename(STATE_FILE)
