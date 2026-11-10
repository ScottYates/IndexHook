"""Accounts, password hashing, sessions, webhook tokens, reverse proxy login."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from .config import Settings
from .db import DB, now

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COOKIE = "indexhook_session"
_N, _R, _P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=2**26, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        kind, n, r, p, salt, dk = (stored or "").split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                             p=int(p), maxmem=2**26, dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def create_user(db: DB, username: str, password: str | None, email: str = "", admin: bool | None = None) -> int:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError("User names use letters, digits, dot, dash and underscore.")
    if password is not None and len(password) < 8:
        raise ValueError("The password needs at least 8 characters.")
    if db.one("SELECT 1 FROM users WHERE username=?", username):
        raise ValueError("That user name is taken.")
    if admin is None:
        admin = db.one("SELECT COUNT(*) c FROM users")["c"] == 0
    uid = db.run(
        "INSERT INTO users(username,email,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
        username, email.strip(), hash_password(password) if password else None, int(admin), now(),
    )
    from .rules import seed_rules
    seed_rules(db, uid)
    return uid


def new_hook_token(db: DB, user_id: int) -> str:
    """Create a webhook token. Only its hash is stored, so it is shown once."""
    token = "ih_" + secrets.token_urlsafe(32)
    db.run("UPDATE users SET hook_token_hash=?, hook_token_hint=? WHERE id=?",
           _sha(token), token[:7] + "…" + token[-4:], user_id)
    return token


def user_for_hook_token(db: DB, token: str):
    if not token:
        return None
    return db.one("SELECT * FROM users WHERE hook_token_hash=? AND disabled=0", _sha(token))


# sessions -------------------------------------------------------------
def start_session(db: DB, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    days = Settings(db).int("session_days") or 30
    db.run("INSERT INTO sessions(id,user_id,csrf,expires_at) VALUES(?,?,?,?)",
           _sha(token), user_id, secrets.token_urlsafe(24), now() + days * 86400)
    db.run("DELETE FROM sessions WHERE expires_at<?", now())
    return token


def session_user(db: DB, token: str | None):
    """Return (user row, csrf token) or (None, None)."""
    if not token:
        return None, None
    row = db.one(
        "SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.id=? AND s.expires_at>? AND u.disabled=0", _sha(token), now())
    return (row, row["csrf"]) if row else (None, None)


def end_session(db: DB, token: str | None):
    if token:
        db.run("DELETE FROM sessions WHERE id=?", _sha(token))


# reverse proxy login ----------------------------------------------------
def proxy_is_trusted(db: DB, peer: str | None) -> bool:
    allowed = {a.strip() for a in Settings(db).get("trusted_proxies").split(",") if a.strip()}
    return bool(peer) and peer in allowed


def header_user(db: DB, headers, peer: str | None):
    """Log in from a header set by a trusted reverse proxy (YunoHost SSOwat, Authelia, ...)."""
    s = Settings(db)
    name = s.get("trusted_header")
    if not name or not proxy_is_trusted(db, peer):
        return None
    username = (headers.get(name) or "").strip()
    if not username or not USERNAME_RE.match(username):
        return None
    user = db.one("SELECT * FROM users WHERE username=?", username)
    if user is None and s.bool("header_auto_create"):
        email = (headers.get(s.get("trusted_header_email")) or "") if s.get("trusted_header_email") else ""
        create_user(db, username, None, email)
        user = db.one("SELECT * FROM users WHERE username=?", username)
    if user is None or user["disabled"]:
        return None
    return user


def header_csrf(db: DB, user_id: int) -> str:
    """Stable CSRF token for header logins, which have no session row."""
    secret = db.get_setting("internal", "csrf_secret")
    if not secret:
        secret = secrets.token_urlsafe(32)
        db.set_setting("internal", "csrf_secret", secret)
    return hmac.new(secret.encode(), f"csrf:{user_id}".encode(), "sha256").hexdigest()


# login throttle -----------------------------------------------------------
class Throttle:
    """Slow down password guessing: after 5 failures, lock the key for a growing time."""

    def __init__(self):
        self.fails: dict[str, tuple[int, float]] = {}

    def wait(self, key: str) -> int:
        n, until = self.fails.get(key, (0, 0))
        return max(0, int(until - time.time()))

    def fail(self, key: str):
        n, _ = self.fails.get(key, (0, 0))
        n += 1
        lock = min(900, 30 * 2 ** (n - 5)) if n >= 5 else 0
        self.fails[key] = (n, time.time() + lock)
        if len(self.fails) > 10000:
            self.fails.clear()

    def ok(self, key: str):
        self.fails.pop(key, None)
