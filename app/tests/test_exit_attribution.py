"""Who ENDED a trade, not just who started it.

Ownership at open was never the whole story. On 2026-07-28 the mirror opened
ATOM and AVAX and the operator closed both by hand hours later — ATOM ran to
S1's TP1 on paper (+4.0%) but realised ~+0.7%. Scored as S1, that is somebody
else's exit decision inside the strategy's expectancy; an average over a
mixture of the two measures neither.
"""
import time

import strategy_ledger as L

# Every write calls _prune(), which drops rows older than RETAIN_D days —
# so fixtures must use realistic epochs, not 1970.
NOW = time.time()


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "LEDGER_FILE", str(tmp_path / "ledger.json"))


def test_a_strategy_exit_is_attributed_to_the_strategy(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    L.record_open("S1", "ATOM/USDT:USDT", "short", ts=NOW - 100)
    L.record_close("S1", "ATOM/USDT:USDT", ts=NOW - 50)
    assert L.exit_kind("ATOMUSDT", NOW - 50) == "S1"
    assert L.owner_of("ATOMUSDT", NOW - 50) == "S1"        # ownership unchanged


def test_a_hand_close_is_attributed_to_manual(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    L.record_open("S1", "ATOM/USDT:USDT", "short", ts=NOW - 100)
    L.record_close("S1", "ATOM/USDT:USDT", ts=NOW - 50, by=L.MANUAL)
    assert L.exit_kind("ATOMUSDT", NOW - 50) == "manual"
    assert L.owner_of("ATOMUSDT", NOW - 50) == "S1"        # S1 still OPENED it


def test_intervention_stats_counts_the_mix(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    for i, by in enumerate([None, L.MANUAL, L.MANUAL, None]):
        sym = f"C{i}/USDT:USDT"
        L.record_open("S1", sym, "long", ts=NOW - 100 + i)
        L.record_close("S1", sym, ts=NOW - 50 + i, by=by)
    iv = L.intervention_stats("S1")
    assert iv["total"] == 4 and iv["manual"] == 2 and iv["by_strategy"] == 2
    assert iv["manual_pct"] == 50.0


def test_rows_predating_closed_by_are_unknown_not_assumed_clean(monkeypatch, tmp_path):
    """Silently counting old rows as strategy-closed would re-introduce the
    exact optimism this is meant to remove."""
    _fresh(monkeypatch, tmp_path)
    L._save({"rows": [{"strategy": "S1", "symbol": "OLDUSDT",
                       "opened": NOW - 100, "closed": NOW - 50}]})       # no closed_by
    iv = L.intervention_stats("S1")
    assert iv["unknown"] == 1 and iv["by_strategy"] == 0 and iv["manual"] == 0
    assert L.exit_kind("OLDUSDT", NOW - 50) == "S1"                 # legacy fallback


def test_open_positions_are_not_counted(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    L.record_open("S1", "X/USDT:USDT", "long", ts=NOW - 100)
    assert L.intervention_stats("S1")["total"] == 0


def test_stats_filter_by_strategy(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    L.record_open("S1", "A/USDT:USDT", "long", ts=NOW - 100)
    L.record_close("S1", "A/USDT:USDT", ts=NOW - 50, by=L.MANUAL)
    L.record_open("S3", "XAUT/USDT:USDT", "long", ts=NOW - 100)
    L.record_close("S3", "XAUT/USDT:USDT", ts=NOW - 50)
    assert L.intervention_stats("S1")["manual"] == 1
    assert L.intervention_stats("S3")["manual"] == 0
    assert L.intervention_stats()["total"] == 2          # unfiltered


# ── the mirror's two exit paths ──────────────────────────────────────────────
def test_mirror_reconciliation_records_a_manual_exit(monkeypatch, tmp_path):
    """_forget() runs when the position vanished OUTSIDE the mirror — that is
    by definition not S1's exit."""
    import config
    import s1_bybit_mirror as M
    import strategy3_exec as X
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "STATE_FILE", str(tmp_path / "pos.json"))
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(M, "_tg", lambda msg: None)

    L.record_open("S1", "ETH/USDT:USDT", "long", ts=NOW - 100)
    M._save({"ETH/USDT:USDT": {"side": "long", "qty": 1.0, "entry": 3500.0,
                               "sl": 3430.0, "ts": 1000}})
    M._forget("ETH/USDT:USDT", "已由其他方式平倉")
    assert L.intervention_stats("S1")["manual"] == 1
