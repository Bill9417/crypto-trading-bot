"""
🔐 Encryption at rest for follower Bybit API secrets (copy-trading).

Follower keys are NEVER stored in plaintext. Each secret is Fernet-encrypted
(AES-128-CBC + HMAC) with a key derived from ONE source shared by the web and
scanner processes, in this order:

  1. COPY_ENC_KEY env   — a urlsafe-base64 32-byte Fernet key (explicit override)
  2. the Flask secret   — env FLASK_SECRET_KEY, else the on-disk .secret_key file
                          that app.py already persists (0600). HKDF-SHA256 turns
                          it into a Fernet key.

Deriving from the Flask secret means zero new setup: both processes read the
same .secret_key file and get the same encryption key. It also means rotating
the Flask secret intentionally invalidates every stored follower key — they
simply re-submit. Plaintext secrets never touch disk, logs, Telegram or LINE.
"""
import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

_SECRET_FILE = os.path.join(os.path.dirname(__file__), ".secret_key")
_INFO = b"wolf-copy-trading-key-v1"          # HKDF context — bump to force re-key
_fernet = None


def _flask_secret() -> bytes:
    """Same resolution order app.py uses, minus generating a new one (read-only):
    FLASK_SECRET_KEY env → .secret_key file. Returns b'' if neither exists."""
    env = os.getenv("FLASK_SECRET_KEY")
    if env and env != "change-me-before-public-release":
        return env.encode()
    try:
        with open(_SECRET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().encode()
    except OSError:
        return b""


def _hkdf(secret: bytes) -> bytes:
    """HKDF-SHA256(secret) → 32 bytes → urlsafe-base64 (a valid Fernet key).
    Salt is empty (the secret already has full entropy); INFO domain-separates
    this from any other use of the same secret."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    okm = hmac.new(prk, _INFO + b"\x01", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(okm)


def _build() -> Fernet:
    explicit = os.getenv("COPY_ENC_KEY", "").strip()
    if explicit:
        return Fernet(explicit.encode())      # must already be a valid Fernet key
    secret = _flask_secret()
    if not secret:
        raise RuntimeError(
            "copy_vault: no COPY_ENC_KEY and no Flask secret to derive from — "
            "cannot encrypt follower keys safely")
    return Fernet(_hkdf(secret))


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _build()
    return _fernet


def available() -> bool:
    """True when a key source exists (so the UI can refuse enrollment cleanly
    rather than 500 if the server is misconfigured)."""
    try:
        _cipher()
        return True
    except Exception:  # noqa: BLE001
        return False


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Raises InvalidToken if the ciphertext was written under a different key
    (e.g. the Flask secret was rotated) — callers treat that as 'key invalid,
    ask the follower to re-submit', never as a crash."""
    return _cipher().decrypt(token.encode()).decode()


def try_decrypt(token: str):
    """(plaintext, None) on success, (None, reason) on failure — never raises."""
    try:
        return decrypt(token), None
    except InvalidToken:
        return None, "cannot decrypt (encryption key changed?)"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:120]


def fingerprint(api_key: str) -> str:
    """A safe-to-display tag for an API key: first 4 + last 4 chars. Never the
    secret. Used so the UI/admin can tell keys apart without decrypting."""
    k = (api_key or "").strip()
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"
