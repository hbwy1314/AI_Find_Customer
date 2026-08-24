"""Unified email sending entrypoint.

Only Microsoft Graph is supported now. The legacy SMTP path has been
removed; per-account ``provider_type`` is coerced to ``"graph"`` at
read time so old rows that still say ``"smtp"`` keep working without
a database migration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from emailing.body_format import format_plaintext_email_body

logger = logging.getLogger(__name__)


def _normalise_provider(provider: str) -> str:
    """Map legacy ``"smtp"`` provider values to ``"graph"``."""
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "smtp", "imap"}:
        return "graph"
    return normalized


# Provider-message-id values that should NEVER show up on a real send.
# We treat them as stub/placeholder signals and refuse to mark a message
# as sent when they slip through. Background: an earlier version of the
# scheduler hard-coded `provider_message_id="x" / thread_key="y"` and
# wrote `status='sent'` rows that never went through Graph. Those rows
# polluted the daily-quota counters and reply detection. This guard is
# the last line of defence if a stub ever returns to the codebase.
_STUB_PROVIDER_IDS: frozenset[str] = frozenset({"x", "y", "test", "stub", "fake", "mock", "placeholder"})


async def send_email(
    account: dict[str, Any],
    *,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: str | None = None,
    thread_key: str | None = None,
    list_unsubscribe_url: str | None = None,
    list_unsubscribe_mailto: str | None = None,
    body_html: str | None = None,
) -> dict[str, Any]:
    """Send one email via Microsoft Graph.

    ``body_html`` is the optional pre-rendered HTML body. When
    provided, it is sent as the email's HTML content (with the
    recipient's real unsubscribe token already substituted in).
    When omitted, Graph API receives a fresh render from the plain
    text body via ``emailing.html_format.plaintext_to_html``.
    """
    if not to_email.strip():
        return {
            "ok": False,
            "provider": _normalise_provider(account.get("provider_type")),
            "provider_message_id": "",
            "thread_key": thread_key or subject,
            "sent_at": "",
            "error": "missing_recipient",
            "error_type": "invalid_recipient",
        }

    provider = _normalise_provider(account.get("provider_type"))
    if provider != "graph":
        # The only supported provider going forward is Graph.
        return {
            "ok": False,
            "provider": provider,
            "provider_message_id": "",
            "thread_key": thread_key or subject,
            "sent_at": "",
            "error": f"unsupported_provider:{provider}",
            "error_type": "permanent_failure",
        }

    try:
        from emailing.graph_client import send_via_graph
        result = await send_via_graph(
            account,
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            reply_to=reply_to,
            thread_key=thread_key,
            list_unsubscribe_url=list_unsubscribe_url,
            list_unsubscribe_mailto=list_unsubscribe_mailto,
            body_html=body_html,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — graph_client raises a variety of types
        logger.exception("Graph send failed: %s", exc)
        return {
            "ok": False,
            "provider": provider,
            "provider_message_id": "",
            "thread_key": thread_key or subject,
            "sent_at": "",
            "error": str(exc),
            "error_type": "transport_error",
        }

    # ── Stub-guard: refuse to mark a send successful if the provider
    # hand-back a placeholder id or thread_key. We only enforce this
    # when the caller claimed `ok=True`; failures already short-circuited
    # upstream. The single-step `sendMail` path legitimately returns an
    # empty `provider_message_id` (no internetMessageId back from
    # sendMail), so empty string is allowed — only known stub tokens
    # are rejected. `thread_key` is also guarded: reply-detector keys
    # on it, so a stub value would silently break conversation linkage.
    if result.get("ok"):
        pm_id = str(result.get("provider_message_id") or "").strip().lower()
        tk = str(result.get("thread_key") or "").strip().lower()
        bad_field = None
        if pm_id in _STUB_PROVIDER_IDS:
            bad_field = "provider_message_id"
        elif tk in _STUB_PROVIDER_IDS:
            bad_field = "thread_key"
        if bad_field:
            logger.warning(
                "Refusing to mark send as sent: %s=%r is a known stub token",
                bad_field,
                result.get(bad_field),
            )
            return {
                "ok": False,
                "provider": provider,
                "provider_message_id": "",
                "thread_key": thread_key or subject,
                "sent_at": "",
                "error": f"stub_{bad_field}:{result.get(bad_field)!r}",
                "error_type": "permanent_failure",
            }
    return result
