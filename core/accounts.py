"""Accounts: users, sessions, a 4-digit app PIN, and password reset.

Kept in its own SQLite file (``data/accounts.db``); each user's own data (portfolio, paper account, journal,
alerts, scans) lives in a separate file under ``data/users/``, so accounts can't see each other's data and
deleting an account deletes everything it had.

Security choices:

* Passwords and PINs are hashed with scrypt and a random salt (stdlib, no extra dependency).
* A session is a random token in an httpOnly cookie; only its SHA-256 is stored, so a stolen database can't be
  replayed. Sessions last 30 days.
* The PIN is an app lock, like Groww's: a session with a PIN is *locked* when the app is opened and after
  15 minutes without activity, and a locked session can't reach any personal data until the PIN is entered.
  Five wrong PINs end the session; the password is needed to log in again.
* Logins slow down after repeated failures for the same email (5 tries, then a 15-minute pause).
* Data collected is the minimum: email, first name, an 18+ confirmation and the consents given, plus optional
  onboarding answers (experience, interests, style, language) that personalise Kai.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SESSION_DAYS = 30
UNLOCK_MINUTES = 15            # PIN-locked again after this long without activity
REOPEN_SECONDS = 60            # opening the app again after this long asks for the PIN
MAX_PIN_TRIES = 5
MAX_LOGIN_TRIES = 5
LOGIN_PAUSE_MINUTES = 15
RESET_CODE_MINUTES = 15
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
PIN_RE = re.compile(r"^\d{4}$")
WEAK_PINS = {"0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1234", "4321", "0123", "1212", "6969"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    pin_hash        TEXT,
    age_confirmed   INTEGER NOT NULL DEFAULT 0,
    terms_version   TEXT NOT NULL,
    whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
    profile         TEXT NOT NULL DEFAULT '{}',
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    login_paused_until TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    unlocked_until  TEXT,
    pin_tries       INTEGER NOT NULL DEFAULT 0,
    user_agent      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS reset_codes (
    user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0
);
"""
TERMS_VERSION = "2026-10"


class AccountError(ValueError):
    """A user-facing problem with what was asked (bad input, wrong password, ...)."""

    def __init__(self, message: str, code: str = "INVALID", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def _t(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


def hash_secret(secret: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_secret(secret: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hash_secret(secret, bytes.fromhex(salt)).split("$")[2], digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password(password: str) -> None:
    if len(password) < 8:
        raise AccountError("Use at least 8 characters for the password.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise AccountError("Use letters and at least one number in the password.")
    if len(password) > 128:
        raise AccountError("That password is too long (128 characters at most).")


def check_pin(pin: str) -> None:
    if not PIN_RE.match(pin or ""):
        raise AccountError("The PIN must be exactly 4 digits.")
    if pin in WEAK_PINS:
        raise AccountError("That PIN is too easy to guess. Avoid repeated or sequential digits.")


class Accounts:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    # --- users ---------------------------------------------------------------------------------
    def count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users")["n"]

    def user(self, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id = ?", user_id)

    def by_email(self, email: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE email = ?", email.strip().lower())

    def user_ids(self) -> list[str]:
        with self._lock:
            return [r["id"] for r in self._conn.execute("SELECT id FROM users")]

    def signup(self, email: str, password: str, name: str, age_confirmed: bool, terms_accepted: bool, whatsapp_opt_in: bool = False) -> dict[str, Any]:
        email = email.strip().lower()
        name = re.sub(r"\s+", " ", name or "").strip()
        if not EMAIL_RE.match(email):
            raise AccountError("Enter a valid email address.")
        if not name or len(name) > 60:
            raise AccountError("Tell us your name (60 characters at most).")
        if not age_confirmed:
            raise AccountError("Kairo is for people aged 18 or over.")
        if not terms_accepted:
            raise AccountError("Please accept the Terms and Privacy Policy to continue.")
        check_password(password)
        if self.by_email(email):
            raise AccountError("An account with this email already exists. Log in instead.", "EMAIL_TAKEN", 409)
        uid, now = secrets.token_hex(8), _iso(_now())
        with self._tx() as c:
            c.execute("INSERT INTO users (id, email, name, password_hash, age_confirmed, terms_version, whatsapp_opt_in, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (uid, email, name, hash_secret(password), 1, TERMS_VERSION, int(whatsapp_opt_in), now, now))
        return self.user(uid)

    def authenticate(self, email: str, password: str) -> dict[str, Any]:
        u = self.by_email(email)
        now = _now()
        if u and (paused := _t(u["login_paused_until"])) and paused > now:
            mins = max(1, round((paused - now).total_seconds() / 60))
            raise AccountError(f"Too many wrong passwords. Try again in {mins} minute{'s' if mins > 1 else ''}, or reset your password.", "PAUSED", 429)
        if not u or not check_secret(password, u["password_hash"]):
            if u:
                tries = u["failed_logins"] + 1
                paused_until = _iso(now + dt.timedelta(minutes=LOGIN_PAUSE_MINUTES)) if tries >= MAX_LOGIN_TRIES else None
                with self._tx() as c:
                    c.execute("UPDATE users SET failed_logins=?, login_paused_until=? WHERE id=?",
                              (0 if paused_until else tries, paused_until, u["id"]))
            raise AccountError("Wrong email or password.", "BAD_LOGIN", 401)
        with self._tx() as c:
            c.execute("UPDATE users SET failed_logins=0, login_paused_until=NULL WHERE id=?", (u["id"],))
        return u

    def update_profile(self, user_id: str, **fields: Any) -> dict[str, Any]:
        u = self.user(user_id)
        profile = {**json.loads(u["profile"]), **{k: v for k, v in fields.items() if v is not None}}
        with self._tx() as c:
            c.execute("UPDATE users SET profile=?, updated_at=? WHERE id=?", (json.dumps(profile), _iso(_now()), user_id))
        return profile

    def set_name(self, user_id: str, name: str) -> None:
        name = re.sub(r"\s+", " ", name or "").strip()
        if not name or len(name) > 60:
            raise AccountError("Tell us your name (60 characters at most).")
        with self._tx() as c:
            c.execute("UPDATE users SET name=?, updated_at=? WHERE id=?", (name, _iso(_now()), user_id))

    def set_whatsapp(self, user_id: str, opt_in: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE users SET whatsapp_opt_in=?, updated_at=? WHERE id=?", (int(opt_in), _iso(_now()), user_id))

    def change_password(self, user_id: str, current: str, new: str) -> None:
        u = self.user(user_id)
        if not check_secret(current, u["password_hash"]):
            raise AccountError("Your current password is wrong.", "BAD_PASSWORD", 401)
        check_password(new)
        with self._tx() as c:
            c.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (hash_secret(new), _iso(_now()), user_id))

    def verify_password(self, user_id: str, password: str) -> None:
        if not check_secret(password or "", self.user(user_id)["password_hash"]):
            raise AccountError("That password is wrong.", "BAD_PASSWORD", 401)

    def delete(self, user_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (user_id,))

    # --- PIN -------------------------------------------------------------------------------------
    def set_pin(self, user_id: str, pin: str) -> None:
        check_pin(pin)
        with self._tx() as c:
            c.execute("UPDATE users SET pin_hash=?, updated_at=? WHERE id=?", (hash_secret(pin), _iso(_now()), user_id))

    def remove_pin(self, user_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE users SET pin_hash=NULL, updated_at=? WHERE id=?", (_iso(_now()), user_id))
            c.execute("UPDATE sessions SET unlocked_until=NULL, pin_tries=0 WHERE user_id=?", (user_id,))

    # --- sessions --------------------------------------------------------------------------------
    def create_session(self, user_id: str, user_agent: str = "") -> str:
        """A new session, unlocked (the password was just typed). Returns the token for the cookie."""
        token, now = secrets.token_urlsafe(32), _now()
        with self._tx() as c:
            c.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, unlocked_until, user_agent) VALUES (?,?,?,?,?,?,?)",
                      (_token_hash(token), user_id, _iso(now), _iso(now + dt.timedelta(days=SESSION_DAYS)), _iso(now),
                       _iso(now + dt.timedelta(minutes=UNLOCK_MINUTES)), user_agent[:200]))
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
        return token

    def session(self, token: str | None) -> dict[str, Any] | None:
        """The live session for a cookie token, with its user, or None."""
        if not token:
            return None
        s = self._one("SELECT s.*, u.pin_hash AS pin_hash FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", _token_hash(token))
        if not s or _t(s["expires_at"]) <= _now():
            return None
        return s

    @staticmethod
    def is_locked(s: dict[str, Any], now: dt.datetime | None = None) -> bool:
        if not s.get("pin_hash"):
            return False
        until = _t(s["unlocked_until"])
        return until is None or until <= (now or _now())

    def touch(self, token: str, s: dict[str, Any]) -> None:
        """Activity: slide the unlock window (only while unlocked) and remember when the user was last seen."""
        now = _now()
        if now - _t(s["last_seen"]) < dt.timedelta(seconds=20):
            return  # spare the disk: one write every 20 s is plenty
        unlocked = None if self.is_locked(s, now) else _iso(now + dt.timedelta(minutes=UNLOCK_MINUTES))
        with self._tx() as c:
            c.execute("UPDATE sessions SET last_seen=?, unlocked_until=COALESCE(?, unlocked_until) WHERE token_hash=?",
                      (_iso(now), unlocked, _token_hash(token)))

    def app_opened(self, token: str, s: dict[str, Any]) -> None:
        """The app was (re)opened: ask for the PIN again unless it was in use moments ago."""
        if s.get("pin_hash") and _now() - _t(s["last_seen"]) > dt.timedelta(seconds=REOPEN_SECONDS):
            self.lock(token)

    def lock(self, token: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET unlocked_until=NULL WHERE token_hash=?", (_token_hash(token),))

    def unlock(self, token: str, s: dict[str, Any], pin: str) -> None:
        """Check the PIN; unlock the session, or count the miss (and end the session after five)."""
        u = self.user(s["user_id"])
        now = _now()
        if not u["pin_hash"]:
            return
        if check_secret(pin or "", u["pin_hash"]):
            with self._tx() as c:
                c.execute("UPDATE sessions SET unlocked_until=?, pin_tries=0, last_seen=? WHERE token_hash=?",
                          (_iso(now + dt.timedelta(minutes=UNLOCK_MINUTES)), _iso(now), _token_hash(token)))
            return
        tries = s["pin_tries"] + 1
        if tries >= MAX_PIN_TRIES:
            self.end_session(token)
            raise AccountError("Too many wrong PINs. For your safety you've been logged out: log in with your password.", "PIN_LOCKOUT", 401)
        with self._tx() as c:
            c.execute("UPDATE sessions SET pin_tries=? WHERE token_hash=?", (tries, _token_hash(token)))
        left = MAX_PIN_TRIES - tries
        raise AccountError(f"Wrong PIN. {left} tr{'ies' if left > 1 else 'y'} left.", "BAD_PIN", 401)

    def end_session(self, token: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    def end_other_sessions(self, user_id: str, keep_token: str | None) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user_id, _token_hash(keep_token or "")))

    # --- password reset ----------------------------------------------------------------------------
    def start_reset(self, email: str) -> tuple[dict[str, Any], str] | None:
        """A 6-digit code for the account with this email (None when there is no such account)."""
        u = self.by_email(email)
        if not u:
            return None
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self._tx() as c:
            c.execute("INSERT INTO reset_codes (user_id, code_hash, expires_at, tries) VALUES (?,?,?,0) ON CONFLICT(user_id) DO UPDATE SET"
                      " code_hash=excluded.code_hash, expires_at=excluded.expires_at, tries=0",
                      (u["id"], hash_secret(code), _iso(_now() + dt.timedelta(minutes=RESET_CODE_MINUTES))))
        return u, code

    def finish_reset(self, email: str, code: str, new_password: str) -> dict[str, Any]:
        u = self.by_email(email)
        r = self._one("SELECT * FROM reset_codes WHERE user_id = ?", u["id"]) if u else None
        if not r or _t(r["expires_at"]) <= _now() or r["tries"] >= 5:
            raise AccountError("That code has expired. Ask for a new one.", "BAD_CODE", 400)
        if not check_secret((code or "").strip(), r["code_hash"]):
            with self._tx() as c:
                c.execute("UPDATE reset_codes SET tries = tries + 1 WHERE user_id=?", (u["id"],))
            raise AccountError("That code is wrong.", "BAD_CODE", 400)
        check_password(new_password)
        with self._tx() as c:
            c.execute("UPDATE users SET password_hash=?, failed_logins=0, login_paused_until=NULL, updated_at=? WHERE id=?",
                      (hash_secret(new_password), _iso(_now()), u["id"]))
            c.execute("DELETE FROM reset_codes WHERE user_id=?", (u["id"],))
            c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # a reset logs out every device
        return u


def public_user(u: dict[str, Any]) -> dict[str, Any]:
    """What the app may see about a user (never hashes)."""
    return {"id": u["id"], "email": u["email"], "name": u["name"], "pin_set": bool(u["pin_hash"]),
            "whatsapp_opt_in": bool(u["whatsapp_opt_in"]), "profile": json.loads(u["profile"]),
            "onboarded": "experience" in json.loads(u["profile"]) or json.loads(u["profile"]).get("skipped", False),
            "created_at": u["created_at"]}
