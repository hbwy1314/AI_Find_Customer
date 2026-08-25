"""Backwards-compatible re-export shim for `require_api_access`.

The real implementation now lives in `auth.security` (UserCtx + CSRF
double-submit + session cookies). This module exists so the 4 call-sites
(`api/routes.py`, `api/email_routes.py`, `api/automation_routes.py`,
`api/sse.py`) keep working without edits.
"""

from __future__ import annotations

from auth.security import (  # noqa: F401
    UserCtx,
    optional_user,
    require_admin,
    require_api_access,
    require_user,
)

__all__ = [
    "UserCtx",
    "require_api_access",
    "optional_user",
    "require_user",
    "require_admin",
]
