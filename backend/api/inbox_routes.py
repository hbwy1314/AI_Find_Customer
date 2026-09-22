"""Unified Graph Inbox and read-state endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth.security import require_api_access, require_user
from config.settings import get_settings
from emailing import graph_client
from emailing.reply_detector import run_graph_reply_detection_once
from emailing.store import EmailStore, get_email_store

router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])


class ReadStateRequest(BaseModel):
    is_read: bool = True
    sync_to_graph: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_filter(request: Request) -> tuple[int, bool]:
    user = require_user(request)
    return user.user_id, user.via == "session" and user.role not in {"admin", "dev"}


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    import json

    result = dict(row)
    for field in ("references_json", "to_recipients_json", "cc_recipients_json", "raw_headers_json"):
        raw = result.pop(field, "")
        try:
            result[field.removesuffix("_json")] = json.loads(raw or ("{}" if field == "raw_headers_json" else "[]"))
        except (TypeError, ValueError):
            result[field.removesuffix("_json")] = {} if field == "raw_headers_json" else []
    result["graph_is_read"] = bool(result.get("graph_is_read"))
    result["site_is_read"] = bool(result.get("site_is_read"))
    result["is_auto_reply"] = bool(result.get("is_auto_reply"))
    result["is_ignored"] = bool(result.get("is_ignored"))
    result["has_attachments"] = bool(result.get("has_attachments"))
    return result


@router.get("/messages", dependencies=[Depends(require_api_access)])
async def list_inbox_messages(
    request: Request,
    unread_only: bool = False,
    match_status: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    owner_id, enforce_owner = _owner_filter(request)
    store = get_email_store()
    rows = store.list_inbound_messages(
        owner_user_id=owner_id,
        enforce_owner=enforce_owner,
        unread_only=unread_only,
        match_status=match_status.strip(),
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_serialize(row) for row in rows],
        "unread_count": store.count_inbound_unread(owner_user_id=owner_id, enforce_owner=enforce_owner),
        "shared_inbox_upn": graph_client.account_upn(None),
        "compat_scan_enabled": bool(getattr(get_settings(), "email_inbox_compat_scan_enabled", True)),
    }


@router.get("/unread-count", dependencies=[Depends(require_api_access)])
async def inbox_unread_count(request: Request) -> dict[str, Any]:
    owner_id, enforce_owner = _owner_filter(request)
    store = get_email_store()
    return {
        "unread_count": store.count_inbound_unread(owner_user_id=owner_id, enforce_owner=enforce_owner),
        "shared_inbox_upn": graph_client.account_upn(None),
    }


@router.get("/messages/{message_id}", dependencies=[Depends(require_api_access)])
async def get_inbox_message(message_id: str, request: Request) -> dict[str, Any]:
    owner_id, enforce_owner = _owner_filter(request)
    store = get_email_store()
    row = store.get_inbound_message(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    if enforce_owner and int(row.get("owner_user_id", 0) or 0) not in {0, owner_id}:
        raise HTTPException(status_code=403, detail="Inbox message access denied")
    return {"message": _serialize(row)}


@router.patch("/messages/{message_id}/read", dependencies=[Depends(require_api_access)])
async def mark_inbox_message_read(
    message_id: str,
    payload: ReadStateRequest,
    request: Request,
) -> dict[str, Any]:
    owner_id, enforce_owner = _owner_filter(request)
    store = get_email_store()
    row = store.get_inbound_message(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    if enforce_owner and int(row.get("owner_user_id", 0) or 0) not in {0, owner_id}:
        raise HTTPException(status_code=403, detail="Inbox message access denied")

    now = _now_iso()
    graph_result: dict[str, Any] | None = None
    if payload.sync_to_graph:
        upn = str(row.get("mailbox_upn", "") or "").strip()
        graph_id = str(row.get("graph_message_id", "") or "").strip()
        if not upn or not graph_id:
            raise HTTPException(status_code=409, detail="该邮件没有可同步到 Graph 的邮箱或消息 ID")
        status_code, body = await graph_client._graph_request(
            "PATCH",
            f"/users/{upn}/messages/{graph_id}",
            json_body={"isRead": payload.is_read},
            timeout=30.0,
        )
        if status_code not in (200, 202, 204):
            raise HTTPException(status_code=502, detail={"error": "graph_read_state_update_failed", "status": status_code, "body": body})
        graph_result = {"updated": True, "is_read": payload.is_read}
        store.update_inbound_message(
            message_id,
            now_iso=now,
            graph_is_read=1 if payload.is_read else 0,
            graph_read_at=now if payload.is_read else "",
        )

    if not store.mark_inbound_site_read(message_id, is_read=payload.is_read, now_iso=now):
        raise HTTPException(status_code=404, detail="Inbox message not found")
    updated = store.get_inbound_message(message_id)
    return {"message": _serialize(updated or {}), "graph": graph_result}


@router.post("/sync", dependencies=[Depends(require_api_access)])
async def sync_inbox(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.role not in {"admin", "dev"}:
        raise HTTPException(status_code=403, detail="Only administrators can sync the shared Inbox")
    settings = get_settings()
    if not bool(getattr(settings, "email_inbox_sync_enabled", True)):
        raise HTTPException(status_code=409, detail="收件同步已在设置中关闭")
    store: EmailStore = get_email_store()
    accounts = graph_client.distinct_poll_accounts(
        store.list_accounts_by_provider("graph"),
        compat_scan=bool(getattr(settings, "email_inbox_compat_scan_enabled", True)),
    )
    result = {"checked": 0, "matched": 0, "skipped": 0, "ignored": 0, "matches": [], "mailboxes": []}
    for account in accounts:
        part = await run_graph_reply_detection_once(store, account, match_replies=True)
        for key in ("checked", "matched", "skipped", "ignored"):
            result[key] += int(part.get(key, 0) or 0)
        result["matches"].extend(part.get("matches", []) or [])
        result["mailboxes"].append(graph_client.account_upn(account))
    result["unread_count"] = store.count_inbound_unread(owner_user_id=user.user_id, enforce_owner=False)
    return result
