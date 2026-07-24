"""🪞 Copy-trading — encryption vault, follower store, mirror engine, routes.

Non-negotiables pinned here:
  • follower secrets never persist in plaintext, never leak to the web layer
  • a member can only ever touch their OWN record; admin endpoints are gated
  • one follower's failure never aborts the mirror or affects the master
  • nothing goes live unless the master switch AND per-follower approval are on
"""
import flask_login.utils

import app as APP
import config
import copy_engine
import copy_store
import copy_vault


# ── vault ────────────────────────────────────────────────────────────────────
def test_vault_round_trip():
    tok = copy_vault.encrypt("my-secret-key")
    assert tok != "my-secret-key"                    # actually ciphertext
    assert copy_vault.decrypt(tok) == "my-secret-key"


def test_vault_try_decrypt_bad_token():
    plain, err = copy_vault.try_decrypt("not-a-real-token")
    assert plain is None and err


def test_vault_fingerprint_hides_the_middle():
    assert copy_vault.fingerprint("ABCD1234WXYZ5678") == "ABCD…5678"
    assert copy_vault.fingerprint("short") == "•••••"     # too short to split


# ── store ────────────────────────────────────────────────────────────────────
def test_upsert_encrypts_and_lands_disabled():
    pub = copy_store.upsert_keys(7, "alice", "KEYAAAA1111", "SECRETZZZZ", 50)
    assert pub["enabled"] is False and pub["state"] == "pending"
    assert pub["key_fp"] == "KEYA…1111"
    rec = copy_store.get(7)
    # ciphertext on disk, NOT the plaintext
    assert rec["enc_secret"] != "SECRETZZZZ" and rec["enc_key"] != "KEYAAAA1111"
    k, s, err = copy_store.decrypt_creds(rec)
    assert err is None and k == "KEYAAAA1111" and s == "SECRETZZZZ"


def test_public_view_never_leaks_ciphertext():
    copy_store.upsert_keys(7, "alice", "KEYAAAA1111", "SECRETZZZZ", 50)
    pub = copy_store.get_public(7)
    assert "enc_key" not in pub and "enc_secret" not in pub


def test_margin_is_clamped():
    copy_store.upsert_keys(7, "a", "K", "S", 1)          # below floor
    assert copy_store.get_public(7)["margin_usdt"] == copy_store.MIN_MARGIN
    copy_store.set_margin(7, 999999)                     # above ceiling
    assert copy_store.get_public(7)["margin_usdt"] == copy_store.MAX_MARGIN


def test_enable_then_disable_states():
    copy_store.upsert_keys(7, "a", "K", "S", 50)
    assert copy_store.set_enabled(7, True)["state"] == "active"
    assert [r["user_id"] for r in copy_store.enabled_records()] == [7]
    disabled = copy_store.set_enabled(7, False)
    assert disabled["state"] == "disabled"               # approved_once ⇒ not 'pending'
    assert copy_store.enabled_records() == []


def test_new_keys_reset_approval():
    copy_store.upsert_keys(7, "a", "K", "S", 50)
    copy_store.set_enabled(7, True)
    # re-submitting keys must drop back to disabled (needs re-approval)
    pub = copy_store.upsert_keys(7, "a", "K2", "S2", 50)
    assert pub["enabled"] is False


def test_remove():
    copy_store.upsert_keys(7, "a", "K", "S", 50)
    assert copy_store.remove(7) is True
    assert copy_store.get(7) is None and copy_store.remove(7) is False


# ── engine ───────────────────────────────────────────────────────────────────
class _FakeClient:
    """Minimal ccxt-bybit stand-in recording the orders it's asked to place."""
    def __init__(self, positions=None, fail=False):
        self.orders, self._positions, self.fail = [], positions or [], fail

    def set_leverage(self, lev, symbol):
        pass

    def price_to_precision(self, symbol, p):
        return str(p)

    def market(self, symbol):
        return {"id": symbol.replace("/", "").replace(":", "")}

    def create_order(self, symbol, typ, side, qty, params=None):
        if self.fail:
            raise RuntimeError("insufficient balance")
        self.orders.append((symbol, side, qty, params))
        return {"id": "oid-1"}

    def fetch_positions(self, symbols):
        return self._positions


def test_mirror_open_dry_run_when_master_switch_off():
    # conftest keeps COPY_TRADING_LIVE False → no client is ever built
    copy_store.upsert_keys(7, "a", "K", "S", 50)
    copy_store.set_enabled(7, True)
    summ = copy_engine.mirror_open("XAUT/USDT:USDT", "long", 2000.0, 1970.0, 50)
    assert summ["live"] is False and summ["total"] == 1 and summ["done"] == 1
    assert copy_engine.status_for(7)["state"] == "dry"


def test_mirror_open_live_sizes_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(config, "COPY_TRADING_LIVE", True)
    # market metadata + sizing come from strategy3_exec — stub deterministically
    monkeypatch.setattr(copy_engine.X, "_market_limits",
                        lambda sym: (0.001, 0.001, 5.0))
    monkeypatch.setattr(copy_engine.X, "qty_for",
                        lambda price, margin, lev, step, mq, mn: (margin / price, ""))
    good, bad = _FakeClient(), _FakeClient(fail=True)
    clients = {"KGOOD": good, "KBAD": bad}
    monkeypatch.setattr(copy_engine, "_client", lambda k, s: clients[k])

    copy_store.upsert_keys(1, "good", "KGOOD", "S", 100)
    copy_store.set_enabled(1, True)
    copy_store.upsert_keys(2, "bad", "KBAD", "S", 100)
    copy_store.set_enabled(2, True)

    summ = copy_engine.mirror_open("XAUT/USDT:USDT", "long", 2000.0, 1970.0, 50)
    assert summ["total"] == 2 and summ["done"] == 1 and summ["failed"] == 1
    assert good.orders and good.orders[0][1] == "buy"        # good follower filled
    assert copy_engine.status_for(1)["state"] == "active"
    assert copy_engine.status_for(2)["state"] == "error"     # bad isolated, not raised


def test_mirror_close_reduce_only(monkeypatch):
    monkeypatch.setattr(config, "COPY_TRADING_LIVE", True)
    c = _FakeClient(positions=[{"symbol": "XAUT/USDT:USDT", "side": "long",
                                "contracts": 0.05}])
    monkeypatch.setattr(copy_engine, "_client", lambda k, s: c)
    copy_store.upsert_keys(1, "a", "K", "S", 100)
    copy_store.set_enabled(1, True)
    summ = copy_engine.mirror_close("XAUT/USDT:USDT", "opposite flag")
    assert summ["done"] == 1
    assert c.orders[0][1] == "sell" and c.orders[0][3]["reduceOnly"] is True


def test_validate_ok_and_bad(monkeypatch):
    class _Bal:
        def fetch_balance(self):
            return {"info": {"result": {"list": [{"totalEquity": "421.5"}]}}}
    monkeypatch.setattr(copy_engine, "_probe_client", lambda k, s: _Bal())
    v = copy_engine.validate("K", "S")
    assert v["ok"] is True and v["equity"] == 421.5

    def _boom(k, s):
        raise copy_engine.ccxt.AuthenticationError("bad key")
    monkeypatch.setattr(copy_engine, "_probe_client", _boom)
    v2 = copy_engine.validate("K", "S")
    assert v2["ok"] is False and v2["error"]


# ── routes / auth ────────────────────────────────────────────────────────────
class _User:
    is_active = True
    is_anonymous = False

    def __init__(self, uid, name, admin=False):
        self.id, self.username, self.is_admin = uid, name, admin
        self.is_authenticated = True

    def get_id(self):
        return str(self.id)


def _as(monkeypatch, user):
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: user)


def test_enroll_stores_only_own_record(monkeypatch):
    monkeypatch.setattr(copy_engine, "validate",
                        lambda k, s: {"ok": True, "equity": 100.0, "error": None})
    with APP.app.test_request_context(
            "/api/copy/enroll", method="POST",
            data={"api_key": "KEYXXXX9999", "api_secret": "SEC", "margin": "60"}):
        _as(monkeypatch, _User(5, "bob"))
        resp = APP.api_copy_enroll()
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"] is True
    rec = copy_store.get_public(5)
    assert rec and rec["margin_usdt"] == 60 and rec["enabled"] is False


def test_enroll_rejects_invalid_keys(monkeypatch):
    monkeypatch.setattr(copy_engine, "validate",
                        lambda k, s: {"ok": False, "equity": None, "error": "金鑰無效"})
    with APP.app.test_request_context(
            "/api/copy/enroll", method="POST",
            data={"api_key": "K", "api_secret": "S", "margin": "60"}):
        _as(monkeypatch, _User(5, "bob"))
        resp, code = APP.api_copy_enroll()
    assert code == 400 and copy_store.get(5) is None


def test_member_cannot_toggle_others(monkeypatch):
    copy_store.upsert_keys(9, "victim", "K", "S", 50)
    with APP.app.test_request_context(
            "/api/copy/admin/toggle", method="POST",
            data={"user_id": "9", "enabled": "true"}):
        _as(monkeypatch, _User(5, "bob", admin=False))
        resp, code = APP.api_copy_admin_toggle()          # admin_required → 403 JSON
    assert code == 403 and resp.get_json() == {"error": "admin only"}
    assert copy_store.get(9)["enabled"] is False          # victim NOT enabled


def test_admin_can_toggle(monkeypatch):
    copy_store.upsert_keys(9, "follower", "K", "S", 50)
    with APP.app.test_request_context(
            "/api/copy/admin/toggle", method="POST",
            data={"user_id": "9", "enabled": "true"}):
        _as(monkeypatch, _User(1, "owner", admin=True))
        resp = APP.api_copy_admin_toggle()
    assert resp.get_json()["ok"] is True
    assert copy_store.get(9)["enabled"] is True


def test_copy_page_renders_member_and_admin(monkeypatch):
    copy_store.upsert_keys(3, "carol", "KEYCAROL999", "S", 50)
    # member: own card, NO admin table
    with APP.app.test_request_context("/copy"):
        _as(monkeypatch, _User(3, "carol"))
        html = APP.copy_page()
    assert "我的跟單設定" in html and "管理員 · 跟單者名單" not in html
    # admin: sees the follower table
    with APP.app.test_request_context("/copy"):
        _as(monkeypatch, _User(1, "owner", admin=True))
        html2 = APP.copy_page()
    assert "管理員 · 跟單者名單" in html2 and "carol" in html2
    # neither view leaks a secret
    assert "SECRETZZZZ" not in html2
