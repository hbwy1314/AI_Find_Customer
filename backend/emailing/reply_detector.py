"""Detect inbound email replies via Microsoft Graph.

The legacy IMAP polling path has been removed. The Graph fetcher
(`graph_client.fetch_graph_replies`) and the shared
`process_inbound_messages` reply-matching loop are the only supported
flow. Per-account ``provider_type`` is coerced to ``"graph"`` at read
time so old rows that still say ``"smtp"`` keep working.
"""

from __future__ import annotations

import asyncio
import email
import uuid
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable

from api.hunt_store import load_hunt, save_hunt
from emailing.store import EmailStore

_AUTO_REPLY_SUBJECT_MARKERS = (
    "out of office",
    "automatic reply",
    "auto reply",
    "autoreply",
    "vacation",
    "on leave",
    "delivery status notification",
    "delivery failure",
    "mail delivery failed",
    "undeliverable",
    "failure notice",
    "read:",
    "read receipt",
)

_AUTO_REPLY_SNIPPET_MARKERS = (
    "i am currently out of office",
    "i'm currently out of office",
    "this is an automatic reply",
    "this is an auto reply",
    "thank you for your email. i am away",
    "delivery has failed",
    "could not be delivered",
    "recipient address rejected",
)

_AUTO_REPLY_LOCAL_PARTS = {
    "mailer-daemon",
    "postmaster",
    "noreply",
    "no-reply",
    "do-not-reply",
    "donotreply",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_message_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("<") and text.endswith(">"):
        return text
    return f"<{text.strip('<>')}>"


def _extract_message_ids(header_value: str) -> list[str]:
    text = str(header_value or "").strip()
    if not text:
        return []
    ids: list[str] = []
    for token in text.replace(",", " ").split():
        normalized = _normalize_message_id(token)
        if normalized:
            ids.append(normalized)
    return ids


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _normalize_subject(subject: str) -> str:
    text = _decode_header_value(subject).strip()
    prefixes = ("re:", "fw:", "fwd:", "sv:", "aw:")
    changed = True
    while changed and text:
        changed = False
        lowered = text.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
    return text


def _received_at(parsed: email.message.Message, fallback: str) -> str:
    raw = parsed.get("Date", "")
    if not raw:
        return fallback
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return fallback
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return fallback


def _extract_snippet(parsed: email.message.Message, max_chars: int = 280) -> str:
    body = parsed.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content() if hasattr(body, "get_content") else str(body)
        except Exception:
            text = str(body.get_payload(decode=True) or "")
    if not text:
        payload = parsed.get_payload(decode=True) or b""
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
    text = str(text or "").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _is_auto_reply(inbound: dict[str, Any]) -> bool:
    subject = str(inbound.get("subject", "") or "").lower()
    snippet = str(inbound.get("snippet", "") or "").lower()
    if any(marker in subject for marker in _AUTO_REPLY_SUBJECT_MARKERS):
        return True
    if any(marker in snippet for marker in _AUTO_REPLY_SNIPPET_MARKERS):
        return True
    from_email = str(inbound.get("from_email", "") or "").strip().lower()
    local = from_email.split("@", 1)[0] if "@" in from_email else from_email
    if local in _AUTO_REPLY_LOCAL_PARTS:
        return True
    headers = inbound.get("headers") or {}
    auto_submitted = str(headers.get("Auto-Submitted", "") or "").strip().lower()
    precedence = str(headers.get("Precedence", "") or "").strip().lower()
    x_autoreply = str(headers.get("X-Autoreply", "") or "").strip().lower()
    x_autorespond = str(headers.get("X-Autorespond", "") or "").strip().lower()
    x_failed_recipients = str(headers.get("X-Failed-Recipients", "") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if precedence in {"bulk", "junk", "list", "auto_reply"}:
        return True
    if x_autoreply or x_autorespond or x_failed_recipients:
        return True
    return False


def process_inbound_messages(
    store: EmailStore,
    inbound_messages: list[dict[str, Any]],
    current: str,
) -> dict[str, Any]:
    """Run the reply-matching loop over a pre-fetched inbound list.

    Used by the Graph fetcher (`run_graph_reply_detection_once`) so all
    inbound mail, regardless of mailbox, applies the same dedup /
    auto-reply filtering / sequence-stopping logic.

    Returns a dict with `checked / matched / skipped / ignored` counts
    plus a `matches` list of dicts suitable for `render_reply_detected_text`:
    each entry carries `{lead_email, lead_name, subject, snippet}`. The
    caller (the email-reply background loop) decides whether to push
    them to Feishu based on the user's settings.
    """
    checked = 0
    matched = 0
    skipped = 0
    ignored = 0
    matched_details: list[dict[str, str]] = []
    for inbound in inbound_messages:
        checked += 1
        raw_ref = str(inbound.get("raw_ref", "") or "")
        if raw_ref and store.has_reply_event(raw_ref):
            skipped += 1
            continue
        if _is_auto_reply(inbound):
            ignored += 1
            continue

        sent_message = _match_sent_message(store, inbound)
        if not sent_message:
            skipped += 1
            continue
        sequence = store.get_sequence(str(sent_message.get("sequence_id", "")))
        if not sequence:
            skipped += 1
            continue

        received_at = str(inbound.get("received_at", "") or current)
        store.create_reply_event({
            "id": str(uuid.uuid4()),
            "sequence_id": str(sequence["id"]),
            "message_id": str(sent_message.get("id", "") or ""),
            "from_email": str(inbound.get("from_email", "") or ""),
            "subject": str(inbound.get("subject", "") or ""),
            "snippet": str(inbound.get("snippet", "") or ""),
            "received_at": received_at,
            "raw_ref": raw_ref,
            "created_at": current,
        })
        store.update_sequence_status(
            str(sequence["id"]),
            status="replied",
            updated_at=current,
            replied_at=received_at,
            stop_reason="reply_detected",
        )
        # Mark the specific recipient row that replied. For the
        # multi-email waterfall, a reply on ANY recipient stops the
        # whole sequence (so we don't keep trying other emails in
        # the pool), but the bookkeeping still needs to know which
        # email replied for analytics.
        try:
            recipient = store.find_recipient_by_email(
                str(sequence["id"]), str(inbound.get("from_email", "") or "").strip().lower()
            )
            if recipient:
                store.mark_recipient_replied(
                    str(recipient["id"]), replied_at=received_at
                )
        except Exception:  # noqa: BLE001
            # Don't let bookkeeping failures block the reply event.
            pass
        store.cancel_future_pending_messages(str(sequence["id"]), updated_at=current)
        _refresh_hunt_email_summary(store, str(sequence["hunt_id"]), str(sequence["campaign_id"]))
        # Capture what we need for a Feishu push notification. The
        # caller decides whether to actually send one (gated by
        # `automation_reply_notifications_enabled`).
        from_email = str(inbound.get("from_email", "") or "")
        subject = str(inbound.get("subject", "") or "")
        snippet = str(inbound.get("snippet", "") or "")
        matched_details.append({
            "lead_email": from_email,
            "lead_name": str(sequence.get("lead_name", "") or ""),
            "subject": subject,
            "snippet": snippet,
        })
        # SSE push so the browser bell updates without waiting for the
        # 30s poll. Best-effort: never raise out of the detection loop.
        try:
            from api.sse import _broadcast_reply
            _broadcast_reply({
                "id": str(uuid.uuid4()),
                "sequence_id": str(sequence["id"]),
                "hunt_id": str(sequence.get("hunt_id", "") or ""),
                "campaign_id": str(sequence.get("campaign_id", "") or ""),
                "from_email": from_email,
                "subject": subject,
                "snippet": snippet,
                "received_at": received_at,
            })
        except Exception:
            # SSE is best-effort; an import error or broadcast hiccup
            # must never break the reply-detection loop.
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "[EmailReply] SSE broadcast failed for sequence %s", sequence["id"],
            )
        matched += 1

    return {
        "checked": checked,
        "matched": matched,
        "skipped": skipped,
        "ignored": ignored,
        "matches": matched_details,
    }


async def run_graph_reply_detection_once(
    store: EmailStore,
    account: dict[str, Any] | None = None,
    *,
    now_iso: str | None = None,
    recent_days: int = 14,
    limit: int = 100,
) -> dict[str, Any]:
    """Poll a Graph mailbox once and match replies to sent messages.

    With ``account=None`` it polls the global shared mailbox
    (``GRAPH_MAILBOX_UPN``); pass a connected account row to poll that
    account's own mailbox — replies land in whichever mailbox did the
    sending.
    """
    from emailing import graph_client

    current = now_iso or _now_iso()
    inbound_messages = await graph_client.fetch_graph_replies(
        account, now_iso=current, recent_days=recent_days, limit=limit
    )
    return process_inbound_messages(store, inbound_messages, current)


# Backward-compat alias: the legacy IMAP path exposed this signature
# so the test suite (and any older caller) can still pass a custom
# `fetcher` to inject reply messages. In production this always
# falls through to the Graph fetcher, but the test seam is preserved
# so the matching logic stays covered.
async def run_reply_detection_once(
    store: EmailStore,
    account: dict[str, Any] | None = None,
    *,
    now_iso: str | None = None,
    fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    recent_days: int = 14,
    limit: int = 100,
    **_: Any,
) -> dict[str, Any]:
    if fetcher is not None:
        # IMAP-style test seam: call the provided fetcher and run the
        # shared reply-matching loop on its output. This is only used
        # by tests; production code goes through run_graph_reply_detection_once.
        current = now_iso or _now_iso()
        inbound_messages = await asyncio.to_thread(
            fetcher, account or {}, now_iso=current
        )
        return process_inbound_messages(store, inbound_messages, current)
    return await run_graph_reply_detection_once(
        store,
        account,
        now_iso=now_iso,
        recent_days=recent_days,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Helpers shared with the scheduler / hunt store. Kept here so the Graph
# and (legacy) IMAP paths can both call them.
# ---------------------------------------------------------------------------

def _match_sent_message(store: EmailStore, inbound: dict[str, Any]) -> dict[str, Any] | None:
    # Walk the message-id / in-reply-to / references chain to find the
    # sent message that this reply is for. If nothing matches, fall
    # back to a (lead_email, subject) lookup which is threading-
    # tolerant (strips Re:/Fwd: prefixes, case-insensitive).
    candidates: list[str] = []
    message_id = _normalize_message_id(str(inbound.get("message_id", "") or ""))
    if message_id:
        candidates.append(message_id)
    in_reply_to = _normalize_message_id(str(inbound.get("in_reply_to", "") or ""))
    if in_reply_to:
        candidates.append(in_reply_to)
    for ref in inbound.get("references", []) or []:
        normalized = _normalize_message_id(str(ref or ""))
        if normalized:
            candidates.append(normalized)
    for mid in candidates:
        matched = store.find_message_by_provider_message_id(mid)
        if matched:
            return matched

    from_email = str(inbound.get("from_email", "") or "").strip().lower()
    normalized_subject = _normalize_subject(str(inbound.get("subject", "") or ""))
    if from_email and normalized_subject:
        return store.find_sent_message_by_lead_email_and_subject(from_email, normalized_subject)
    return None


def _refresh_hunt_email_summary(store: EmailStore, hunt_id: str, campaign_id: str) -> None:
    """Recompute hunt-level email counters and persist the hunt JSON."""
    hunt = load_hunt(hunt_id)
    if not hunt:
        return
    campaign = store.get_campaign(campaign_id)
    sequences = store.list_sequences_for_campaign(campaign_id)
    result = hunt.setdefault("result", {})
    result["email_campaign_summary"] = {
        "campaign_id": campaign_id,
        "status": campaign.get("status", "draft") if campaign else "draft",
        "sequences_total": len(sequences),
        "sent_count": store.count_messages_for_campaign(campaign_id, status="sent"),
        "failed_count": store.count_messages_for_campaign(campaign_id, status="failed"),
        "pending_count": store.count_messages_for_campaign(campaign_id, status="pending"),
        "replied_count": sum(1 for seq in sequences if seq.get("status") == "replied"),
    }
    save_hunt(hunt_id, hunt)
