"""Login, logout and "who am I".

Opaque token in ``shop.sessions``, delivered as the httpOnly cookie
``sf_session``. Every seeded user's password is ``password123``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.auth import (
    clear_session_cookie,
    create_session,
    destroy_session,
    get_current_user,
    set_session_cookie,
    token_from_request,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.schemas import ApiError, LoginRequest, UserEnvelope, UserOut
from app.telemetry import emit, set_user

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=UserEnvelope)
def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession = Depends(get_db),
) -> dict:
    email = payload.email.strip().lower()
    user = db.execute(
        select(User).where(func.lower(User.email) == email)
    ).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash):
        # Same answer either way — no user enumeration.
        emit("business", "login_failed", level="warn", email=email)
        raise ApiError(401, "invalid_credentials", "That email and password don't match.")

    session = create_session(db, user)
    set_session_cookie(response, session.token)
    set_user(user.id)

    emit("business", "login_succeeded", user_id=user.id, email=user.email)
    return {"user": UserOut.model_validate(user).model_dump()}


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
) -> dict:
    token = token_from_request(request)
    removed = destroy_session(db, token) if token else False
    clear_session_cookie(response)
    return {"ok": True, "signed_out": bool(removed)}


@router.get("/me", response_model=UserEnvelope)
def me(user: User = Depends(get_current_user)) -> dict:
    return {"user": UserOut.model_validate(user).model_dump()}


__all__ = ["router"]
