"""
📇 Follower store for copy-trading — the web process's source of truth.

Persists to copy_followers.json (gitignored — it holds encrypted API secrets).
Written ONLY by the web process (enroll / revoke / enable / margin); the
scanner process reads it but writes its runtime status to a SEPARATE file
(copy_status.json, see copy_engine) so the two processes never write the same
file and can't clobber each other.

Each record:
  user_id, username, key_fp (display-safe), enc_key, enc_secret (Fernet),
  margin_usdt, enabled, approved_once, created_at, updated_at

Secrets are encrypted via copy_vault before they ever reach disk.
"""
import json
import os
import threading
import time

import copy_vault

STORE_FILE = os.path.join(os.path.dirname(__file__), "copy_followers.json")
_lock = threading.Lock()

MIN_MARGIN = 5.0            # Bybit rejects dust; also a floor against fat-finger 0
MAX_MARGIN = 5000.0        # sanity ceiling — a follower can't mirror a whale size


def _clamp_margin(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return MIN_MARGIN
    return max(MIN_MARGIN, min(MAX_MARGIN, round(v, 2)))


def _load() -> list:
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt = no followers yet
        return []


def _save(rows: list) -> None:
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, STORE_FILE)


def _public(rec: dict) -> dict:
    """A record safe to hand to a template / JSON API — NO ciphertext, NO
    plaintext. Just what the UI needs to render."""
    enabled = bool(rec.get("enabled"))
    approved = bool(rec.get("approved_once"))
    state = "active" if enabled else ("disabled" if approved else "pending")
    return {
        "user_id": rec.get("user_id"),
        "username": rec.get("username"),
        "key_fp": rec.get("key_fp"),
        "margin_usdt": rec.get("margin_usdt"),
        "enabled": enabled,
        "state": state,                         # active | pending | disabled
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


# ── reads ──────────────────────────────────────────────────────────────────
def get(user_id: int) -> dict | None:
    for rec in _load():
        if rec.get("user_id") == user_id:
            return rec
    return None


def get_public(user_id: int) -> dict | None:
    rec = get(user_id)
    return _public(rec) if rec else None


def list_public() -> list:
    return [_public(r) for r in sorted(_load(),
            key=lambda r: (not r.get("enabled"), r.get("username") or ""))]


def enabled_records() -> list:
    """Full records (with ciphertext) for every enabled follower — the engine
    decrypts these. Never expose the result to the web layer."""
    return [r for r in _load() if r.get("enabled")]


def decrypt_creds(rec: dict):
    """(api_key, api_secret, None) or (None, None, reason). Never raises."""
    k, e1 = copy_vault.try_decrypt(rec.get("enc_key", ""))
    s, e2 = copy_vault.try_decrypt(rec.get("enc_secret", ""))
    if e1 or e2:
        return None, None, (e1 or e2)
    return k, s, None


# ── writes (web process only) ──────────────────────────────────────────────
def upsert_keys(user_id: int, username: str, api_key: str, api_secret: str,
                margin) -> dict:
    """Add or replace a follower's keys. Submitting keys ALWAYS resets enabled
    to False — new keys must be re-approved by an admin before they go live."""
    api_key, api_secret = api_key.strip(), api_secret.strip()
    with _lock:
        rows = _load()
        now = time.time()
        rec = next((r for r in rows if r.get("user_id") == user_id), None)
        if rec is None:
            rec = {"user_id": user_id, "created_at": now, "approved_once": False}
            rows.append(rec)
        rec.update({
            "username": username,
            "key_fp": copy_vault.fingerprint(api_key),
            "enc_key": copy_vault.encrypt(api_key),
            "enc_secret": copy_vault.encrypt(api_secret),
            "margin_usdt": _clamp_margin(margin),
            "enabled": False,                    # re-approval required on new keys
            "updated_at": now,
        })
        _save(rows)
        return _public(rec)


def set_margin(user_id: int, margin) -> dict | None:
    with _lock:
        rows = _load()
        rec = next((r for r in rows if r.get("user_id") == user_id), None)
        if rec is None:
            return None
        rec["margin_usdt"] = _clamp_margin(margin)
        rec["updated_at"] = time.time()
        _save(rows)
        return _public(rec)


def set_enabled(user_id: int, enabled: bool) -> dict | None:
    """Admin approve / disable. Enabling stamps approved_once so the UI can tell
    'never approved' apart from 'approved then paused'."""
    with _lock:
        rows = _load()
        rec = next((r for r in rows if r.get("user_id") == user_id), None)
        if rec is None:
            return None
        rec["enabled"] = bool(enabled)
        if enabled:
            rec["approved_once"] = True
        rec["updated_at"] = time.time()
        _save(rows)
        return _public(rec)


def remove(user_id: int) -> bool:
    with _lock:
        rows = _load()
        kept = [r for r in rows if r.get("user_id") != user_id]
        if len(kept) == len(rows):
            return False
        _save(kept)
        return True
