#!/usr/bin/env python
"""
set_password.py — change a web-login password, and audit for weak ones.

WHY THIS EXISTS: the dashboard used to sit behind an obscure Cloudflare
quick-tunnel URL that rotated on every restart, so a guessable password was
survivable through sheer obscurity. It now lives on a PERMANENT public HTTPS
address (Tailscale Funnel). An audit on 2026-08-02 found an admin account
whose username and password were both "1" — anyone who found the URL and
tried the most obvious credential in existence would have got full admin:
real Binance and Bybit balances, open positions, the close-position button,
the live-strategy switch, and other members' encrypted API keys.

    python set_password.py --audit              # list weak accounts
    python set_password.py <username>           # change a password (prompted)
    python set_password.py <username> --demote   # drop admin rights

Nothing here touches trading. It only writes the users table.
"""
import argparse
import getpass
import sys

# Passwords that make a public login page a formality. Not exhaustive — it is
# the "would a stranger guess this in ten tries" list, which is the bar that
# actually matters for an internet-facing box.
TRIVIAL = {
    "1", "2", "3", "12", "123", "1234", "12345", "123456", "1234567", "12345678",
    "0000", "1111", "2222", "password", "password1", "password123", "passwd",
    "admin", "admin123", "root", "test", "guest", "user", "letmein", "qwerty",
    "abc123", "111111", "000000", "1q2w3e", "iloveyou", "welcome", "monkey",
    "dragon", "wolf", "scanner", "wolfscanner", "wolfman", "trading", "crypto",
}
MIN_LEN = 12


# Answers already paid for, keyed by (username, hash) — see weak_reason.
_WEAK_CACHE: dict = {}
_WEAK_CACHE_MAX = 512


def weak_reason(username: str, password_hash: str) -> str | None:
    """Why this account is weak, or None. Checks the hash against the trivial
    list plus username-derived guesses — never needs the plaintext.

    Memoised on (username, hash), because a password hash is deliberately
    expensive to check and this runs ~44 candidates against EVERY user. On
    /health that was 101 scrypt verifications and 7.3 seconds of CPU per page
    load — on a box whose real job is running a live trading engine, and on a
    page built to be refreshed. Membership grows with copy-trading, so the cost
    was linear in users and rising.

    Caching cannot weaken the audit: the hash IS the cache key, so changing a
    password changes the key and the answer is recomputed from scratch. There is
    no staleness window to reason about — a hash that has not changed cannot
    have a different answer.
    """
    from werkzeug.security import check_password_hash
    key = (username or "", password_hash or "")
    if key in _WEAK_CACHE:
        return _WEAK_CACHE[key]

    u = (username or "").lower()
    candidates = TRIVIAL | {u, u + "1", u + "123", u + u, u + "2026", u + "!"}
    result = None
    for c in candidates:
        try:
            if check_password_hash(password_hash, c):
                result = f"password is {c!r} — guessable in seconds"
                break
        except Exception:  # noqa: BLE001 — a malformed hash is its own problem
            result = "password hash is unreadable"
            break

    if len(_WEAK_CACHE) >= _WEAK_CACHE_MAX:      # bounded; old hashes are dead keys
        _WEAK_CACHE.clear()
    _WEAK_CACHE[key] = result
    return result


def audit(app, User) -> int:
    with app.app_context():
        users = User.query.all()
        bad = []
        for u in users:
            r = weak_reason(u.username, u.password)
            tag = "ADMIN" if u.is_admin else "member"
            if r:
                bad.append(u)
                print(f"  ✗ {u.username!r:<14} [{tag}]  {r}")
            else:
                print(f"  ✓ {u.username!r:<14} [{tag}]  not in the guessable set")
        print()
        if bad:
            print(f"{len(bad)} of {len(users)} accounts are trivially guessable.")
            print("This site is reachable from the public internet — fix now:")
            for u in bad:
                print(f"    python set_password.py {u.username}")
            return 1
        print(f"All {len(users)} accounts cleared the guessable-password check.")
        return 0


def change(app, db, User, username: str, demote: bool) -> int:
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"No such user: {username!r}")
            return 1
        pw = getpass.getpass(f"New password for {username!r}: ")
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match.")
            return 1
        if len(pw) < MIN_LEN:
            print(f"Too short — use at least {MIN_LEN} characters.")
            return 1
        if pw.lower() in TRIVIAL or pw.lower() == username.lower():
            print("That is in the guessable list. Pick something else.")
            return 1
        user.password = generate_password_hash(pw, method="pbkdf2:sha256")
        if demote:
            user.is_admin = False
        db.session.commit()
        print(f"Updated {username!r}" + (" and removed admin rights." if demote else "."))
        print("Existing browser sessions stay signed in — log out elsewhere if unsure.")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Change or audit dashboard passwords.")
    p.add_argument("username", nargs="?", help="account to change")
    p.add_argument("--audit", action="store_true", help="report weak accounts and exit")
    p.add_argument("--demote", action="store_true", help="also remove admin rights")
    a = p.parse_args(argv)

    import app as A
    if a.audit or not a.username:
        return audit(A.app, A.User)
    return change(A.app, A.db, A.User, a.username, a.demote)


if __name__ == "__main__":
    sys.exit(main())
