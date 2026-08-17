"""Simple scheduler for pending outbound emails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from api.hunt_store import load_hunt, save_hunt
from config.settings import get_settings
from emailing.email_sender import send_email
from emailing.store import EmailStore
from emailing.unsubscribe import build_mailto_unsubscribe, build_unsubscribe_url, issue_token


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_fallback_account(
    store: EmailStore,
    current_account: dict[str, Any],
    sent_today_cache: dict[str, int],
    now_iso: str,
) -> dict[str, Any] | None:
    """Pick another active account of the same provider with remaining quota.

    Used when a sequence's bound account hit its daily cap: instead of
    stalling until tomorrow, the sequence is rebound to the least-loaded
    eligible mailbox (rotation order breaks ties). Returns None when every
    mailbox of that provider is capped out.
    """
    provider = str(current_account.get("provider_type", "") or "").strip().lower()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for row in store.list_accounts_by_provider(provider):
        if str(row.get("status", "active")) != "active":
            continue
        row_id = str(row.get("id", "") or "")
        if not row_id or row_id == str(current_account.get("id", "") or ""):
            continue
        limit = int(row.get("daily_send_limit", 0) or 0)
        used = sent_today_cache.get(row_id)
        if used is None:
            used = store.count_sent_today_for_account(row_id, now_iso=now_iso)
            sent_today_cache[row_id] = used
        if limit > 0 and used >= limit:
            continue
        candidates.append((int(used), int(row.get("sort_order", 0) or 0), row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _refresh_hunt_email_summary(store: EmailStore, hunt_id: str, campaign_id: str) -> None:
    hunt = load_hunt(hunt_id)
    if not hunt:
        return
    campaign = store.get_campaign(campaign_id)
    sequences = store.list_sequences_for_campaign(campaign_id)
    settings = get_settings()
    template_summary = store.get_template_performance_for_campaign(
        campaign_id,
        underperforming_min_assigned=int(getattr(settings, "email_template_underperforming_min_assigned", 10) or 10),
        underperforming_min_reply_rate=float(getattr(settings, "email_template_underperforming_min_reply_rate", 1.0) or 1.0),
    )
    summary = {
        "campaign_id": campaign_id,
        "status": campaign.get("status", "draft") if campaign else "draft",
        "sequences_total": len(sequences),
        "sent_count": store.count_messages_for_campaign(campaign_id, status="sent"),
        "failed_count": store.count_messages_for_campaign(campaign_id, status="failed"),
        "pending_count": store.count_messages_for_campaign(campaign_id, status="pending"),
        "replied_count": sum(1 for seq in sequences if seq.get("status") == "replied"),
        "template_summary": list(template_summary.values()),
    }
    result = hunt.setdefault("result", {})
    result["email_campaign_summary"] = summary
    save_hunt(hunt_id, hunt)


async def run_scheduler_once(
    store: EmailStore,
    *,
    now_iso: str | None = None,
    sender: Callable[..., Awaitable[dict[str, Any]]] = send_email,
) -> dict[str, int]:
    """Send pending email jobs that are ready."""
    current = now_iso or _now_iso()
    jobs = store.list_pending_messages_ready(current)
    sent = 0
    failed = 0
    skipped = 0
    # Per-account sent-today counters. Cached per account_id so a single
    # scheduler pass with many jobs for the same account doesn't re-query
    # the count for every send.
    sent_today_cache: dict[str, int] = {}
    for job in jobs:
        sequence = store.get_sequence(str(job.get("sequence_id", "")))
        if not sequence or sequence.get("status") in {"replied", "stopped", "completed", "failed"}:
            skipped += 1
            continue
        campaign = store.get_campaign(str(sequence.get("campaign_id", "")))
        if not campaign or campaign.get("status") != "active":
            skipped += 1
            continue
        # Resolve the effective outbound account: the sequence-level
        # binding (set by campaign-creation rotation) wins over the
        # campaign-level default.
        account = None
        seq_account_id = str(sequence.get("email_account_id", "") or "")
        if seq_account_id:
            candidate = store.get_account(seq_account_id)
            if candidate and str(candidate.get("status", "active")) == "active":
                account = candidate
        if account is None:
            account = store.get_account(str(campaign.get("email_account_id", "")))
        if not account or account.get("status") != "active":
            store.mark_message_failed(str(job["id"]), failure_reason="inactive_email_account", updated_at=current)
            failed += 1
            continue

        # Per-account daily limit guard. When the bound account reached its
        # cap, first try to rotate the sequence to another eligible mailbox
        # of the same provider; only when every mailbox is capped out do we
        # defer to tomorrow (counter resets at 00:00 UTC).
        account_id = str(account.get("id", "") or "")
        daily_limit = int(account.get("daily_send_limit", 0) or 0)
        if daily_limit > 0:
            used_today = sent_today_cache.get(account_id)
            if used_today is None:
                used_today = store.count_sent_today_for_account(account_id, now_iso=current)
                sent_today_cache[account_id] = used_today
            if used_today >= daily_limit:
                fallback = _pick_fallback_account(store, account, sent_today_cache, current)
                if fallback is not None:
                    store.rebind_sequence_account(
                        str(sequence["id"]),
                        email_account_id=str(fallback["id"]),
                        updated_at=current,
                    )
                    account = fallback
                    account_id = str(fallback["id"])
                    daily_limit = int(fallback.get("daily_send_limit", 0) or 0)
                    used_today = sent_today_cache.get(account_id, 0)
                if daily_limit > 0 and used_today >= daily_limit:
                    # Re-queue for the same time tomorrow (or +24h from now if
                    # already past midnight) so the campaign doesn't appear stuck.
                    next_day = (
                        datetime.fromisoformat(current.replace("Z", "+00:00"))
                        if "T" in current
                        else datetime.now(timezone.utc)
                    )
                    if next_day.tzinfo is None:
                        next_day = next_day.replace(tzinfo=timezone.utc)
                    next_at = next_day.replace(hour=0, minute=5, second=0, microsecond=0)
                    if next_at <= next_day:
                        next_at = next_at + timedelta(days=1)
                    store.update_sequence_status(
                        str(sequence["id"]),
                        status=str(sequence.get("status", "running") or "running"),
                        updated_at=current,
                        next_scheduled_at=next_at.isoformat(),
                    )
                    # Don't mark the message as failed — the campaign is healthy, just capped.
                    skipped += 1
                    continue

        template_id = str(sequence.get("template_id", "") or "")
        if template_id:
            settings = get_settings()
            template_summary = store.get_template_performance_for_campaign(
                str(sequence.get("campaign_id", "")),
                underperforming_min_assigned=int(getattr(settings, "email_template_underperforming_min_assigned", 10) or 10),
                underperforming_min_reply_rate=float(getattr(settings, "email_template_underperforming_min_reply_rate", 1.0) or 1.0),
            )
            template_perf = template_summary.get(template_id)
            template_status = str((template_perf or {}).get("status", "") or "")
            if template_status in {"underperforming", "exhausted"}:
                store.cancel_future_pending_messages(str(sequence["id"]), updated_at=current)
                store.update_sequence_status(
                    str(sequence["id"]),
                    status="stopped",
                    updated_at=current,
                    stop_reason=f"template_{template_status}",
                    next_scheduled_at="",
                )
                skipped += 1
                _refresh_hunt_email_summary(store, str(sequence["hunt_id"]), str(sequence["campaign_id"]))
                continue

        recipient = str(sequence.get("lead_email", "") or "").strip()
        # Unsubscribe guard — required for CAN-SPAM/GDPR compliance. A
        # global 'all' opt-out stops every future send to this address;
        # a finer scope (campaign:{id}) stops just that campaign. Either
        # way, the message is marked failed (not retried) and the
        # sequence is parked with stop_reason='unsubscribed' so the UI
        # surfaces it correctly.
        if recipient and store.is_unsubscribed(
            recipient,
            scope=f"campaign:{str(sequence.get('campaign_id', '') or '')}",
        ):
            store.mark_message_failed(
                str(job["id"]),
                failure_reason="recipient_unsubscribed",
                updated_at=current,
            )
            store.update_sequence_status(
                str(sequence["id"]),
                status="stopped",
                updated_at=current,
                stop_reason="unsubscribed",
                next_scheduled_at="",
            )
            skipped += 1
            continue

        # Build unsubscribe URL + mailto fallback so every email carries
        # a one-click opt-out. Scope is the campaign so unsubscribing
        # here only blocks this campaign (the recipient can still get
        # mail from other campaigns unless they hit the global 'all'
        # link, which is in the same URL via the landing page).
        unscope = f"campaign:{str(sequence.get('campaign_id', '') or '')}"
        untoken = issue_token(recipient or "unknown", scope=unscope) if recipient else ""
        un_base = str(getattr(get_settings(), "public_base_url", "") or "").strip() or "http://api.nineluan.com"
        un_url = build_unsubscribe_url(un_base, untoken) if untoken else ""
        un_mailto = build_mailto_unsubscribe(recipient) if recipient else ""

        result = await sender(
            account,
            to_email=recipient,
            subject=str(job.get("subject", "") or ""),
            body_text=str(job.get("body_text", "") or ""),
            reply_to=str(account.get("reply_to", "") or ""),
            thread_key=str(job.get("thread_key", "") or ""),
            list_unsubscribe_url=un_url or None,
            list_unsubscribe_mailto=un_mailto or None,
        )
        if result.get("ok"):
            store.mark_message_sent(
                str(job["id"]),
                provider_message_id=str(result.get("provider_message_id", "") or ""),
                thread_key=str(result.get("thread_key", "") or ""),
                sent_at=current,
            )
            sent_today_cache[account_id] = sent_today_cache.get(account_id, 0) + 1
            step_number = int(job.get("step_number", 1) or 1)
            next_message = store.get_message_for_step(str(sequence["id"]), step_number + 1)
            next_scheduled = str(next_message.get("scheduled_at", "") or "") if next_message else ""
            store.update_sequence_status(
                str(sequence["id"]),
                status="completed" if not next_message else "running",
                updated_at=current,
                current_step=step_number,
                last_sent_at=current,
                next_scheduled_at=next_scheduled,
            )
            sent += 1
        else:
            store.mark_message_failed(
                str(job["id"]),
                failure_reason=str(result.get("error_type", "") or result.get("error", "") or "send_failed"),
                updated_at=current,
            )
            store.update_sequence_status(
                str(sequence["id"]),
                status="failed",
                updated_at=current,
                stop_reason=str(result.get("error_type", "") or "send_failed"),
            )
            failed += 1
        _refresh_hunt_email_summary(store, str(sequence["hunt_id"]), str(sequence["campaign_id"]))
    return {"sent": sent, "failed": failed, "skipped": skipped}
