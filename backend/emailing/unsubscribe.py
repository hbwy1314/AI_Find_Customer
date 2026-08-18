"""Email unsubscribe — HMAC-signed token generation, verification, recording.

Used by `api/unsubscribe_routes.py` to handle the one-click unsubscribe links
appended to every outbound email. Tokens encode (email, scope, issued_at)
and are bound to a server-side secret so they cannot be forged.

Scope controls granularity:
- ``"all"`` — opt out of every future email from this system
- ``"campaign:{id}"`` — opt out of a specific campaign only
- ``"sequence:{id}"`` — opt out of a specific follow-up sequence only
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import time
from typing import Any

# Token format: <base64url payload>.<base64url signature>
# Payload = <email>|<scope>|<issued_at_unix>|<ttl_seconds>
_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days
_SEPARATOR = "|"
_APP_SECRET_KEY = "unsubscribe_token_secret"


def _store():
    """Late import the email store (avoids a top-level settings/settings_store
    cycle in the import graph)."""
    from emailing.store import get_email_store

    return get_email_store()


def _secret() -> bytes:
    """Return the per-deployment signing secret, creating one on first use.

    The secret is persisted in the `app_settings` SQLite table so it
    survives restarts. Outstanding unsubscribe links keep working after
    a service restart because the same secret signs and verifies them.
    """
    raw = _store().get_app_setting(_APP_SECRET_KEY, "").strip()
    if not raw:
        raw = _secrets.token_urlsafe(32)
        _store().set_app_setting(_APP_SECRET_KEY, raw)
    return raw.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(payload: bytes) -> str:
    return _b64url(hmac.new(_secret(), payload, hashlib.sha256).digest())


def issue_token(email: str, scope: str = "all", *, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> str:
    """Generate an unsubscribe token bound to (email, scope)."""
    if not email:
        raise ValueError("email is required")
    issued_at = int(time.time())
    payload_str = f"{email}{_SEPARATOR}{scope}{_SEPARATOR}{issued_at}{_SEPARATOR}{ttl_seconds}"
    payload_b = _b64url(payload_str.encode("utf-8"))
    sig = _sign(payload_b.encode("ascii"))
    return f"{payload_b}.{sig}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify signature + expiry. Returns {email, scope, issued_at, ttl} on success, else None."""
    if not token or "." not in token:
        return None
    payload_b, _, sig = token.partition(".")
    expected = _sign(payload_b.encode("ascii"))
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        decoded = _b64url_decode(payload_b).decode("utf-8")
        parts = decoded.split(_SEPARATOR)
        if len(parts) != 4:
            return None
        email, scope, issued_at_s, ttl_s = parts
        issued_at = int(issued_at_s)
        ttl = int(ttl_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if int(time.time()) > issued_at + ttl:
        return None
    return {"email": email, "scope": scope, "issued_at": issued_at, "ttl": ttl}


def token_hash(token: str) -> str:
    """Hash a token for storage (so the raw token isn't kept in the DB)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_unsubscribe_url(base_url: str, token: str) -> str:
    """Construct the full HTTPS URL embedded in email bodies / headers."""
    base = (base_url or "").rstrip("/")
    return f"{base}/api/unsubscribe/{token}"


def build_mailto_unsubscribe(to_email: str) -> str:
    """Construct the mailto: address for List-Unsubscribe-Post fallback."""
    # Subject is pre-formatted so an MTA-side filter can grep it.
    return f"mailto:unsubscribe@{to_email.split('@', 1)[-1]}?subject=unsubscribe"


def append_footer(body_text: str, unsubscribe_url: str) -> str:
    """Append a tiny, plain-text unsubscribe footer to the body.

    The footer is short and unobtrusive — never a wall of legal text. It must
    appear at the very end so the URL is the last thing a recipient sees.

    If the body already ends with a footer (recognised by the
    ``--`` separator + ``"不再接收此类邮件："`` marker), the existing
    footer is replaced in-place with the new URL instead of being
    duplicated. This keeps preview-stored bodies consistent with the
    version that actually gets sent.
    """
    sep = "\n\n--\n"
    footer = f"不再接收此类邮件：{unsubscribe_url}\n"
    cleaned = (body_text or "").rstrip()
    if not cleaned:
        return footer
    # Detect an existing placeholder footer introduced by
    # `body_format._append_unsubscribe_placeholder` (or by an earlier
    # send) and replace its URL with the new one.
    if "\n\n--\n" in cleaned and "不再接收此类邮件：" in cleaned:
        head, _, tail = cleaned.rpartition("\n\n--\n")
        return head + sep + footer
    return cleaned + sep + footer

