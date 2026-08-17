"""User and session management — bcrypt + server-side sessions in SQLite."""

from __future__ import annotations

import logging
import secrets as _pysecrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from config.settings import get_settings
from emailing.store import EmailStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_utc().isoformat()


def expires_iso(ttl_seconds: int) -> str:
    return (now_utc() + timedelta(seconds=ttl_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_BCRYPT_MAX_BYTES = 72  # bcrypt silently truncates past 72 bytes


def _truncate(plain: str) -> bytes:
    encoded = plain.encode("utf-8")
    return encoded[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    if not plain:
        raise ValueError("Password must not be empty")
    return bcrypt.hashpw(_truncate(plain), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def authenticate(store: EmailStore, email: str, password: str) -> dict[str, Any] | None:
    user = store.get_user_by_email(email)
    if not user:
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return {
        "id": int(user["id"]),
        "email": user["email"],
        "role": user.get("role", "user"),
        "created_at": user.get("created_at", ""),
    }


def create_user(store: EmailStore, *, email: str, password: str, role: str = "user") -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Invalid email")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    pw_hash = hash_password(password)
    created = now_iso()
    try:
        user_id = store.create_user(
            email=email, password_hash=pw_hash, role=role, created_at=created
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Email already registered") from exc
    return {"id": user_id, "email": email, "role": role, "created_at": created}


def get_user(store: EmailStore, user_id: int) -> dict[str, Any] | None:
    return store.get_user_by_id(user_id)


def change_password(
    store: EmailStore,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
) -> None:
    """Change a user's password after verifying the current one.

    Raises ValueError on bad current password, weak new password, or unknown
    user. The update is in-place; existing sessions are NOT revoked (callers
    who need that can `delete_session()` for the row).
    """
    if not new_password or len(new_password) < 8:
        raise ValueError("新密码至少需要 8 位")
    user = store.get_user_by_id(user_id)
    if not user:
        raise ValueError("User not found")
    if not verify_password(current_password, user.get("password_hash", "")):
        raise ValueError("当前密码不正确")
    new_hash = hash_password(new_password)
    with store._connect() as conn:  # noqa: SLF001 — internal helper, OK for write
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, int(user_id)),
        )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(
    store: EmailStore,
    *,
    user_id: int,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    ttl = int(settings.session_ttl_seconds or 0)
    session_id = _pysecrets.token_urlsafe(48)
    csrf_token = _pysecrets.token_urlsafe(32)
    now = now_iso()
    store.create_session(
        session_id=session_id,
        user_id=user_id,
        csrf_token=csrf_token,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_iso(ttl),
        last_seen_at=now,
        created_at=now,
    )
    return {
        "session_id": session_id,
        "csrf_token": csrf_token,
        "expires_at": expires_iso(ttl),
        "ttl_seconds": ttl,
    }


def load_session(store: EmailStore, session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    row = store.get_session(session_id)
    if not row:
        return None
    if not _is_session_alive(row):
        store.delete_session(session_id)
        return None
    return row


def touch_session(store: EmailStore, session_id: str) -> None:
    store.touch_session(session_id, now_iso())


def destroy_session(store: EmailStore, session_id: str) -> None:
    store.delete_session(session_id)


def _is_session_alive(row: dict[str, Any]) -> bool:
    raw = row.get("expires_at", "")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now_utc()


# ---------------------------------------------------------------------------
# Signup gate
# ---------------------------------------------------------------------------

def is_signup_open(store: EmailStore) -> bool:
    if store.count_users() == 0:
        return True
    return store.is_signup_open()


def close_signup(store: EmailStore) -> None:
    store.mark_signup_closed(last_admin_at=now_iso())


# ---------------------------------------------------------------------------
# Convenience: cookie construction kwargs
# ---------------------------------------------------------------------------

def cookie_kwargs(*, name: str, value: str, max_age: int) -> dict[str, Any]:
    settings = get_settings()
    return {
        "key": name,
        "value": value,
        "max_age": max_age,
        "httponly": name == settings.session_cookie_name,  # only session is HttpOnly
        "secure": bool(settings.cookie_secure),
        "samesite": settings.cookie_samesite or "lax",
        "path": "/",
    }


def clear_cookie_kwargs(name: str) -> dict[str, Any]:
    settings = get_settings()
    return {
        "key": name,
        "value": "",
        "max_age": 0,
        "expires": 0,
        "httponly": name == settings.session_cookie_name,
        "secure": bool(settings.cookie_secure),
        "samesite": settings.cookie_samesite or "lax",
        "path": "/",
    }
