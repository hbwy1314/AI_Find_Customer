"""Authentication routes — signup / login / logout / me / signup-status.

These endpoints are NOT gated by `require_api_access` — they ARE the
auth surface. They set HttpOnly session cookies plus a non-HttpOnly
CSRF cookie (double-submit).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from auth import users as auth_users
from auth.security import UserCtx, optional_user
from config.settings import get_settings
from emailing.store import get_email_store

logger = logging.getLogger(__name__)

router = APIRouter()


class SignupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    role: str


class MeResponse(BaseModel):
    user: UserOut
    signup_open: bool
    via: str


def _client_meta(request: Request) -> tuple[str, str]:
    ip = (request.client.host if request.client else "") or ""
    ua = request.headers.get("user-agent", "") or ""
    return ip[:64], ua[:512]


def _set_auth_cookies(response: Response, session_id: str, csrf_token: str, max_age: int) -> None:
    settings = get_settings()
    samesite = settings.cookie_samesite or "lax"
    secure = bool(settings.cookie_secure)
    response.set_cookie(
        key=settings.session_cookie_name or "aih_session",
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name or "aih_csrf",
        value=csrf_token,
        max_age=max_age,
        httponly=False,  # JS-readable so SPA can echo back as X-CSRF-Token
        secure=secure,
        samesite=samesite,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    samesite = settings.cookie_samesite or "lax"
    secure = bool(settings.cookie_secure)
    for name in (settings.session_cookie_name or "aih_session",
                 settings.csrf_cookie_name or "aih_csrf"):
        response.delete_cookie(key=name, path="/", samesite=samesite, secure=secure)


@router.get("/signup-status")
def signup_status() -> dict:
    store = get_email_store()
    return {
        "open": auth_users.is_signup_open(store),
        "user_count": store.count_users(),
    }


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, response: Response) -> dict:
    store = get_email_store()
    if not auth_users.is_signup_open(store):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Signup is closed. Ask an administrator to add a new account.",
        )
    is_first_user = store.count_users() == 0
    role = "admin" if is_first_user else "user"
    try:
        user = auth_users.create_user(
            store, email=payload.email, password=payload.password, role=role
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if is_first_user:
        auth_users.close_signup(store)
        logger.info("First user registered as admin: %s", user["email"])

    ip, ua = _client_meta(request)
    session = auth_users.create_session(store, user_id=user["id"], ip=ip, user_agent=ua)
    _set_auth_cookies(response, session["session_id"], session["csrf_token"], session["ttl_seconds"])
    return {"user": UserOut(id=user["id"], email=user["email"], role=user["role"]).model_dump()}


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    store = get_email_store()
    user = auth_users.authenticate(store, payload.email, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    ip, ua = _client_meta(request)
    session = auth_users.create_session(store, user_id=user["id"], ip=ip, user_agent=ua)
    _set_auth_cookies(response, session["session_id"], session["csrf_token"], session["ttl_seconds"])
    return {"user": UserOut(**{k: user[k] for k in ("id", "email", "role")}).model_dump()}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    settings = get_settings()
    session_id = request.cookies.get(settings.session_cookie_name or "aih_session", "").strip()
    if session_id:
        auth_users.destroy_session(get_email_store(), session_id)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(ctx: UserCtx | None = Depends(optional_user)) -> dict:
    """Return the current user, or 401 if not authenticated.

    `ctx` is populated by `optional_user` when a valid session cookie
    is present. When unauthenticated, `ctx` is None.
    """
    if ctx is None or ctx.via != "session":
        # API-token and localhost dev bypass do not give a real user identity.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    store = get_email_store()
    return {
        "user": {"id": ctx.user_id, "email": ctx.email, "role": ctx.role},
        "signup_open": auth_users.is_signup_open(store),
        "via": ctx.via,
    }


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
    """Change the current session user's password.

    Requires a valid session cookie (NOT exempt from the auth chain). The
    caller must re-authenticate with the new password on subsequent logins
    — existing sessions stay valid (intentional: avoids kicking the user
    out mid-task).
    """
    settings = get_settings()
    cookie_name = settings.session_cookie_name or "aih_session"
    session_id = request.cookies.get(cookie_name, "").strip()
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    store = get_email_store()
    row = auth_users.load_session(store, session_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired, please log in again",
        )
    try:
        auth_users.change_password(
            store,
            user_id=int(row["user_id"]),
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    logger.info("User id=%s changed their password", row["user_id"])
    return {"ok": True}
