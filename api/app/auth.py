"""Authentication: opaque session tokens in ``shop.sessions``.

No JWT, no refresh flow, no signup. A login row is minted in ``shop.sessions``
and handed to the browser as the httpOnly cookie ``sf_session``. Every seeded
user has the password ``password123`` (hashed with bcrypt at seed time).

Routers depend on :func:`get_current_user`; it answers 401 when the cookie is
missing, unknown or expired.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request, Response
from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models import Session as SessionRow
from app.models import User
from app.schemas import ApiError, as_utc
from app.telemetry import set_user

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

#: The one cookie this project uses. Contract-fixed — the web tracker and the
#: ghost-run scripts both hardcode it.
SESSION_COOKIE = "sf_session"

#: How long a login lasts. Long enough that a ghost run recorded "on Tuesday"
#: is still a valid session when the robot investigates it.
SESSION_TTL = timedelta(days=int(os.getenv("SESSION_TTL_DAYS", "30")))

#: Cookies are issued over plain HTTP on localhost, so `secure` stays off.
#: localhost:3000 -> localhost:8000 is same-site, so `lax` still crosses ports.
COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "lax")
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10)


# --------------------------------------------------------------------------- #
#  Passwords
# --------------------------------------------------------------------------- #


def hash_password(raw: str) -> str:
    """Hash a plaintext password. Used by the seed script."""
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """Constant-time-ish password check. Never raises on a malformed hash."""
    if not hashed:
        return False
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Sessions
# --------------------------------------------------------------------------- #


def new_token() -> str:
    """43 URL-safe characters — comfortably inside ``sessions.token`` (64)."""
    return secrets.token_urlsafe(32)


def create_session(db: DbSession, user: User) -> SessionRow:
    """Mint and persist a session row for ``user``."""
    now = datetime.now(timezone.utc)
    row = SessionRow(
        token=new_token(),
        user_id=user.id,
        created_at=now,
        expires_at=now + SESSION_TTL,
    )
    db.add(row)
    db.flush()
    return row


def destroy_session(db: DbSession, token: str) -> bool:
    """Delete a session row. Returns whether anything was removed."""
    result = db.execute(delete(SessionRow).where(SessionRow.token == token))
    return bool(result.rowcount)


def purge_expired_sessions(db: DbSession) -> int:
    """Housekeeping used by the debug reset."""
    now = datetime.now(timezone.utc)
    return int(db.execute(delete(SessionRow).where(SessionRow.expires_at < now)).rowcount or 0)


# --------------------------------------------------------------------------- #
#  Cookie plumbing
# --------------------------------------------------------------------------- #


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def token_from_request(request: Request) -> str | None:
    """Cookie first; ``Authorization: Bearer <token>`` as a scripting fallback.

    The bearer fallback exists for the ghost-run scripts and for ``curl``
    reproductions the robot writes — the browser always uses the cookie.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return token
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        candidate = header[7:].strip()
        if candidate:
            return candidate
    return None


# --------------------------------------------------------------------------- #
#  Dependencies
# --------------------------------------------------------------------------- #


def _lookup_user(db: DbSession, token: str | None) -> User | None:
    if not token:
        return None
    row = db.get(SessionRow, token)
    if row is None:
        return None
    expires_at = as_utc(row.expires_at)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        return None
    user = db.execute(select(User).where(User.id == row.user_id)).scalar_one_or_none()
    if user is not None:
        # Bind the user to the trace so every later event in this request —
        # sql, business, error and the closing `request` event — carries it.
        set_user(user.id)
    return user


def get_current_user(
    request: Request,
    db: DbSession = Depends(get_db),
) -> User:
    """Require a signed-in user. 401 ``{"error": "unauthorized"}`` otherwise."""
    user = _lookup_user(db, token_from_request(request))
    if user is None:
        raise ApiError(401, "unauthorized", "You need to be signed in to do that.")
    return user


def get_optional_user(
    request: Request,
    db: DbSession = Depends(get_db),
) -> User | None:
    """Same lookup, but anonymous is allowed (used for public catalog reads)."""
    return _lookup_user(db, token_from_request(request))


__all__ = [
    "SESSION_COOKIE",
    "SESSION_TTL",
    "clear_session_cookie",
    "create_session",
    "destroy_session",
    "get_current_user",
    "get_optional_user",
    "hash_password",
    "purge_expired_sessions",
    "set_session_cookie",
    "token_from_request",
    "verify_password",
]
