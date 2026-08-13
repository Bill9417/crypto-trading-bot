"""📓 OI at S1 signal time — and the refusal to answer early.

The request was "add OI to S1 to know if it will be better". It was measured
first, and the measurement said the question cannot be answered backwards:
Binance keeps ~30 days of OI history, and S1 fires 0.1 times per symbol per 30
days, so the whole window holds about three S1 trades.

The tests that matter are therefore about REFUSING to produce a number from a
sample that cannot carry one — that is the failure mode this repo has already
paid for 46 times out of 48.
"""
import pytest

import s1_oi_capture as S


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_FILE", str(tmp_path / "s1oi.json"))
    return tmp_path / "s1oi.json"


def _rows(n, state="longs_opening"):
    st = S._blank()
    st["started"] = 1000.0
    st["rows"] = [{"symbol": f"S{i}", "direction": "LONG", "state": state,
                   "oi_pct": 1.0, "px_pct": 1.0, "pctile": 50.0, "ts": 1000.0 + i}
                  for i in range(n)]
    return st


def test_a_tiny_sample_refuses_to_produce_a_verdict():
    """THE point of the module. Three trades split two ways gives two groups of
    one or two and a number that LOOKS like an answer — worse than no answer."""
    r = S.report(_rows(3))
    assert r["enough"] is False
    assert "樣本太少" in r["verdict"]
    assert "exp" not in r and "expectancy" not in r


def test_it_says_how_many_more_are_needed():
    r = S.report(_rows(4))
    assert r["need"] == S.MIN_N_FOR_SPLIT - 4


def test_an_empty_capture_explains_why_rather_than_looking_broken():
    """S1 firing 0.1×/symbol/30d means an empty store is the EXPECTED state for
    a long time. Silence here would read as a broken feature."""
    r = S.report(S._blank())
    assert r["n"] == 0
    assert "0.1" in r["verdict"]


def test_a_sufficient_sample_unlocks_the_split():
    r = S.report(_rows(S.MIN_N_FOR_SPLIT))
    assert r["enough"] is True
    assert "足夠" in r["verdict"]


def test_building_and_unwinding_are_counted_separately():
    st = _rows(10)
    for row in st["rows"][:4]:
        row["state"] = "longs_closing"
    r = S.report(st)
    assert r["building"] == 6 and r["unwinding"] == 4


def test_capture_never_raises_when_the_snapshot_fails(monkeypatch):
    """A missing OI reading must not stop an S1 signal. The trade is the
    product; this is bookkeeping about it."""
    monkeypatch.setattr(S, "oi_snapshot",
                        lambda sym: (_ for _ in ()).throw(RuntimeError("down")))
    assert S.note("BTC/USDT:USDT", "LONG", 6, 100.0) is False


def test_an_empty_snapshot_is_not_recorded_as_a_zero():
    """Writing zeros for a failed fetch would assert 'OI did not move', which
    is a fabricated reading — the exact nz()/fillna(0) trap this repo hit
    before."""
    monkeypatch_ok = S.oi_snapshot
    S.oi_snapshot = lambda sym: {}
    try:
        assert S.note("BTC/USDT:USDT", "LONG", 6, 100.0) is False
        assert S.load()["rows"] == []
    finally:
        S.oi_snapshot = monkeypatch_ok


def test_a_capture_round_trips(monkeypatch):
    monkeypatch.setattr(S, "oi_snapshot", lambda sym: {
        "oi_pct": 4.2, "px_pct": 1.1, "state": "longs_opening",
        "pctile": 88.0, "samples": 190})
    assert S.note("BTC/USDT:USDT", "LONG", 6, 100.0, now=5.0) is True
    rows = S.load()["rows"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC/USDT:USDT" and rows[0]["state"] == "longs_opening"


def test_capture_is_wired_into_s1_and_only_as_capture():
    """It must never gate a signal. A filter added on the strength of three
    trades is the thing this whole module exists to avoid."""
    import inspect
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "bot.py"), encoding="utf-8").read()
    assert "s1_oi_capture.note(" in src
    block = src[src.index("s1_oi_capture") - 400:src.index("s1_oi_capture") + 400]
    for gate in ("if s1_oi_capture", "return False, \"oi", "skip"):
        assert gate not in block, f"OI is gating S1, not just being recorded: {gate}"
