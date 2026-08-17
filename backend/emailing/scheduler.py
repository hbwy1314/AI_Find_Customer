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


def _advance_expired_recipients(store: EmailStore, current_iso: str) -> int:
    """Flip ``waiting_reply`` recipients past the waterfall window to
    ``skipped`` and clone the latest sent message into a new pending
    row addressed to the next pending recipient.

    Returns the number of recipients flipped. Called once per
    scheduler pass so we react to "no reply" the next minute, not
    the next day.

    The window is taken from settings.email_recipient_waterfall_days
    (default 3). 0 disables the waterfall — we then never advance
    waiting recipients, and a single-email sequence behaves exactly
    like the legacy code.
    """
    settings = get_settings()
    raw_days = getattr(settings, "email_recipient_waterfall_days", 3)
    # NB: `raw_days` may be 0 (disabled) — preserve the zero, don't
    # fall back to 3 on falsy.
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 3
    if days <= 0:
        return 0
    threshold = (
        datetime.fromisoformat(current_iso.replace("Z", "+00:00"))
        if "T" in current_iso
        else datetime.now(timezone.utc)
    )
    if threshold.tzinfo is None:
        threshold = threshold.replace(tzinfo=timezone.utc)
    cutoff = (threshold - timedelta(days=days)).isoformat()
    expired = store.waiting_recipients_older_than(cutoff)
    flipped = 0
    for r in expired:
        store.advance_waiting_recipient(str(r["id"]), updated_at=current_iso)
        flipped += 1
        # Pick the next pending recipient for this sequence. If none
        # remain, the sequence is exhausted — nothing more to send.
        next_recipient = store.next_pending_recipient(str(r["sequence_id"]))
        if next_recipient is None:
            # Mark the sequence as exhausted so the UI surfaces it.
            try:
                store.update_sequence_status(
                    str(r["sequence_id"]),
                    status="exhausted",
                    updated_at=current_iso,
                    stop_reason="all_recipients_tried",
                    next_scheduled_at="",
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        # Update sequence's primary lead_email to the next recipient
        # (so any subsequent steps we create — and the unsubscribe /
        # dedup logic — use the new address).
        store.update_sequence_lead_email(
            str(r["sequence_id"]),
            lead_email=str(next_recipient["email"]),
            updated_at=current_iso,
        )
        # Clone the most-recent message for this sequence into a
        # fresh pending row. The previous message stays as `sent`
        # for history. The new row will be picked up on the next
        # scheduler pass.
        latest = store.latest_message_for_sequence(str(r["sequence_id"]))
        if latest:
            store.clone_pending_message_after(
                str(latest["id"]), scheduled_at=current_iso
            )
    return flipped


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
    # WATERFALL STEP 1: flip any waiting_reply recipients whose
    # `last_attempt_at` is older than the configured waterfall window
    # to `skipped`. After this, `next_pending_recipient` will pick the
    # next candidate in the pool. This runs first so a sequence
    # that's been waiting gets a fresh shot on the very next pass.
    _advance_expired_recipients(store, current)

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
        if not sequence or sequence.get("status") in {"replied", "stopped", "completed", "failed", "exhausted"}:
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

        # WATERFALL: resolve the actual recipient. The pool is checked
        # first (multi-email mode); we fall back to sequence.lead_email
        # for legacy single-recipient sequences that never registered
        # a pool. If both are empty there's nothing to send — skip the
        # job so it doesn't loop forever in pending.
        recipient = ""
        recipient_row = store.next_pending_recipient(str(sequence["id"]))
        if recipient_row:
            recipient = str(recipient_row.get("email", "") or "").strip()
        if not recipient:
            recipient = str(sequence.get("lead_email", "") or "").strip()
        if not recipient:
            store.mark_message_failed(
                str(job["id"]),
                failure_reason="no_recipient",
                updated_at=current,
            )
            failed += 1
            continue
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
            # WATERFALL: mark this recipient as waiting_reply so the
            # scheduler will time it out and clone a fresh message
            # for the next recipient if no reply arrives. Done BEFORE
            # the status calculation so the exhaustion check below
            # sees the recipient in waiting_reply state.
            if recipient_row is not None:
                store.mark_recipient_sent(
                    str(recipient_row["id"]), sent_at=current
                )
            # Decide sequence status:
            # - exhausted: pool is empty (no pending + no waiting)
            # - completed (legacy): no recipient pool AND no next step
            # - running: anything else
            next_step_message = store.get_message_for_step(
                str(sequence["id"]), step_number + 1
            )
            recipients = store.list_recipients(str(sequence["id"]))
            if recipients:
                if store.is_sequence_exhausted(str(sequence["id"])):
                    new_status = "exhausted"
                    stop_reason = "all_recipients_tried"
                    next_sched = ""
                else:
                    new_status = "running"
                    stop_reason = None
                    next_sched = ""
            elif next_step_message is not None:
                new_status = "running"
                stop_reason = None
                next_sched = str(next_step_message.get("scheduled_at", "") or "")
            else:
                new_status = "completed"
                stop_reason = None
                next_sched = ""
            store.update_sequence_status(
                str(sequence["id"]),
                status=new_status,
                updated_at=current,
                current_step=step_number,
                last_sent_at=current,
                next_scheduled_at=next_sched,
                stop_reason=stop_reason,
            )
            sent += 1
        else:
            error_kind = str(result.get("error_type", "") or result.get("error", "") or "send_failed")
            store.mark_message_failed(
                str(job["id"]),
                failure_reason=error_kind,
                updated_at=current,
            )
            # WATERFALL: don't park the whole sequence on a single
            # send failure — just retire this recipient (e.g. bad
            # address, mailbox full) and let the next pending one
            # try. We only mark the sequence `failed` if this was a
            # legacy single-recipient sequence (no pool). For pooled
            # sequences the status decision is the same as the success
            # branch — exhausted if pool is empty, running otherwise.
            if recipient_row is not None:
                store.mark_recipient_failed(
                    str(recipient_row["id"]),
                    reason=error_kind,
                    updated_at=current,
                )
                recipients = store.list_recipients(str(sequence["id"]))
                if recipients and store.is_sequence_exhausted(str(sequence["id"])):
                    new_status = "exhausted"
                    stop_reason = "all_recipients_failed"
                else:
                    new_status = "running"
                    stop_reason = None
                store.update_sequence_status(
                    str(sequence["id"]),
                    status=new_status,
                    updated_at=current,
                    stop_reason=stop_reason,
                )
                # If a next pending recipient exists, clone a fresh
                # message for it in THIS pass so the scheduler
                # immediately retries with the next candidate. Without
                # this the sequence would look stalled until the
                # next scheduler tick — bad for product UX and
                # confusing in tests.
                next_rec = store.next_pending_recipient(str(sequence["id"]))
                if next_rec is not None:
                    store.update_sequence_lead_email(
                        str(sequence["id"]),
                        lead_email=str(next_rec["email"]),
                        updated_at=current,
                    )
                    latest = store.latest_message_for_sequence(str(sequence["id"]))
                    if latest:
                        new_msg_id = store.clone_pending_message_after(
                            str(latest["id"]), scheduled_at=current
                        )
                        if new_msg_id:
                            new_job = store.get_message(new_msg_id)
                            if new_job:
                                jobs.append(new_job)
            else:
                # Legacy single-recipient behavior: a failed send
                # parks the sequence. Tests that exercise this path
                # use `status="failed"` in the response.
                store.update_sequence_status(
                    str(sequence["id"]),
                    status="failed",
                    updated_at=current,
                    stop_reason=error_kind,
                )
            failed += 1
        _refresh_hunt_email_summary(store, str(sequence["hunt_id"]), str(sequence["campaign_id"]))
    return {"sent": sent, "failed": failed, "skipped": skipped}
