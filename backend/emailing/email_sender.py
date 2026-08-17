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
from emailing.unsubscribe import append_footer

logger = logging.getLogger(__name__)


def _normalise_provider(provider: str) -> str:
    """Map legacy ``"smtp"`` provider values to ``"graph"``."""
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "smtp", "imap"}:
        return "graph"
    return normalized


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
) -> dict[str, Any]:
    """Send one email via Microsoft Graph."""
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
        return await send_via_graph(
            account,
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            reply_to=reply_to,
            thread_key=thread_key,
            list_unsubscribe_url=list_unsubscribe_url,
            list_unsubscribe_mailto=list_unsubscribe_mailto,
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
