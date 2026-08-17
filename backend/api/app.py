"""FastAPI application — main entry point for the AI Hunter API."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from argparse import Namespace
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from api.auth_routes import router as auth_router
from api.automation_routes import router as automation_router
from api.email_accounts_routes import router as email_accounts_router
from api.email_routes import (
    CreateCampaignRequest,
    create_email_campaign,
    start_email_campaign,
)
from api.email_routes import (
    router as email_router,
)
from api.hunt_store import load_all_hunts
from api.notifications_routes import router as notifications_router
from api.routes import (
    HuntRequest,
    TemplateSeedRequest,
    _hunts,
    _prepare_template_seed,
    create_hunt_internal,
    request_hunt_cancel,
    router,
    start_background_workers,
    stop_background_workers,
)
from api.settings_routes import router as settings_router
from api.sse import sse_router
from automation.job_queue import HuntJobQueue
from automation.metrics import collect_automation_metrics, collect_automation_status
from automation.notifier import (
    render_alert_text,
    render_discovery_batch_text,
    render_hunt_completed_text,
    render_hunt_failed_text,
    render_hunt_started_text,
    render_send_batch_text,
    render_summary_text,
    send_feishu_text,
)
from automation.runtime import update_worker_state
from config.settings import get_settings
from emailing.readiness import ensure_outbound_tested
from emailing.scheduler import run_scheduler_once
from emailing.store import EmailStore, set_email_store
from scripts.headless_worker import JobCancelledError, _campaign_name

# Configure logging for the entire application
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

# Suppress noisy LiteLLM logs (they spam "completion() model=..." on every call)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)
logging.getLogger("LiteLLM Proxy").setLevel(logging.WARNING)
logging.getLogger("LiteLLM Router").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

import litellm  # noqa: E402

litellm.suppress_debug_info = True
litellm.set_verbose = False


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _automation_worker_id() -> str:
    return f"{socket.gethostname()}:embedded-consumer"


def _notify_feishu(text: str) -> None:
    webhook_url = str(get_settings().automation_feishu_webhook_url or "").strip()
    if not webhook_url:
        return
    send_feishu_text(webhook_url, text)


async def _notify_feishu_async(text: str) -> None:
    await asyncio.to_thread(_notify_feishu, text)


def _extract_hunt_id_from_error(message: str) -> str:
    match = re.search(r"hunt\s+([^\s:]+)\s+failed:", str(message or ""))
    return str(match.group(1)) if match else ""


# Markers that signal a non-retryable failure: configuration is missing or
# the request itself is invalid. Retrying with the same payload will keep
# failing forever, so the consumer must mark the job failed instead of
# requeuing it. A leading "4xx" (HTTP 4xx raised as a string) is the
# strongest signal — that's how `create_email_campaign` and similar
# endpoints surface permanent validation errors today.
_NON_RETRYABLE_ERROR_MARKERS: tuple[str, ...] = (
    "4xx:",
    "400:",
    "401:",
    "403:",
    "404:",
    "409:",
    "422:",
    "not configured",
    "missing:",
    "is required",
    "validationerror",
    "invalid api key",
    "unauthorized",
)


def _is_non_retryable_automation_error(exc: BaseException) -> bool:
    """Return True if the exception is permanent (no point retrying)."""
    message = str(exc or "")
    if not message:
        return False
    lowered = message.lower()
    for marker in _NON_RETRYABLE_ERROR_MARKERS:
        if marker in lowered:
            return True
    # Last resort: look at the exception type name itself (e.g. ValueError,
    # KeyError) which we treat as caller bugs that won't be fixed by retry.
    type_name = type(exc).__name__.lower()
    if type_name in {"valueerror", "keyerror", "typeerror", "attributeerror"}:
        return True
    return False


def _embedded_consumer_enabled(settings) -> bool:
    # TestClient/pytest should not mutate the operator's real queue DB.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return bool(getattr(settings, "automation_embedded_consumer_enabled", True))


def _template_seed_prewarm_enabled(settings) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return bool(getattr(settings, "automation_template_seed_prewarm_enabled", True))


def _job_needs_template_seed(job: dict[str, object] | None) -> bool:
    if not isinstance(job, dict):
        return False
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return False
    if not bool(payload.get("enable_email_craft")):
        return False
    return not isinstance(payload.get("template_seed"), dict)


def _template_seed_request_from_payload(payload: dict[str, object]) -> TemplateSeedRequest:
    return TemplateSeedRequest(
        website_url=str(payload.get("website_url", "") or ""),
        description=str(payload.get("description", "") or ""),
        product_keywords=list(payload.get("product_keywords", []) or []),
        target_customer_profile=str(payload.get("target_customer_profile", "") or ""),
        target_regions=list(payload.get("target_regions", []) or []),
        uploaded_file_ids=list(payload.get("uploaded_file_ids", []) or payload.get("uploaded_files", []) or []),
        email_template_examples=list(payload.get("email_template_examples", []) or []),
        email_template_notes=str(payload.get("email_template_notes", "") or ""),
        email_account_id=(str(payload.get("email_account_id")).strip() or None)
        if payload.get("email_account_id") else None,
    )


async def _run_template_seed_prewarm_once() -> bool:
    settings = get_settings()
    queue = HuntJobQueue(settings.automation_queue_db_path)
    queue.init_db()

    update_worker_state(
        "template_seed",
        enabled=_template_seed_prewarm_enabled(settings),
        running=True,
        worker_id=f"{socket.gethostname()}:template-seed",
        last_poll_at=_now_iso(),
    )
    for job in queue.list_jobs(limit=50):
        if str(job.get("status", "") or "") != "queued":
            continue
        if str(job.get("template_seed_status", "") or "") == "preparing":
            continue
        if not _job_needs_template_seed(job):
            continue
        job_id = str(job.get("id", "") or "")
        if not job_id or not queue.mark_template_seed_preparing(job_id, updated_at=_now_iso()):
            continue
        update_worker_state(
            "template_seed",
            active_job_id=job_id,
            last_activity_at=_now_iso(),
            last_error="",
        )
        try:
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            template_seed = await _prepare_template_seed(_template_seed_request_from_payload(payload))
            queue.attach_template_seed(job_id, template_seed=template_seed, updated_at=_now_iso())
            update_worker_state(
                "template_seed",
                active_job_id="",
                last_completed_job_id=job_id,
                last_activity_at=_now_iso(),
                last_error="",
            )
            logger.info("[TemplateSeedWorker] prewarmed template seed for job=%s", job_id[:8])
        except Exception as exc:
            queue.mark_template_seed_failed(job_id, error_message=str(exc), updated_at=_now_iso())
            update_worker_state(
                "template_seed",
                active_job_id="",
                last_activity_at=_now_iso(),
                last_error=str(exc),
            )
            logger.warning("[TemplateSeedWorker] prewarm failed for job=%s: %s", job_id[:8], exc)
        return True

    update_worker_state(
        "template_seed",
        active_job_id="",
        last_poll_at=_now_iso(),
    )
    return False


async def _template_seed_prewarm_loop() -> None:
    while True:
        try:
            settings = get_settings()
            if not _template_seed_prewarm_enabled(settings):
                update_worker_state(
                    "template_seed",
                    enabled=False,
                    running=False,
                    active_job_id="",
                    last_poll_at=_now_iso(),
                )
                await asyncio.sleep(5)
                continue
            update_worker_state(
                "template_seed",
                enabled=True,
                running=True,
                worker_id=f"{socket.gethostname()}:template-seed",
            )
            did_work = await _run_template_seed_prewarm_once()
            if did_work:
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            update_worker_state("template_seed", last_error="prewarm iteration failed", last_activity_at=_now_iso())
            logger.exception("[TemplateSeedWorker] prewarm iteration failed")
        await asyncio.sleep(max(1, int(get_settings().automation_consumer_poll_seconds)))


async def _wait_for_hunt_embedded(*, hunt_id: str, poll_seconds: int, should_cancel: object | None = None) -> dict[str, str]:
    last_status = ""
    while True:
        if callable(should_cancel) and should_cancel():
            raise JobCancelledError(f"queue job cancelled while waiting for hunt {hunt_id}")
        hunt = _hunts.get(hunt_id)
        if not hunt:
            raise RuntimeError(f"hunt {hunt_id} not found")
        current = str(hunt.get("status", "") or "")
        stage = str(hunt.get("current_stage", "") or "")
        if current != last_status:
            result = hunt.get("result") or {}
            logger.info(
                "hunt=%s status=%s stage=%s leads=%s emails=%s",
                hunt_id[:8],
                current,
                stage or "-",
                len(result.get("leads", []) or []),
                hunt.get("email_sequences_count", 0),
            )
            last_status = current
        if current in {"completed", "failed", "cancelled"}:
            return {
                "status": current,
                "error": str(hunt.get("error", "") or ""),
            }
        await asyncio.sleep(max(1, poll_seconds))


async def _run_embedded_consumer_job(args: Namespace, payload: dict[str, object]) -> dict[str, object]:
    progress_callback = getattr(args, "progress_callback", None)
    cancel_check = getattr(args, "cancel_check", None)

    def report(stage: str, message: str, **extra: object) -> None:
        if callable(progress_callback):
            progress_callback(stage=stage, message=message, **extra)

    def ensure_not_cancelled() -> None:
        if callable(cancel_check) and cancel_check():
            raise JobCancelledError("Queue job cancelled by user")

    if bool(payload.get("enable_email_craft")) and not isinstance(payload.get("template_seed"), dict):
        try:
            ensure_not_cancelled()
            report("template_seed", "Preparing email template seed", template_seed_status="preparing")
            template_seed = await _prepare_template_seed(_template_seed_request_from_payload(payload))
            payload = dict(payload)
            payload["template_seed"] = template_seed
            report(
                "template_seed",
                "Template seed prepared",
                template_seed_status="ready",
                template_seed_source=str(template_seed.get("source", "") or "pre_generated"),
            )
            logger.info("prepared template seed before hunt creation")
        except Exception as exc:
            report("template_seed", f"Template seed preparation failed: {exc}", template_seed_status="failed")
            logger.warning("template seed preparation failed, continuing without pre-generated seed: %s", exc)

    ensure_not_cancelled()
    report("create_hunt", "Creating hunt from queue job")
    created = await create_hunt_internal(HuntRequest(**payload))
    hunt_id = str(created.hunt_id)
    report("hunt_created", "Hunt created, waiting for execution", hunt_id=hunt_id)
    try:
        await _notify_feishu_async(render_hunt_started_text(payload, hunt_id=hunt_id))
    except Exception as exc:
        logger.warning("failed to send start notification for hunt=%s: %s", hunt_id[:8], exc)

    try:
        ensure_not_cancelled()
        report("wait_hunt", "Consumer is polling hunt status", hunt_id=hunt_id)
        status = await _wait_for_hunt_embedded(
            hunt_id=hunt_id,
            poll_seconds=int(args.status_poll_seconds),
            should_cancel=cancel_check,
        )
        if str(status.get("status", "")) == "cancelled":
            # The hunt was cancelled (via the job cancel route or the
            # hunt detail page). Don't raise RuntimeError — that path
            # treats the failure as retryable and burns attempts.
            # JobCancelledError is caught by the outer handler and ends
            # the loop without requeueing.
            raise JobCancelledError(
                f"queue job cancelled while waiting for hunt {hunt_id}"
            )
        if str(status.get("status", "")) != "completed":
            raise RuntimeError(f"hunt {hunt_id} failed: {status.get('error', 'unknown error')}")

        ensure_not_cancelled()
        report("load_result", "Loading completed hunt result", hunt_id=hunt_id)
        hunt = _hunts.get(hunt_id) or {}
        result = hunt.get("result") or {}
        leads = result.get("leads") or []
        sequences = result.get("email_sequences") or []

        campaign_summary: dict[str, object] | None = None
        if args.auto_start_campaign and payload.get("enable_email_craft"):
            ensure_not_cancelled()
            if not sequences:
                # A hunt that found no leads produces no email sequences.
                # That is a valid outcome, not an error: skip campaign
                # creation and complete the job. Calling
                # create_email_campaign here would raise 400 "No generated
                # email sequences" and requeue the job forever.
                report(
                    "campaign_skipped",
                    "Hunt produced no email sequences; nothing to send",
                    hunt_id=hunt_id,
                )
                campaign_summary = {
                    "campaign_id": "",
                    "status": "skipped_no_sequences",
                    "sequence_count": 0,
                }
            else:
                report("create_campaign", "Creating campaign from approved email sequences", hunt_id=hunt_id)
                # Plumb the user-pinned account (if any) through to the
                # campaign. None / empty falls back to the auto-managed
                # default account, which follows `Settings.email_provider_type`.
                pinned_account_id = payload.get("email_account_id")
                if isinstance(pinned_account_id, str):
                    pinned_account_id = pinned_account_id.strip() or None
                created_campaign = await create_email_campaign(
                    hunt_id,
                    CreateCampaignRequest(
                        name=_campaign_name(args.campaign_name_prefix, hunt_id),
                        email_account_id=pinned_account_id,
                    ),
                )
                campaign_id = str(created_campaign.campaign_id)
                sequence_count = int(created_campaign.sequence_count or 0)
                if sequence_count > 0:
                    ensure_not_cancelled()
                    report("start_campaign", "Starting campaign and handing off to scheduler", hunt_id=hunt_id)
                    campaign_summary = await start_email_campaign(campaign_id)
                else:
                    report("campaign_draft", "Campaign created but no send-ready sequences were available", hunt_id=hunt_id)
                    campaign_summary = {"campaign_id": campaign_id, "status": "draft", "sequence_count": 0}

        final_result: dict[str, object] = {
            "hunt_id": hunt_id,
            "website_url": str(payload.get("website_url", "") or ""),
            "lead_count": len(leads) if isinstance(leads, list) else 0,
            "email_sequence_count": len(sequences) if isinstance(sequences, list) else 0,
            "campaign": campaign_summary,
        }
        report("completed", "Queue job completed", hunt_id=hunt_id)
        try:
            await _notify_feishu_async(render_hunt_completed_text(final_result))
        except Exception as exc:
            logger.warning("failed to send completion notification for hunt=%s: %s", hunt_id[:8], exc)
        return final_result
    except Exception as exc:
        report("failed", f"Queue job failed: {exc}", hunt_id=hunt_id)
        try:
            await _notify_feishu_async(render_hunt_failed_text(payload, error_message=str(exc)))
        except Exception as notify_exc:
            logger.warning("failed to send failure notification for hunt=%s: %s", hunt_id[:8], notify_exc)
        raise


async def _run_automation_consumer_once() -> bool:
    settings = get_settings()
    queue = HuntJobQueue(settings.automation_queue_db_path)
    queue.init_db()
    update_worker_state(
        "consumer",
        enabled=_embedded_consumer_enabled(settings),
        running=True,
        worker_id=_automation_worker_id(),
        last_poll_at=_now_iso(),
    )
    job = queue.claim_next(worker_id=_automation_worker_id(), now_iso=_now_iso())
    if not job:
        update_worker_state(
            "consumer",
            active_job_id="",
            last_poll_at=_now_iso(),
        )
        return False

    job_id = str(job["id"])
    update_worker_state(
        "consumer",
        active_job_id=job_id,
        last_claimed_job_id=job_id,
        last_activity_at=_now_iso(),
        last_error="",
    )
    logger.info("[AutomationConsumer] claimed job=%s", job_id[:8])
    queue.update_progress(
        job_id,
        updated_at=_now_iso(),
        progress_stage="claimed",
        progress_message="Embedded consumer claimed this queue job",
    )

    consumer_args = Namespace(
        auto_start_campaign=bool(settings.automation_consumer_auto_start_campaign),
        campaign_name_prefix="Auto Campaign",
        status_poll_seconds=int(settings.automation_consumer_status_poll_seconds),
    )
    consumer_args.progress_callback = lambda stage, message, **extra: queue.update_progress(
        job_id,
        updated_at=_now_iso(),
        progress_stage=str(stage or ""),
        progress_message=str(message or ""),
        hunt_id=str(extra.get("hunt_id", "") or ""),
        template_seed_status=extra.get("template_seed_status"),
        template_seed_source=extra.get("template_seed_source"),
    )
    consumer_args.cancel_check = lambda: queue.is_cancellation_requested(job_id)

    try:
        result = await _run_embedded_consumer_job(consumer_args, job.get("payload") or {})
        queue.mark_completed(job_id, hunt_id=str(result["hunt_id"]), finished_at=_now_iso())
        update_worker_state(
            "consumer",
            active_job_id="",
            last_completed_job_id=job_id,
            last_activity_at=_now_iso(),
            last_error="",
        )
        logger.info("[AutomationConsumer] completed job=%s hunt=%s", job_id[:8], str(result['hunt_id'])[:8])
    except JobCancelledError as exc:
        latest_job = queue.get(job_id) or {}
        hunt_id = str(latest_job.get("last_hunt_id", "") or job.get("last_hunt_id", "") or "")
        if hunt_id:
            request_hunt_cancel(hunt_id, reason=str(exc))
        update_worker_state(
            "consumer",
            active_job_id="",
            last_activity_at=_now_iso(),
            last_error=str(exc),
        )
        logger.warning("[AutomationConsumer] job=%s cancelled: %s", job_id[:8], exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        attempts_used = int(job.get("attempt_count", 0) or 0)
        max_attempts = max(1, int(getattr(settings, "automation_consumer_max_attempts", 5) or 5))
        # Non-retryable errors (missing config, validation failures, etc.)
        # fail the same way every time — requeueing just wastes attempts
        # and burns the user's quota. Mark them failed immediately and
        # surface the error.
        if _is_non_retryable_automation_error(exc):
            queue.mark_failed(
                job_id,
                error_message=f"non-retryable: {exc}",
                finished_at=_now_iso(),
            )
            update_worker_state(
                "consumer",
                active_job_id="",
                last_activity_at=_now_iso(),
                last_error=str(exc),
            )
            logger.error(
                "[AutomationConsumer] job=%s marked failed (non-retryable): %s",
                job_id[:8], exc,
            )
        elif attempts_used >= max_attempts:
            # Transient errors that exhausted the attempt budget.
            queue.mark_failed(
                job_id,
                error_message=f"stopped after {attempts_used} attempts: {exc}",
                finished_at=_now_iso(),
            )
            update_worker_state(
                "consumer",
                active_job_id="",
                last_activity_at=_now_iso(),
                last_error=str(exc),
            )
            logger.error(
                "[AutomationConsumer] job=%s permanently failed after %s attempt(s): %s",
                job_id[:8], attempts_used, exc,
            )
        else:
            available_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=max(1, int(settings.automation_consumer_retry_delay_seconds)))
            ).isoformat()
            queue.requeue(
                job_id,
                available_at=available_at,
                error_message=str(exc),
                updated_at=_now_iso(),
                hunt_id=_extract_hunt_id_from_error(str(exc)),
            )
            update_worker_state(
                "consumer",
                active_job_id="",
                last_activity_at=_now_iso(),
                last_error=str(exc),
            )
            logger.exception("[AutomationConsumer] job=%s failed and was requeued: %s", job_id[:8], exc)
    return True


async def _automation_consumer_loop() -> None:
    while True:
        try:
            settings = get_settings()
            if not _embedded_consumer_enabled(settings):
                update_worker_state(
                    "consumer",
                    enabled=False,
                    running=False,
                    active_job_id="",
                    last_poll_at=_now_iso(),
                )
                await asyncio.sleep(5)
                continue
            update_worker_state("consumer", enabled=True, running=True, worker_id=_automation_worker_id())
            did_work = await _run_automation_consumer_once()
            if did_work:
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            update_worker_state("consumer", last_error="polling iteration failed", last_activity_at=_now_iso())
            logger.exception("[AutomationConsumer] polling iteration failed")
        await asyncio.sleep(max(1, int(get_settings().automation_consumer_poll_seconds)))


async def _email_scheduler_loop() -> None:
    """Poll pending email jobs and dispatch due messages."""
    while True:
        try:
            settings = get_settings()
            if not bool(settings.email_auto_send_enabled):
                await asyncio.sleep(60)
                continue
            # Provider-aware gate: checks GRAPH_* + a verified Graph test.
            # The scheduler previously did an unconditional SMTP check
            # which made every iteration throw on Graph-only deployments.
            ensure_outbound_tested(settings)
            store = EmailStore(settings.email_db_path)
            store.init_db()
            result = await run_scheduler_once(store)
            if result["sent"] or result["failed"]:
                logger.info("[EmailScheduler] sent=%s failed=%s skipped=%s", result["sent"], result["failed"], result["skipped"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[EmailScheduler] polling iteration failed")
        await asyncio.sleep(60)


async def _email_reply_loop() -> None:
    """Poll inbox for replies and stop follow-up sequences.

    Uses Microsoft Graph exclusively. Each connected Graph account may
    send from its own UPN, so replies land there; the loop also polls
    the global shared mailbox (Graph ``MAILBOX_UPN``) as a fallback.
    """
    while True:
        try:
            settings = get_settings()
            if not bool(settings.email_reply_detection_enabled):
                await asyncio.sleep(max(30, int(settings.email_reply_check_interval_seconds)))
                continue
            store = EmailStore(settings.email_db_path)
            store.init_db()
            from emailing import graph_client
            from emailing.reply_detector import run_graph_reply_detection_once

            # Poll every distinct Graph mailbox: each connected account
            # may send from its own UPN, so replies land there. Falls
            # back to the global shared mailbox when configured.
            poll_accounts: list[dict[str, Any] | None] = []
            seen_upns: set[str] = set()
            for acct in store.list_accounts_by_provider("graph"):
                if str(acct.get("status", "active")) != "active":
                    continue
                upn = graph_client.account_upn(acct)
                if not upn or upn in seen_upns:
                    continue
                seen_upns.add(upn)
                poll_accounts.append(acct)
            global_upn = graph_client.account_upn(None)
            if global_upn and global_upn not in seen_upns:
                poll_accounts.append(None)
            if not poll_accounts:
                poll_accounts = [None]

            result = {"checked": 0, "matched": 0, "skipped": 0, "ignored": 0, "matches": []}
            for poll_account in poll_accounts:
                part = await run_graph_reply_detection_once(store, poll_account)
                for key in ("checked", "matched", "skipped", "ignored"):
                    result[key] += int(part.get(key, 0) or 0)
                result["matches"].extend(part.get("matches", []) or [])
            if result["matched"]:
                logger.info(
                    "[EmailReply][graph] checked=%s matched=%s skipped=%s",
                    result["checked"], result["matched"], result["skipped"],
                )
            await _maybe_notify_reply_matches(result.get("matches", []))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[EmailReply] polling iteration failed")
        await asyncio.sleep(max(30, int(settings.email_reply_check_interval_seconds)))


async def _maybe_notify_reply_matches(matches: list[dict[str, str]]) -> None:
    """Push a single batched Feishu notification for newly matched replies.

    Gated by `automation_reply_notifications_enabled` (default True) and
    by the presence of a Feishu webhook URL. If the webhook is missing,
    no-op — the in-app notification bell still surfaces the same events.

    Multiple matches in a single poll cycle are collapsed into one
    message so a busy day doesn't translate to a notification flood.
    """
    if not matches:
        return
    settings = get_settings()
    if not bool(getattr(settings, "automation_reply_notifications_enabled", True)):
        return
    webhook_url = str(getattr(settings, "automation_feishu_webhook_url", "") or "").strip()
    if not webhook_url:
        return
    try:
        from automation.notifier import render_reply_detected_text, send_feishu_text
        text = render_reply_detected_text(matches)
        if not text:
            return
        await asyncio.to_thread(send_feishu_text, webhook_url, text)
        logger.info(
            "[EmailReply] Feishu reply-alert sent for %d match(es)", len(matches),
        )
    except Exception:
        # Notification is best-effort; never let a webhook hiccup stop
        # the reply-detection loop from continuing on the next cycle.
        logger.exception("[EmailReply] Feishu reply-alert push failed")


async def _automation_notify_loop() -> None:
    last_summary_at = 0.0
    last_alert_at = 0.0
    discovery_buffer: list[dict[str, str | int]] = []
    send_buffer: list[dict[str, str]] = []
    seen_hunt_ids: set[str] = set()
    seen_sent_message_ids: set[str] = set()
    primed = False
    last_discovery_flush_at = 0.0
    last_send_flush_at = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            settings = get_settings()
            webhook_url = str(settings.automation_feishu_webhook_url or "").strip()
            if not webhook_url:
                await asyncio.sleep(60)
                continue

            now_monotonic = loop.time()
            if bool(getattr(settings, "automation_event_notifications_enabled", True)):
                batch_flush_interval = max(60, int(getattr(settings, "automation_event_flush_interval_seconds", 600) or 600))
                discovery_batch_size = max(1, int(getattr(settings, "automation_discovery_batch_size", 5) or 5))
                send_batch_size = max(1, int(getattr(settings, "automation_send_batch_size", 10) or 10))

                hunts = load_all_hunts(mark_interrupted=False)
                store = EmailStore(settings.email_db_path)
                store.init_db()
                if not primed:
                    seen_hunt_ids.update(
                        hunt_id for hunt_id, hunt in hunts.items()
                        if str(hunt.get("status", "") or "") == "completed"
                    )
                    seen_sent_message_ids.update(
                        str(item.get("id", "") or "")
                        for item in store.list_sent_messages_since(since_iso="1970-01-01T00:00:00+00:00", limit=5000)
                        if str(item.get("id", "") or "")
                    )
                    primed = True
                    last_discovery_flush_at = now_monotonic
                    last_send_flush_at = now_monotonic

                for hunt_id, hunt in hunts.items():
                    if hunt_id in seen_hunt_ids:
                        continue
                    if str(hunt.get("status", "") or "") != "completed":
                        continue
                    seen_hunt_ids.add(hunt_id)
                    result = hunt.get("result") or {}
                    for lead in result.get("leads", []) or []:
                        if not isinstance(lead, dict):
                            continue
                        emails = lead.get("emails", []) or []
                        discovery_buffer.append({
                            "company_name": str(lead.get("company_name", "") or ""),
                            "website": str(lead.get("website", "") or ""),
                            "email_count": len(emails) if isinstance(emails, list) else 0,
                        })

                for item in store.list_sent_messages_since(since_iso="1970-01-01T00:00:00+00:00", limit=500):
                    message_id = str(item.get("id", "") or "")
                    if not message_id or message_id in seen_sent_message_ids:
                        continue
                    seen_sent_message_ids.add(message_id)
                    send_buffer.append({
                        "company_name": str(item.get("lead_name", "") or ""),
                        "lead_email": str(item.get("lead_email", "") or ""),
                        "subject": str(item.get("subject", "") or ""),
                    })

                if discovery_buffer and (
                    len(discovery_buffer) >= discovery_batch_size
                    or now_monotonic - last_discovery_flush_at >= batch_flush_interval
                ):
                    text = render_discovery_batch_text(discovery_buffer[:])
                    await asyncio.to_thread(send_feishu_text, webhook_url, text)
                    discovery_buffer.clear()
                    last_discovery_flush_at = now_monotonic
                    logger.info("[AutomationNotify] discovery batch sent")

                if send_buffer and (
                    len(send_buffer) >= send_batch_size
                    or now_monotonic - last_send_flush_at >= batch_flush_interval
                ):
                    text = render_send_batch_text(send_buffer[:])
                    await asyncio.to_thread(send_feishu_text, webhook_url, text)
                    send_buffer.clear()
                    last_send_flush_at = now_monotonic
                    logger.info("[AutomationNotify] send batch sent")

            if bool(settings.automation_summary_enabled):
                interval = max(300, int(settings.automation_summary_interval_seconds or 7200))
                if now_monotonic - last_summary_at >= interval:
                    status = collect_automation_status()
                    metrics = collect_automation_metrics(hours=max(1, interval // 3600))
                    metrics["status_snapshot"] = status
                    text = render_summary_text(metrics)
                    await asyncio.to_thread(send_feishu_text, webhook_url, text)
                    last_summary_at = now_monotonic
                    logger.info("[AutomationNotify] summary sent")

            if bool(settings.automation_alerts_enabled):
                alert_interval = max(300, int(settings.automation_alert_interval_seconds or 1800))
                if now_monotonic - last_alert_at >= alert_interval:
                    status = collect_automation_status()
                    metrics = collect_automation_metrics(hours=2)
                    should_alert = (
                        status["hunt_jobs"]["queued"] >= int(settings.automation_alert_backlog_threshold or 20)
                        or status["email_queue"]["pending"] >= int(settings.automation_alert_backlog_threshold or 20)
                        or metrics["emails"]["failed"] >= int(settings.automation_alert_failed_messages_threshold or 10)
                    )
                    if should_alert:
                        text = render_alert_text(status, metrics)
                        await asyncio.to_thread(send_feishu_text, webhook_url, text)
                        logger.warning("[AutomationNotify] alert sent")
                    last_alert_at = now_monotonic
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[AutomationNotify] loop failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    settings = get_settings()
    app.state.settings = settings
    app.state.email_scheduler_task = None
    app.state.email_reply_task = None
    app.state.automation_notify_task = None
    app.state.automation_consumer_task = None
    app.state.template_seed_task = None
    email_store = EmailStore(settings.email_db_path)
    email_store.init_db()
    set_email_store(email_store)
    app.state.email_store = email_store
    queue = HuntJobQueue(settings.automation_queue_db_path)
    queue.init_db()
    recovered_jobs = queue.recover_interrupted_running_jobs(updated_at=_now_iso())
    if recovered_jobs:
        logger.warning("[AutomationConsumer] recovered %s interrupted running job(s) after startup", recovered_jobs)

    # Enable Langfuse tracing if configured
    from observability.setup import setup_observability
    setup_observability()

    app.state.email_scheduler_task = asyncio.create_task(_email_scheduler_loop())
    logger.info("[EmailScheduler] background loop started")
    app.state.email_reply_task = asyncio.create_task(_email_reply_loop())
    logger.info("[EmailReply] background loop started")
    app.state.automation_notify_task = asyncio.create_task(_automation_notify_loop())
    logger.info("[AutomationNotify] background loop started")
    app.state.template_seed_task = asyncio.create_task(_template_seed_prewarm_loop())
    logger.info("[TemplateSeedWorker] background loop started")
    app.state.automation_consumer_task = asyncio.create_task(_automation_consumer_loop())
    logger.info("[AutomationConsumer] background loop started")
    update_worker_state("consumer", enabled=_embedded_consumer_enabled(settings), running=True, worker_id=_automation_worker_id())

    start_background_workers()

    try:
        yield
    finally:
        task = getattr(app.state, "email_scheduler_task", None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            logger.info("[EmailScheduler] background loop stopped")
        reply_task = getattr(app.state, "email_reply_task", None)
        if reply_task:
            reply_task.cancel()
            with suppress(asyncio.CancelledError):
                await reply_task
            logger.info("[EmailReply] background loop stopped")
        template_seed_task = getattr(app.state, "template_seed_task", None)
        if template_seed_task:
            template_seed_task.cancel()
            with suppress(asyncio.CancelledError):
                await template_seed_task
            logger.info("[TemplateSeedWorker] background loop stopped")
        notify_task = getattr(app.state, "automation_notify_task", None)
        if notify_task:
            notify_task.cancel()
            with suppress(asyncio.CancelledError):
                await notify_task
            logger.info("[AutomationNotify] background loop stopped")
        consumer_task = getattr(app.state, "automation_consumer_task", None)
        if consumer_task:
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
            logger.info("[AutomationConsumer] background loop stopped")
        update_worker_state("consumer", running=False, active_job_id="")

    await stop_background_workers()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="AI Hunter API",
        description="Multi-agent B2B lead hunting pipeline",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # TrustedHost: in production restrict to the host portion of public_base_url
    # (set automatically by Settings._apply_production_defaults).
    trusted_hosts = settings.trusted_hosts or ["*"]
    if trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    # Trust X-Forwarded-Proto from nginx so cookies get Secure when behind HTTPS.
    class ProxyHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            forwarded_proto = request.headers.get("x-forwarded-proto")
            if forwarded_proto:
                request.scope["scheme"] = forwarded_proto.split(",")[0].strip()
            return await call_next(request)
    app.add_middleware(ProxyHeadersMiddleware)

    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/v1")
    app.include_router(automation_router)
    app.include_router(email_router)
    app.include_router(email_accounts_router, prefix="/api/v1/email-accounts")
    app.include_router(notifications_router)
    app.include_router(sse_router, prefix="/api/v1")
    # Public unsubscribe endpoints (no auth — recipients click from email).
    from api.unsubscribe_routes import router as unsubscribe_router
    app.include_router(unsubscribe_router)
    if settings.settings_api_enabled:
        app.include_router(settings_router)

    return app


app = create_app()
