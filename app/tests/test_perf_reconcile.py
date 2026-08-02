"""/performance reconciliation — the tracked record vs the exchanges' own.

The page already said the two "differ on purpose", which is true and useless:
it left the SIZE of the gap to the reader. The tracked record contains phantom
entries (signals recorded and then SCORED even though the order skipped, failed
or closed differently), so a paper record can read as a track record.
"""
import app as A


OK_REAL = {"ok": True, "n_trades": 12, "net": 3.5}
OK_BYBIT = {"ok": True, "n_trades": 8, "net": -1.25}
DEAD = {"ok": False, "error": "no keys"}


def test_phantom_when_nothing_was_ever_traded():
    r = A.perf_reconcile(20, 12, {"ok": True, "n_trades": 0, "net": 0.0}, DEAD)
    assert r["verdict"] == "phantom"
    assert "紙上紀錄" in r["note"]


def test_diverged_when_most_signals_never_reached_the_exchange():
    r = A.perf_reconcile(100, 60, {"ok": True, "n_trades": 10, "net": 1.0}, DEAD)
    assert r["verdict"] == "diverged"
    assert "以交易所分頁為準" in r["note"]


def test_ok_when_the_counts_are_in_the_same_ballpark():
    r = A.perf_reconcile(25, 15, OK_REAL, OK_BYBIT)
    assert r["verdict"] == "ok"
    assert r["exchange_trades"] == 20


def test_both_accounts_are_summed():
    r = A.perf_reconcile(25, 15, OK_REAL, OK_BYBIT)
    assert r["exchange_net"] == 2.25              # 3.5 + (-1.25)


def test_an_api_failure_is_not_evidence_of_a_discrepancy():
    """'Cannot tell' must never render as 'phantom'."""
    r = A.perf_reconcile(20, 12, DEAD, DEAD)
    assert r["verdict"] is None
    assert r["exchange_trades"] is None and r["exchange_net"] is None
    assert "無法" in r["note"]


def test_one_dead_account_still_reconciles_against_the_other():
    r = A.perf_reconcile(20, 12, DEAD, OK_BYBIT)
    assert r["verdict"] is not None
    assert r["exchange_trades"] == 8


def test_no_tracked_trades_is_not_a_phantom():
    r = A.perf_reconcile(0, 0, {"ok": True, "n_trades": 0, "net": 0.0}, DEAD)
    assert r["verdict"] == "ok"


def test_never_raises_on_junk():
    r = A.perf_reconcile(5, 2, {"ok": True, "n_trades": None, "net": None}, None)
    assert r["verdict"] is not None


def _as_admin(c):
    with A.app.app_context():
        u = A.User.query.filter_by(is_admin=True).first() or A.User.query.first()
        uid = u.id
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def test_the_page_renders_the_banner(monkeypatch):
    """End to end: the reconciliation must reach the template.

    Both exchange summaries are stubbed. Without that this test only passed on
    a machine holding live API keys — everywhere else both accounts read as
    unavailable, perf_reconcile correctly returned verdict=None (a failed API
    call is NOT evidence of a discrepancy), the banner was hidden by design,
    and the assert blamed the template for an environment problem.
    """
    import executor as _ex
    import strategy3_exec as _s3
    monkeypatch.setattr(_ex, "realized_pnl_summary", lambda *a, **k: OK_REAL)
    monkeypatch.setattr(_s3, "closed_pnl_summary", lambda *a, **k: OK_BYBIT)

    A.app.config["TESTING"] = True
    c = A.app.test_client()
    _as_admin(c)
    body = c.get("/performance").get_data(as_text=True)
    assert "對帳" in body
    assert "交易所實際淨損益" in body        # the summed figure reached the page too


def test_the_page_hides_the_banner_when_it_cannot_tell(monkeypatch):
    """The other half of the contract: with no usable exchange data the page
    must stay SILENT rather than imply the tracked record is phantom."""
    import executor as _ex
    import strategy3_exec as _s3
    monkeypatch.setattr(_ex, "realized_pnl_summary", lambda *a, **k: DEAD)
    monkeypatch.setattr(_s3, "closed_pnl_summary", lambda *a, **k: DEAD)

    A.app.config["TESTING"] = True
    c = A.app.test_client()
    _as_admin(c)
    body = c.get("/performance").get_data(as_text=True)
    assert body.count("對帳") == 0
    assert "<title" in body                  # the page still rendered fine
