"""Notification endpoints — recent inbound reply events for the in-app bell.

A reply to one of our outbound emails shows up as an `email_reply_events`
row. The bell on the header polls this endpoint so the user sees replies
without leaving the page.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from auth.security import require_api_access, require_user
from config.settings import get_settings
from emailing.store import get_email_store

router = APIRouter()


class NotificationItem(BaseModel):
    id: str
    sequence_id: str
    hunt_id: str
    campaign_id: str
    from_email: str
    subject: str
    snippet: str
    received_at: str


class NotificationsResponse(BaseModel):
    items: list[NotificationItem]
    unread: int
    last_seen_at: Optional[str] = None


def _query_recent_reply_events(
    *,
    since_iso: Optional[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Latest inbound replies joined to sequence/campaign/account info."""
    store = get_email_store()
    with store._connect() as conn:
        if since_iso:
            rows = conn.execute(
                """
                SELECT r.id, r.sequence_id, r.from_email, r.subject, r.snippet, r.received_at, r.created_at,
                       s.hunt_id, s.campaign_id, s.lead_name
                FROM email_reply_events r
                JOIN lead_email_sequences s ON s.id = r.sequence_id
                WHERE r.received_at >= ?
                ORDER BY r.received_at DESC
                LIMIT ?
                """,
                (since_iso, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT r.id, r.sequence_id, r.from_email, r.subject, r.snippet, r.received_at, r.created_at,
                       s.hunt_id, s.campaign_id, s.lead_name
                FROM email_reply_events r
                JOIN lead_email_sequences s ON s.id = r.sequence_id
                ORDER BY r.received_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/v1/notifications/recent")
def recent_notifications(
    request: Request,
    since: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Return the most recent inbound reply events for the in-app bell.

    - `since` (optional): ISO timestamp; only events after this are returned
      (used by the bell to compute the unread delta).
    - `limit` is clamped to 1..100.

    Requires login. Same dev-mode localhost bypass as the other protected
    endpoints.
    """
    require_api_access(request)
    require_user(request)
    safe_limit = max(1, min(int(limit or 20), 100))
    rows = _query_recent_reply_events(since_iso=since, limit=safe_limit)
    items = [
        NotificationItem(
            id=str(r.get("id", "")),
            sequence_id=str(r.get("sequence_id", "")),
            hunt_id=str(r.get("hunt_id", "")),
            campaign_id=str(r.get("campaign_id", "")),
            from_email=str(r.get("from_email", "")),
            subject=str(r.get("subject", "")),
            snippet=str(r.get("snippet", "")),
            received_at=str(r.get("received_at", "")),
        )
        for r in rows
    ]
    return NotificationsResponse(
        items=items,
        unread=len(items) if since else 0,
        last_seen_at=since,
    ).model_dump()


@router.post("/api/v1/notifications/mark-seen")
def mark_seen(request: Request) -> dict:
    """Persist the user's last-seen timestamp so the bell can compute
    unread count on the next poll.
    """
    require_api_access(request)
    require_user(request)
    settings = get_settings()
    # The user's identity isn't attached to UserCtx, so we use a single
    # global timestamp. In a multi-tenant future this would key by user_id.
    now_iso = datetime.now(timezone.utc).isoformat()
    # Stash as a sentinel file in the user data dir, alongside the DBs.
    from pathlib import Path
    state_path = Path(settings.email_db_path).with_suffix(".seen")
    try:
        state_path.write_text(now_iso, encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "last_seen_at": now_iso}
