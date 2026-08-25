"""SSE streaming endpoints — real-time updates via Server-Sent Events.

Two streams today:
- `/hunts/{id}/stream` — hunt pipeline progress (uses the per-hunt
  broadcast queue in `routes._sse_queues`).
- `/replies/stream` — global reply-notification push. Any open
  browser tab gets a `reply` event the moment the reply-detection
  loop matches a new inbound message, so the bell badge updates
  without waiting for the 30s poll.
- `/automation/jobs/{id}/stream` — automation job progress.

All streams emit a `heartbeat` event every ~30s so reverse proxies
and load balancers don't kill an idle connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.automation_routes import _serialize_job
from api.routes import _hunts, _sse_queues
from api.security import require_api_access
from automation.job_queue import HuntJobQueue
from config.settings import get_settings

logger = logging.getLogger(__name__)

sse_router = APIRouter()


def _automation_job_queue() -> HuntJobQueue:
    settings = get_settings()
    queue = HuntJobQueue(settings.automation_queue_db_path)
    queue.init_db()
    return queue


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    json_data = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {json_data}\n\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Reply-event push channel
# ---------------------------------------------------------------------------
# Each browser tab that subscribes to /replies/stream gets its own
# bounded queue. When the reply-detection loop matches a new inbound
# message, it calls `_broadcast_reply()` which fans out to every
# subscriber queue. Slow clients get their oldest queued event
# dropped so they don't block newer ones.

_REPLY_QUEUE_MAX = 200
_reply_subscribers: list[asyncio.Queue] = []


def _broadcast_reply(data: dict[str, Any]) -> None:
    """Push a reply event to every active SSE subscriber.

    Best-effort: full queues are drained for the offending subscriber
    (slow client protection) and stale queues (closed connections) are
    pruned. This function never raises — pushing a notification is
    never allowed to break the reply-detection loop.
    """
    if not _reply_subscribers:
        return
    stale: list[asyncio.Queue] = []
    for q in _reply_subscribers:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            # Drop the oldest to make room. If even that fails (e.g.
            # another coroutine is also draining), mark the queue
            # stale so we drop the subscription on the next pass.
            try:
                q.get_nowait()
                q.put_nowait(data)
            except Exception:  # noqa: BLE001
                stale.append(q)
    for q in stale:
        try:
            _reply_subscribers.remove(q)
        except ValueError:
            pass


async def _reply_event_generator() -> AsyncGenerator[str, None]:
    """Per-subscriber SSE stream for reply events."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_REPLY_QUEUE_MAX)
    _reply_subscribers.append(queue)
    logger.info("[SSE] reply stream subscribed (total=%d)", len(_reply_subscribers))
    try:
        # Initial frame so the browser's EventSource flips to OPEN and
        # the React effect can clear any "connecting" state.
        yield _sse_event("heartbeat", {"connected_at": _now_iso()})
        while True:
            try:
                # wait_for gives us a chance to send heartbeats even
                # when no new replies are arriving — keeps proxies and
                # load balancers from severing an idle connection.
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield _sse_event("reply", data)
            except asyncio.TimeoutError:
                yield _sse_event("heartbeat", {"ts": time.time()})
    finally:
        try:
            _reply_subscribers.remove(queue)
        except ValueError:
            pass
        logger.info("[SSE] reply stream unsubscribed (total=%d)", len(_reply_subscribers))


async def _event_generator(hunt_id: str, queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Generate SSE events for a hunt's progress.

    Listens on a per-subscriber asyncio.Queue that receives events
    broadcast by _run_hunt in routes.py.
    """
    try:
        # Send current state as initial heartbeat
        hunt = _hunts[hunt_id]
        yield _sse_event("heartbeat", {
            "status": hunt["status"],
            "current_stage": hunt.get("current_stage"),
            "hunt_round": hunt.get("hunt_round", 0),
            "leads_count": hunt.get("leads_count", 0),
            "email_sequences_count": hunt.get("email_sequences_count", 0),
        })

        # Replay all completed stage snapshots so late-joining clients get history
        for snapshot in hunt.get("stage_snapshots", {}).values():
            yield _sse_event("stage_data", snapshot)

        # If already completed/failed, send final event and close
        if hunt["status"] == "completed":
            result = hunt.get("result") or {}
            yield _sse_event("completed", {
                "leads_count": len(result.get("leads", [])) if isinstance(result, dict) else 0,
                "email_sequences_count": len(result.get("email_sequences", [])) if isinstance(result, dict) else 0,
                "hunt_round": result.get("hunt_round", 0) if isinstance(result, dict) else 0,
            })
            return
        if hunt["status"] == "failed":
            yield _sse_event("failed", {"error": hunt.get("error", "Unknown error")})
            return

        # Listen for broadcast events
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield _sse_event(event, data)
                if event in ("completed", "failed"):
                    return
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                h = _hunts.get(hunt_id, {})
                yield _sse_event("heartbeat", {
                    "status": h.get("status", "unknown"),
                    "current_stage": h.get("current_stage"),
                    "hunt_round": h.get("hunt_round", 0),
                    "leads_count": h.get("leads_count", 0),
                    "email_sequences_count": h.get("email_sequences_count", 0),
                })
                # Check if hunt ended while we were waiting
                if h.get("status") in ("completed", "failed"):
                    return
    finally:
        # Unregister subscriber
        if hunt_id in _sse_queues:
            try:
                _sse_queues[hunt_id].remove(queue)
            except ValueError:
                pass
            if not _sse_queues[hunt_id]:
                del _sse_queues[hunt_id]


async def _automation_job_event_generator(job_id: str) -> AsyncGenerator[str, None]:
    queue = _automation_job_queue()
    previous_snapshot = ""
    while True:
        job = queue.get(job_id)
        if not job:
            yield _sse_event("failed", {"error": "Automation job not found"})
            return

        payload = _serialize_job(job)
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if snapshot != previous_snapshot:
            event_type = "update"
            if previous_snapshot == "":
                event_type = "heartbeat"
            elif payload["status"] == "completed":
                event_type = "completed"
            elif payload["status"] == "failed":
                event_type = "failed"
            yield _sse_event(event_type, payload)
            previous_snapshot = snapshot

        if payload["status"] in {"completed", "failed"}:
            return
        await asyncio.sleep(1.0)


@sse_router.get("/hunts/{hunt_id}/stream", dependencies=[Depends(require_api_access)])
async def stream_hunt(hunt_id: str):
    """Stream real-time hunt progress via SSE.

    Event types:
    - stage_change: Pipeline stage changed (insight, keyword_gen, search, etc.)
    - round_change: New hunting round started
    - progress: Lead count updated
    - lead_progress: Per-URL extraction progress (scraping, lead_found, scrape_done, etc.)
    - completed: Hunt finished successfully
    - failed: Hunt failed with error
    - heartbeat: Keep-alive ping
    """
    if hunt_id not in _hunts:
        raise HTTPException(status_code=404, detail="Hunt not found")

    # Create a per-subscriber queue
    queue: asyncio.Queue = asyncio.Queue()
    if hunt_id not in _sse_queues:
        _sse_queues[hunt_id] = []
    _sse_queues[hunt_id].append(queue)

    return StreamingResponse(
        _event_generator(hunt_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@sse_router.get("/automation/jobs/{job_id}/stream", dependencies=[Depends(require_api_access)])
async def stream_automation_job(job_id: str):
    queue = _automation_job_queue()
    if not queue.get(job_id):
        raise HTTPException(status_code=404, detail="Automation job not found")

    return StreamingResponse(
        _automation_job_event_generator(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@sse_router.get("/replies/stream", dependencies=[Depends(require_api_access)])
async def stream_replies():
    """Push a `reply` event to the browser for every newly matched reply.

    Authenticated via the existing session cookie + CSRF double-submit
    pipeline. EventSource doesn't allow custom headers, but the
    browser sends the session cookie automatically, so this Just
    Works as long as the user is logged in.

    Events:
    - `reply`:     new reply matched; `data` is a NotificationItem
    - `heartbeat`: keep-alive every 30s
    """
    return StreamingResponse(
        _reply_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
