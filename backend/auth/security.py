"""Authentication + CSRF dependencies for FastAPI.

Replaces the old localhost + API-token gate with a chain of trust:

    1. Session cookie (`aih_session`) → look up server-side session row
       (and validate CSRF double-submit on mutating verbs when applicable).
    2. `API_ACCESS_TOKEN` via `X-API-Key` / `Authorization: Bearer` / `?api_key=`.
    3. If `API_ACCESS_TOKEN` is empty: only allow localhost callers (dev).

`require_api_access` is the existing dependency signature so it remains
a drop-in replacement at every call-site; it now also returns a
`UserCtx` (or `None` for anonymous / API-token callers).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from config.settings import get_settings
from emailing.store import EmailStore

_LOCAL_HOSTS = {"", "127.0.0.1", "::1", "localhost", "testclient", "test"}
_CSRF_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/signup",
    "/api/auth/logout",
    "/api/auth/signup-status",
}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class UserCtx(BaseModel):
    """Identity attached to a request, returned by `require_api_access`.

    Pydantic model (not dataclass) so FastAPI does not auto-include it in the
    request body schema when mixed with a Pydantic body model.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    role: str
    email: str
    csrf_token: Optional[str] = None
    session_id: Optional[str] = None
    via: str = "session"  # "session" | "api_token" | "localhost"


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _store(request: Request) -> EmailStore:
    store = getattr(request.app.state, "email_store", None)
    if store is not None:
        return store
    from emailing.store import get_email_store  # late import to avoid cycle
    singleton = get_email_store()
    request.app.state.email_store = singleton
    return singleton


def _is_local(request: Request) -> bool:
    host = (request.client.host if request.client else "").strip().lower()
    return host in _LOCAL_HOSTS


def require_api_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> UserCtx | None:
    """Validate the request. Returns a UserCtx for session users, else None.

    Raises HTTPException for non-local, unauthenticated requests when
    `API_ACCESS_TOKEN` is set.
    """
    return _resolve_ctx(request, authorization, x_api_key, api_key, raise_on_fail=True)


def optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> UserCtx | None:
    """Same as `require_api_access` but never raises — returns None for anonymous."""
    return _resolve_ctx(request, authorization, x_api_key, api_key, raise_on_fail=False)


def _resolve_ctx(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    api_key: str | None,
    *,
    raise_on_fail: bool,
) -> UserCtx | None:
    settings = get_settings()

    # 1. Try session cookie first.
    cookie_name = settings.session_cookie_name or "aih_session"
    session_id = request.cookies.get(cookie_name, "").strip()
    if session_id:
        from auth import users as auth_users  # late import to avoid cycle
        store = _store(request)
        try:
            row = auth_users.load_session(store, session_id)
        except Exception:  # noqa: BLE001
            row = None
        if row:
            # CSRF double-submit for mutating verbs (skip exempt auth paths)
            if request.method.upper() in _MUTATING_METHODS:
                path = request.url.path.rstrip("/")
                if path not in _CSRF_EXEMPT_PATHS:
                    csrf_cookie = request.cookies.get(
                        settings.csrf_cookie_name or "aih_csrf", ""
                    )
                    csrf_header = request.headers.get("x-csrf-token", "")
                    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
                        if raise_on_fail:
                            raise HTTPException(
                                status_code=status.HTTP_403_FORBIDDEN,
                                detail="CSRF token missing or invalid",
                            )
                        return None
            ctx = UserCtx(
                user_id=int(row["user_id"]),
                role=str(row.get("user_role", "user") or "user"),
                email=str(row.get("user_email", "")),
                csrf_token=str(row.get("csrf_token", "") or "") or None,
                session_id=str(row.get("id", "") or "") or None,
                via="session",
            )
            request.state.user_ctx = ctx
            return ctx

    # 2. Fall through to API token / localhost dev bypass.
    expected = settings.api_access_token.strip()
    if not expected:
        if _is_local(request):
            ctx = UserCtx(user_id=0, role="dev", email="", via="localhost")
            request.state.user_ctx = ctx
            return ctx
        if raise_on_fail:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API access is restricted to localhost unless API_ACCESS_TOKEN is configured.",
            )
        return None

    provided = x_api_key or api_key or _extract_bearer_token(authorization)
    if provided == expected:
        ctx = UserCtx(user_id=0, role="admin", email="", via="api_token")
        request.state.user_ctx = ctx
        return ctx

    if raise_on_fail:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API access token.",
        )
    return None


def _current_user_from(request: Request) -> UserCtx:
    """Return the UserCtx stashed on `request.state` by `require_api_access`.

    Use this in route handlers instead of declaring a `UserCtx` typed parameter,
    which would cause FastAPI to wrap the body in a `Body_*` envelope schema.
    """
    ctx = getattr(request.state, "user_ctx", None)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return ctx


def require_user(request: Request) -> UserCtx:
    return _current_user_from(request)


def require_admin(request: Request) -> UserCtx:
    user = _current_user_from(request)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required",
        )
    return user
