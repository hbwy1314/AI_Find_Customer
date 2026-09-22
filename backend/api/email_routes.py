"""Email campaign API routes."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agents.email_craft_agent import _active_step_specs
from api.hunt_store import load_hunt, now_iso, save_hunt
from api.security import require_admin, require_api_access, require_resource_access, require_user
from config.settings import get_settings
from emailing.policy import expand_email_targets
from emailing.readiness import ensure_inbound_tested, ensure_outbound_ready, ensure_outbound_tested
from emailing.reply_detector import run_graph_reply_detection_once
from emailing.scheduler import run_scheduler_once
from emailing.store import EmailStore

router = APIRouter(prefix="/api/v1", tags=["email"])


def _store() -> EmailStore:
    store = EmailStore(get_settings().email_db_path)
    store.init_db()
    return store


def _render_email_html(body_text: str, locale: str | None = None) -> str:
    """Render the preview HTML for ``body_text`` (placeholder unsubscribe).

    ``locale`` is the campaign's primary language and is forwarded
    to the unsubscribe-card renderer so the button text + prompt
    + "reply 'unsubscribe'" hint stay in the same language as the
    body. Without this the card stays Chinese on an English email
    and the recipient is more likely to ignore it.

    Import is local so module-load order stays simple (body_format /
    html_format are otherwise imported by the email craft agent and
    can pull in heavy ML deps we don't need at request time).
    """
    from emailing.html_format import render_preview_html
    return render_preview_html(body_text, locale=locale)


def _pick_account_for_campaign(
    store: EmailStore,
    *,
    owner_user_id: int | None = None,
    now_iso_str: str | None = None,
) -> dict[str, Any] | None:
    """Pick an active account with remaining quota for a new campaign.
    
    Returns the first active Graph account (by sort_order) that has
    not exceeded its daily or hourly send limit. Returns None when
    every account is capped out or no accounts exist.
    
    When owner_user_id is provided, only considers accounts owned by
    that user or system accounts (owner_user_id=0).
    """
    current = now_iso_str or now_iso()
    candidates = []
    for row in store.list_accounts():
        if str(row.get("status", "active")) != "active":
            continue
        if str(row.get("provider_type", "graph")) != "graph":
            continue
        account_id = str(row.get("id", "") or "")
        if not account_id or account_id == "default":
            continue
        # Multi-tenant filter: skip accounts not owned by this user
        if owner_user_id is not None:
            account_owner = int(row.get("owner_user_id", 0) or 0)
            if account_owner != 0 and account_owner != owner_user_id:
                continue
        
        # Check daily limit
        daily_limit = int(row.get("daily_send_limit", 0) or 0)
        if daily_limit > 0:
            used_today = store.count_sent_today_for_account(account_id, now_iso=current)
            if used_today >= daily_limit:
                continue
        
        # Check hourly limit
        hourly_limit = int(row.get("hourly_send_limit", 0) or 0)
        if hourly_limit > 0:
            used_hour = store.count_sent_last_hour_for_account(account_id, now_iso=current)
            if used_hour >= hourly_limit:
                continue
        
        # This account has remaining quota
        candidates.append(row)
    
    # list_accounts() already sorts by sort_order, created_at, so the
    # first candidate is the one with the lowest sort_order
    return candidates[0] if candidates else None


def _sequence_is_campaign_ready(sequence: dict[str, Any]) -> bool:
    manual_review = sequence.get("manual_review")
    if isinstance(manual_review, dict):
        decision = str(manual_review.get("decision", "") or "")
        if decision == "approved":
            return True
        if decision == "rejected":
            return False
    if not bool(getattr(get_settings(), "email_require_approval_before_send", True)):
        return True
    return bool(sequence.get("auto_send_eligible"))


def _campaign_summary(store: EmailStore, campaign_id: str) -> dict[str, Any]:
    settings = get_settings()
    campaign = store.get_campaign(campaign_id)
    sequences = store.list_sequences_for_campaign(campaign_id)
    template_summary = store.get_template_performance_for_campaign(
        campaign_id,
        underperforming_min_assigned=int(getattr(settings, "email_template_underperforming_min_assigned", 10) or 10),
        underperforming_min_reply_rate=float(getattr(settings, "email_template_underperforming_min_reply_rate", 1.0) or 1.0),
    )
    return {
        "campaign": campaign,
        "sequence_count": len(sequences),
        "sent_count": store.count_messages_for_campaign(campaign_id, status="sent"),
        "pending_count": store.count_messages_for_campaign(campaign_id, status="pending"),
        "failed_count": store.count_messages_for_campaign(campaign_id, status="failed"),
        "template_summary": list(template_summary.values()),
        "sequences": sequences,
    }


def _write_summary_to_hunt(store: EmailStore, hunt_id: str, campaign_id: str) -> None:
    hunt = load_hunt(hunt_id)
    if not hunt:
        return
    result = hunt.setdefault("result", {})
    settings = get_settings()
    campaign = store.get_campaign(campaign_id)
    sequences = store.list_sequences_for_campaign(campaign_id)
    template_summary = store.get_template_performance_for_campaign(
        campaign_id,
        underperforming_min_assigned=int(getattr(settings, "email_template_underperforming_min_assigned", 10) or 10),
        underperforming_min_reply_rate=float(getattr(settings, "email_template_underperforming_min_reply_rate", 1.0) or 1.0),
    )
    result["email_campaign_summary"] = {
        "campaign_id": campaign_id,
        "status": campaign.get("status", "draft") if campaign else "draft",
        "sequences_total": len(sequences),
        "sent_count": store.count_messages_for_campaign(campaign_id, status="sent"),
        "failed_count": store.count_messages_for_campaign(campaign_id, status="failed"),
        "pending_count": store.count_messages_for_campaign(campaign_id, status="pending"),
        "replied_count": sum(1 for seq in sequences if seq.get("status") == "replied"),
        "template_summary": list(template_summary.values()),
    }
    generated_sequences = result.get("email_sequences")
    if isinstance(generated_sequences, list):
        for sequence in generated_sequences:
            if not isinstance(sequence, dict):
                continue
            template_id = str(sequence.get("template_id") or "")
            if template_id and template_id in template_summary:
                performance = template_summary[template_id]
                sequence["template_assigned_count"] = performance.get("assigned_count", sequence.get("template_assigned_count", 0))
                sequence["template_remaining_capacity"] = performance.get("remaining_capacity", sequence.get("template_remaining_capacity", 0))
                sequence["template_performance"] = {
                    "sent_count": performance.get("sent_count", 0),
                    "replied_count": performance.get("replied_count", 0),
                    "reply_rate": performance.get("reply_rate", 0.0),
                    "status": performance.get("status", "warming_up"),
                    "optimization_needed": bool(performance.get("optimization_needed", False)),
                    "recommended_action": str(performance.get("recommended_action", "keep_collecting_data") or "keep_collecting_data"),
                    "reason": str(performance.get("reason", "") or ""),
                }
    save_hunt(hunt_id, hunt)


class CreateCampaignRequest(BaseModel):
    name: str = "Outbound Campaign"
    email_account_id: str | None = None


class CampaignResponse(BaseModel):
    campaign_id: str
    status: str
    sequence_count: int


async def _create_email_campaign_internal(
    hunt_id: str,
    payload: CreateCampaignRequest,
    *,
    owner_user_id: int = 0,
) -> CampaignResponse:
    """Create a campaign without requiring an HTTP Request context.

    Called by the automation consumer directly (no session / no Request).
    The HTTP route ``create_email_campaign`` wraps this after doing its
    own access-control check.
    """
    hunt = load_hunt(hunt_id)
    if not hunt or not isinstance(hunt.get("result"), dict):
        raise ValueError(f"Hunt {hunt_id} result not found")
    sequences = hunt["result"].get("email_sequences", [])
    if not isinstance(sequences, list) or not sequences:
        raise ValueError("No generated email sequences found for this hunt")

    existing_campaigns = _store().list_campaigns_for_hunt(hunt_id)
    for existing in existing_campaigns:
        if str(existing.get("name", "")) == payload.name and str(existing.get("status", "")) in {"draft", "active", "paused"}:
            summary = _campaign_summary(_store(), str(existing["id"]))
            return CampaignResponse(
                campaign_id=str(existing["id"]),
                status=str(existing.get("status", "draft")),
                sequence_count=int(summary["sequence_count"]),
            )

    settings = get_settings()
    # Readiness errors (ValueError) propagate to the HTTP route, which
    # maps them to 409.
    ensure_outbound_ready(settings)

    store = _store()
    requested_account_id = str(payload.email_account_id or "").strip()
    if requested_account_id:
        account = store.get_account(requested_account_id)
        if not account or str(account.get("status", "active")) != "active":
            raise ValueError("Selected email account is not active")
    else:
        account = _pick_account_for_campaign(store, owner_user_id=owner_user_id)
        if not account:
            raise ValueError(
                "No available email account found. All accounts have reached "
                "their send limits or no active Graph accounts are configured."
            )
    campaign_id = str(uuid.uuid4())
    created = now_iso()
    store.create_campaign({
        "id": campaign_id,
        "owner_user_id": owner_user_id or int(hunt.get("owner_user_id", 0) or 0),
        "hunt_id": hunt_id,
        "email_account_id": account["id"],
        "name": payload.name,
        "status": "draft",
        "language_mode": settings.email_language_mode,
        "default_language": settings.email_default_language,
        "fallback_language": settings.email_fallback_language,
        "tone": settings.email_tone,
        "step1_delay_days": settings.email_step1_delay_days,
        "step2_delay_days": settings.email_step2_delay_days,
        "step3_delay_days": settings.email_step3_delay_days,
        "min_fit_score": settings.email_min_fit_score_to_send,
        "min_contactability_score": settings.email_min_contactability_score_to_send,
        "created_at": created,
        "updated_at": created,
    })
    base_time = datetime.now(timezone.utc)
    created_lead_keys: set[str] = set()
    for seq in sequences:
        lead = seq.get("lead") or {}
        primary_target = seq.get("target") or {}
        raw_targets = []
        if isinstance(primary_target, dict):
            raw_targets.append(primary_target)
        raw_targets.extend(seq.get("targets") or [])
        raw_targets.extend(expand_email_targets(lead) or [])
        seen_target_emails: set[str] = set()
        targets = []
        for target in raw_targets:
            if not isinstance(target, dict):
                continue
            target_email = str(target.get("target_email", "") or "").strip().lower()
            if not target_email or target_email in seen_target_emails:
                continue
            seen_target_emails.add(target_email)
            targets.append(target)
        emails = seq.get("emails") or []
        template_perf = seq.get("template_performance") or {}
        template_status = str(template_perf.get("status", "") or "")
        if not targets or not emails:
            continue
        if not _sequence_is_campaign_ready(seq):
            continue
        if template_status in {"underperforming", "exhausted"}:
            continue
        for target in targets:
            lead_identity = str(lead.get("website") or lead.get("company_name") or "").strip().lower()
            if not lead_identity:
                lead_identity = hashlib.sha256(
                    json.dumps(lead, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                ).hexdigest()
            lead_key = f"lead:{lead_identity}"
            if lead_key in created_lead_keys or store.has_contact_history_for_lead_key(lead_key):
                continue
            created_lead_keys.add(lead_key)
            sequence_id = str(uuid.uuid4())
            store.create_sequence({
                "id": sequence_id,
                "campaign_id": campaign_id,
                "hunt_id": hunt_id,
                "lead_key": lead_key or (sequence_id.lower() + "|" + str(target.get("target_email") or "").lower()),
                "lead_email": str(target.get("target_email") or ""),
                "lead_name": str(lead.get("company_name") or ""),
                "decision_maker_name": str(target.get("target_name") or ""),
                "decision_maker_title": str(target.get("target_title") or ""),
                "locale": str(seq.get("locale") or "en_US"),
                "generation_mode": str(seq.get("generation_mode") or "personalized"),
                "template_id": str(seq.get("template_id") or ""),
                "template_group": str(seq.get("template_group") or ""),
                "template_usage_index": int(seq.get("template_usage_index", 0) or 0),
                "template_max_send_count": int(seq.get("template_max_send_count", 0) or 0),
                "status": "scheduled",
                "current_step": 0,
                "stop_reason": "",
                "replied_at": "",
                "last_sent_at": "",
                "next_scheduled_at": "",
                "created_at": created,
                "updated_at": created,
                "email_account_id": account["id"] if requested_account_id else "",
            })
            next_scheduled = ""
            step_specs = _active_step_specs()
            for index, email in enumerate(emails):
                step_number = int(email.get("sequence_number", 1) or 1)
                llm_day = email.get("suggested_send_day")
                fallback_day = (
                    step_specs[index]["suggested_send_day"]
                    if index < len(step_specs)
                    else (step_specs[-1]["suggested_send_day"] if step_specs else 0)
                )
                try:
                    day_value = int(llm_day) if llm_day is not None else fallback_day
                except (TypeError, ValueError):
                    day_value = fallback_day
                if day_value < 0:
                    day_value = 0
                scheduled_at = (base_time + timedelta(days=day_value)).isoformat()
                if step_number == 1:
                    next_scheduled = scheduled_at
                store.create_message({
                    "id": str(uuid.uuid4()),
                    "sequence_id": sequence_id,
                    "step_number": step_number,
                    "goal": str(email.get("email_type", "") or ""),
                    "locale": str(seq.get("locale") or "en_US"),
                    "subject": str(email.get("subject", "") or ""),
                    "body_text": str(email.get("body_text", "") or ""),
                    "body_html": _render_email_html(
                        str(email.get("body_text", "") or ""),
                        locale=str(seq.get("locale") or "") or None,
                    ),
                    "status": "pending",
                    "scheduled_at": scheduled_at,
                    "sent_at": "",
                    "provider_message_id": "",
                    "thread_key": "",
                    "failure_reason": "",
                    "created_at": created,
                    "updated_at": created,
                })
            store.update_sequence_status(sequence_id, status="scheduled", updated_at=created, next_scheduled_at=next_scheduled)
            target_emails = [
                str(t.get("target_email") or "").strip().lower()
                for t in targets
                if str(t.get("target_email") or "").strip()
            ]
            if target_emails:
                is_role_based_per_email = {
                    str(t.get("target_email") or "").strip().lower(): bool(t.get("is_role_based"))
                    for t in targets
                    if str(t.get("target_email") or "").strip()
                }
                store.add_recipients(
                    sequence_id,
                    target_emails,
                    is_role_based_per_email=is_role_based_per_email,
                )

    _write_summary_to_hunt(store, hunt_id, campaign_id)
    summary = _campaign_summary(store, campaign_id)
    return CampaignResponse(campaign_id=campaign_id, status="draft", sequence_count=summary["sequence_count"])


@router.post("/hunts/{hunt_id}/email-campaigns", response_model=CampaignResponse, dependencies=[Depends(require_api_access)])
async def create_email_campaign(hunt_id: str, payload: CreateCampaignRequest, request: Request):
    hunt = load_hunt(hunt_id)
    if not hunt or not isinstance(hunt.get("result"), dict):
        raise HTTPException(status_code=404, detail="Hunt result not found")
    require_resource_access(request, hunt.get("owner_user_id"))
    owner = require_user(request)
    account_owner_id = int(hunt.get("owner_user_id", 0) or 0) if owner.via != "session" else owner.user_id
    # Explicitly-selected mailbox: enforce account-level access here.
    # Auto-picked accounts are resolved ONCE inside
    # `_create_email_campaign_internal` (it filters by the same
    # owner_user_id), so we don't duplicate the pick here — picking twice
    # could bind a different mailbox than the one we just authorized.
    requested_account_id = str(payload.email_account_id or "").strip()
    if requested_account_id:
        store = _store()
        account = store.get_account(requested_account_id)
        if not account or str(account.get("status", "active")) != "active":
            raise HTTPException(status_code=400, detail="Selected email account is not active")
        require_resource_access(request, account.get("owner_user_id"))
    try:
        return await _create_email_campaign_internal(hunt_id, payload, owner_user_id=account_owner_id)
    except ValueError as exc:
        # Re-raise readiness errors (missing config) as 409 so callers
        # can distinguish "config not ready" from "bad request".
        detail = str(exc)
        status_code = 409 if any(
            marker in detail.lower()
            for marker in ("not configured", "not tested", "missing", "graph", "smtp", "provider")
        ) else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get("/hunts/{hunt_id}/email-campaigns", dependencies=[Depends(require_api_access)])
async def list_email_campaigns(hunt_id: str, request: Request):
    hunt = load_hunt(hunt_id)
    if not hunt:
        raise HTTPException(status_code=404, detail="Hunt not found")
    require_resource_access(request, hunt.get("owner_user_id"))
    store = _store()
    campaigns = store.list_campaigns_for_hunt(hunt_id)
    return [{"campaign": c, **_campaign_summary(store, c["id"])} for c in campaigns]


async def _start_email_campaign_internal(campaign_id: str) -> dict[str, str]:
    """Start a campaign without requiring an HTTP Request context.

    Called by the automation consumer directly (no session / no Request).
    The HTTP route ``start_email_campaign`` wraps this after doing its
    own access-control check.
    """
    store = _store()
    campaign = store.get_campaign(campaign_id)
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")
    settings = get_settings()
    # Readiness errors (ValueError) propagate to the HTTP route, which
    # maps them to 409.
    ensure_outbound_tested(settings)
    campaign_account_id = str(campaign.get("email_account_id", ""))
    updated = now_iso()
    if campaign_account_id == "default":
        # Legacy campaign: rebind to a real account before starting
        logger = logging.getLogger(__name__)
        real_account = _pick_account_for_campaign(
            store,
            owner_user_id=int(campaign.get("owner_user_id", 0) or 0),
            now_iso_str=updated,
        )
        if not real_account:
            raise ValueError(
                "Cannot start campaign: no available email accounts found. "
                "All accounts have reached their send limits or no Graph accounts exist."
            )
        # Rebind campaign from 'default' to real account
        with store._connect() as conn:
            conn.execute(
                "UPDATE email_campaigns SET email_account_id = ?, updated_at = ? WHERE id = ?",
                (real_account["id"], updated, campaign_id),
            )
        logger.info(
            "Rebound legacy campaign %s from 'default' to account %s (%s)",
            campaign_id[:8], real_account["id"], real_account.get("from_email")
        )
    store.update_campaign_status(campaign_id, "active", updated_at=updated)
    _write_summary_to_hunt(store, str(campaign["hunt_id"]), campaign_id)
    return {"campaign_id": campaign_id, "status": "active"}


@router.post("/email-campaigns/{campaign_id}/start", dependencies=[Depends(require_api_access)])
async def start_email_campaign(campaign_id: str, request: Request):
    store = _store()
    campaign = store.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    hunt = load_hunt(str(campaign["hunt_id"]))
    require_resource_access(request, (hunt or {}).get("owner_user_id"))
    try:
        result = await _start_email_campaign_internal(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.post("/email-campaigns/{campaign_id}/pause", dependencies=[Depends(require_api_access)])
async def pause_email_campaign(campaign_id: str, request: Request):
    store = _store()
    campaign = store.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    hunt = load_hunt(str(campaign["hunt_id"]))
    require_resource_access(request, (hunt or {}).get("owner_user_id"))
    updated = now_iso()
    store.update_campaign_status(campaign_id, "paused", updated_at=updated)
    _write_summary_to_hunt(store, str(campaign["hunt_id"]), campaign_id)
    return {"campaign_id": campaign_id, "status": "paused"}


@router.get("/email-sequences/{sequence_id}", dependencies=[Depends(require_api_access)])
async def get_email_sequence(sequence_id: str, request: Request):
    store = _store()
    sequence = store.get_sequence(sequence_id)
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    hunt = load_hunt(str(sequence.get("hunt_id", "")))
    require_resource_access(request, (hunt or {}).get("owner_user_id"))
    messages = store.list_messages_for_sequence(sequence_id)
    reply_events = store.list_reply_events_for_sequence(sequence_id)
    return {"sequence": sequence, "messages": messages, "reply_events": reply_events}


@router.post("/utilities/render-email-html", dependencies=[Depends(require_api_access)])
async def render_email_html(payload: dict[str, str]):
    """Render a plain-text email body as the HTML the recipient
    will see.

    Used by the in-product email preview to fill in ``body_html``
    for sequences that were generated before the HTML pipeline
    existed (so they have only ``body_text`` in storage). Without
    this, the operator would see the old plain-text footer in the
    preview even though the actual outbound mail has the new HTML
    card with the clickable unsubscribe button.
    """
    body_text = str(payload.get("body_text", "") or "")
    locale = payload.get("locale") or None
    if not body_text.strip():
        raise HTTPException(status_code=400, detail="body_text is required")
    from emailing.html_format import render_preview_html
    return {"body_html": render_preview_html(body_text, locale=locale)}


@router.post(
    "/email-scheduler/run",
    dependencies=[Depends(require_api_access), Depends(require_admin)],
)
async def run_email_scheduler():
    store = _store()
    try:
        ensure_outbound_tested(get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await run_scheduler_once(store)


@router.post(
    "/email-replies/check",
    dependencies=[Depends(require_api_access), Depends(require_admin)],
)
async def run_email_reply_check():
    store = _store()
    settings = get_settings()
    try:
        ensure_inbound_tested(settings)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    
    from emailing import graph_client
    poll_accounts = graph_client.distinct_poll_accounts(
        store.list_accounts_by_provider("graph"),
        compat_scan=bool(getattr(settings, "email_inbox_compat_scan_enabled", True)),
    )
    
    result = {"checked": 0, "matched": 0, "skipped": 0, "ignored": 0}
    for poll_account in poll_accounts:
        part = await run_graph_reply_detection_once(store, poll_account)
        for key in ("checked", "matched", "skipped", "ignored"):
            result[key] += int(part.get(key, 0) or 0)
    return result
