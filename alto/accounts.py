"""Contas locais do Alto. A senha fica só como hash. O cartão não passa por aqui."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alto.engine import ROOT

TRIAL_DAYS = 14
SESSION_DAYS = 30
PLAN = "mensal"

DATA_DIR = Path(os.environ.get("ALTO_DATA") or (ROOT / "alto-data"))
DB_PATH = DATA_DIR / "alto.db"

_lock = threading.Lock()
_connection: sqlite3.Connection | None = None


class AuthError(Exception):
    pass


def init() -> None:
    with _lock:
        _database()


def register(email: str, password: str) -> str:
    clean = _email(email)
    _password(password)
    salt = os.urandom(16)
    now = _now()
    until = _today_plus(TRIAL_DAYS)
    with _lock:
        db = _database()
        try:
            cursor = db.execute(
                "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                (clean, _digest(password, salt), salt.hex(), now.isoformat()),
            )
            user_id = int(cursor.lastrowid)
            db.execute(
                """
                INSERT INTO subscriptions (user_id, status, plan, period_end, provider, provider_ref)
                VALUES (?, 'trial', ?, ?, '', '')
                """,
                (user_id, PLAN, until),
            )
            token = _open_session(db, user_id, now)
            db.commit()
            return token
        except sqlite3.IntegrityError:
            db.rollback()
            raise AuthError("Já existe uma conta com esse e-mail.") from None


def login(email: str, password: str) -> str:
    clean = _email(email)
    _password(password)
    with _lock:
        db = _database()
        row = db.execute("SELECT * FROM users WHERE email = ?", (clean,)).fetchone()
        if row is None:
            _digest(password, b"\x00" * 16)
            raise AuthError("E-mail ou senha não conferem.")
        salt = bytes.fromhex(row["salt"])
        digest = _digest(password, salt)
        if not hmac.compare_digest(digest, row["password_hash"]):
            raise AuthError("E-mail ou senha não conferem.")
        token = _open_session(db, int(row["id"]), _now())
        db.commit()
        return token


def logout(token: str) -> None:
    if not token:
        return
    with _lock:
        db = _database()
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()


def session_user(token: str) -> dict | None:
    if not token:
        return None
    with _lock:
        db = _database()
        row = db.execute(
            """
            SELECT users.id, users.email, sessions.expires_at
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if row is None:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires < _now():
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
            return None
        subscription = _subscription(db, int(row["id"]))
        return {"id": int(row["id"]), "email": row["email"], "subscription": subscription}


def public_identity(record: dict | None) -> dict:
    if not record:
        return {"user": None, "subscription": None}
    return {
        "user": {"email": record["email"]},
        "subscription": dict(record["subscription"]),
    }


def _subscription(db: sqlite3.Connection, user_id: int) -> dict:
    row = db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {"status": "expired", "plan": PLAN, "until": _today_plus(0), "allows": False}
    status = row["status"]
    until = row["period_end"]
    allows = status in {"trial", "active"} and until >= _today()
    if not allows and status != "expired":
        db.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
        db.commit()
        status = "expired"
    return {"status": status, "plan": row["plan"], "until": until, "allows": allows}


def _open_session(db: sqlite3.Connection, user_id: int, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires.isoformat()),
    )
    return token


def _database() -> sqlite3.Connection:
    global _connection
    if _connection is not None:
        return _connection
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            plan TEXT NOT NULL,
            period_end TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            provider_ref TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    _connection = connection
    return connection


def _email(value: str) -> str:
    email = str(value or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if "@" not in email or email.startswith("@") or "." not in domain or len(email) > 200:
        raise AuthError("Esse e-mail não parece válido.")
    return email


def _password(value: str) -> None:
    if not isinstance(value, str) or len(value) < 8 or len(value) > 200:
        raise AuthError("A senha precisa ter pelo menos 8 caracteres.")


def _digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _today_plus(days: int) -> str:
    return (datetime.now().astimezone().date() + timedelta(days=days)).isoformat()
