"""
Strategy 3 — "why is it not in a position right now?", in one card.

2026-08-08: the owner watched XAUT print score 97.5/100, Vegas green, MSB bull
and an ALL-LONG multi-timeframe bias, and asked why the bot had not gone long.
Answering it took a 40-line throwaway diagnostic, because strategy3_state.json
recorded WHAT the engine thought (last_flag / consumed) but never WHEN the flag
fired or WHY the entry did not happen. This module is that answer, and the
scanner now persists the two missing facts (flag_ts, skip_reason) so it can be
given without a script.

The rule that surprises people, and that caused this question:

    flag-flip enters on the FLIP EVENT of a closed bar, and flags strictly
    ALTERNATE long → short → long.

So a chart that merely LOOKS long is not a signal — the long flag for this leg
may already have been spent days ago. Score, Vegas and MSB describe the same
leg the engine already acted on; they are not a fresh trigger.

explain() is pure (no network, no config) so the wording can be unit-tested
against a hand-built state dict. card() renders it; report_tg() is the /xaut
command and the message the scanner posts to the 📈 S1 topic on every change.
"""
import time

# Every state explain() can return. Ordered by how the loop reaches them.
STATES = ("no_data", "holding", "blocked", "no_signal_yet",
          "spent", "waiting_vegas", "arming")

_DIR_TXT = {"long": "做多 LONG", "short": "做空 SHORT"}


def _age(ts, now) -> str:
    """'93 小時前' — the fact that was missing from the state file."""
    if not ts:
        return "時間未記錄"
    h = max(0.0, (now - float(ts)) / 3600.0)
    if h < 1:
        return f"{int(h * 60)} 分鐘前"
    if h < 48:
        return f"{h:.0f} 小時前"
    return f"{h / 24:.1f} 天前"


def explain(base: str, st: dict, params: dict, *, blocked: str = "",
            now: float = None) -> dict:
    """Why S3 is (or is not) holding `base` right now.

    st      one symbol's slice of strategy3_state.json
    params  config.strategy3_params(base)
    blocked '' when entries may proceed, else the reason they may not
            (alert-only mode, circuit breaker, daily loss limit)

    Mirrors decide() in strategy3_scanner exactly — if that changes, this must.
    Returns {state, headline, next_step, ...raw fields} with state in STATES.
    """
    now = now or time.time()
    st = st or {}
    occ = params.get("engine") == "occ"
    word = "交叉" if occ else "旗標"          # OCC crosses vs flag-flip flags
    tf = params.get("timeframe", "30m")

    flag = st.get("last_flag")
    consumed = bool(st.get("consumed"))
    holding = st.get("pos_dir")
    vegas = st.get("last_vegas")
    score = st.get("last_score")
    out = {
        "base": base, "timeframe": tf, "engine": params.get("engine", "flagflip"),
        "flag": flag, "flag_ago": _age(st.get("flag_ts"), now),
        "consumed": consumed, "holding": holding, "score": score,
        "vegas": vegas, "price": st.get("last_price"),
        "skip_reason": st.get("skip_reason") or "",
        "seen_ago": _age(st.get("last_seen"), now),
        "attempts": int(st.get("open_attempts") or 0),
    }

    if not st.get("last_seen"):
        return {**out, "state": "no_data",
                "headline": f"{base} 還沒有掃描紀錄",
                "next_step": "等 S3 掃描器跑過第一根收盤 K 棒。"}

    if holding:
        return {**out, "state": "holding",
                "headline": f"{base} 目前持有 {_DIR_TXT.get(holding, holding)}",
                "next_step": f"出場條件是反向{word} — 不設固定停利。"}

    if blocked:
        return {**out, "state": "blocked",
                "headline": f"{base} 目前不會進場：{blocked}",
                "next_step": "先解除上面的限制，訊號才會被執行。"}

    if not flag:
        return {**out, "state": "no_signal_yet",
                "headline": f"{base} 還沒有任何{word}",
                "next_step": f"等第一個{word}出現。"}

    d = _DIR_TXT.get(flag, flag)
    opposite = _DIR_TXT["short" if flag == "long" else "long"]
    if consumed:
        # The case that prompted this module: the leg's flag is spent, so the
        # chart can look perfect for days and nothing will happen.
        why = out["skip_reason"]
        head = f"{base} 最近一次{word}是 {d}（{out['flag_ago']}），已經用掉了"
        if why:
            head = (f"{base} 最近一次{word}是 {d}（{out['flag_ago']}），"
                    f"沒有進場：{why}")
        return {**out, "state": "spent", "headline": head,
                "next_step": (f"{word}是輪流出現的 — 要先跑出一個 {opposite} {word}，"
                              f"下一個 {d} 才算新訊號。在那之前分數再高也不會進場。")}

    agrees = ((flag == "long" and (vegas or 0) > 0)
              or (flag == "short" and (vegas or 0) < 0))
    if not occ and not agrees:
        return {**out, "state": "waiting_vegas",
                "headline": f"{base} 有 {d} {word}（{out['flag_ago']}），但 Vegas 還沒同意",
                "next_step": f"Vegas 轉成{'綠' if flag == 'long' else '紅'}的那根收盤就進場。"}

    return {**out, "state": "arming",
            "headline": f"{base} {d} {word}成立（{out['flag_ago']}），準備進場",
            "next_step": "下一根收盤 K 棒送單。"}


def card(info: dict) -> str:
    """The Telegram message. No sizes, no balances, no free margin — this goes
    to a PUBLIC group topic (see the guard test in test_protect_suite)."""
    icon = {"holding": "📌", "blocked": "🛑", "spent": "⏸️", "arming": "🎯",
            "waiting_vegas": "⏳", "no_signal_yet": "…", "no_data": "❔"}
    lines = [f"{icon.get(info['state'], 'ℹ️')} S3 · {info['base']} "
             f"{info['timeframe']}（Bybit）",
             "",
             info["headline"], info["next_step"], ""]

    bits = []
    if info.get("score") is not None:
        # .1f, not .0f — the chart shows 97.5 and a card that says 98 invites
        # exactly the "is this even the same number?" doubt this module exists
        # to remove.
        bits.append(f"分數 {info['score']:.1f}/100")
    v = info.get("vegas")
    if v is not None:
        bits.append("Vegas " + ("綠" if v > 0 else "紅" if v < 0 else "平"))
    if info.get("price"):
        bits.append(f"價格 {info['price']:.6g}")
    if bits:
        lines.append(" · ".join(bits))
    lines.append(f"最後一根收盤 {info['seen_ago']}")
    if info["state"] == "spent":
        # Said out loud because it is the whole misunderstanding: the meter and
        # the trigger are different questions.
        lines.append("")
        lines.append("（分數/Vegas 描述的是同一段趨勢，不是新的進場訊號。）")
    return "\n".join(lines)


def _blocked_reason() -> str:
    """'' when entries may proceed. Same three gates open_flip checks, in the
    same order, so the card can never disagree with the executor."""
    try:
        import strategy3_scanner
        blocked = strategy3_scanner.live_blocked()
        if blocked:
            return blocked
    except Exception as exc:  # noqa: BLE001 — a broken import must not hide the rest
        return f"無法讀取 S3 設定（{exc}）"
    for mod in ("strategy3_risk", "daily_risk"):
        try:
            why = __import__(mod).entry_blocked()
            if why:
                return why
        except Exception:  # noqa: BLE001 — the gates themselves fail open
            continue
    return ""


def snapshot(base: str = None) -> list:
    """[explain(...)] for every S3 symbol (or just `base`). Reads the state
    file the scanner writes — never recomputes the signal, so the card always
    shows what the running engine actually decided."""
    import config
    import strategy3_scanner
    state = strategy3_scanner.load_state()
    blocked = _blocked_reason()
    bases = [base.upper()] if base else list(config.STRATEGY3_SYMBOLS)
    out = []
    for b in bases:
        sym = f"{b}/{config.QUOTE_ASSET}:{config.QUOTE_ASSET}"
        out.append(explain(b, state.get(sym) or {},
                           config.strategy3_params(b), blocked=blocked))
    return out


def report_tg(base: str = None) -> str:
    """/xaut and /s3 — the same card the scanner posts when the state changes."""
    try:
        infos = snapshot(base)
    except Exception as exc:  # noqa: BLE001
        return f"讀不到 S3 狀態：{exc}"
    if not infos:
        return "S3 沒有設定任何標的。"
    return "\n\n".join(card(i) for i in infos)
