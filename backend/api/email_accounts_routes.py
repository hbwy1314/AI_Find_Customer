"""CRUD routes for `email_accounts` (multi-account per user).

All accounts use Microsoft Graph for both send and receive. The
SMTP/IMAP fields are gone from the public API. Existing DB rows
that still have ``provider_type='smtp'`` are coerced to ``'graph'``
at read time so no migration is required.

Endpoints:
  GET    /api/v1/email-accounts               list (secrets hidden)
  POST   /api/v1/email-accounts               create Graph account
  PATCH  /api/v1/email-accounts/{id}          update fields (secrets encrypted)
  DELETE /api/v1/email-accounts/{id}          delete (only if no campaign ref)
  POST   /api/v1/email-accounts/{id}/test    test Graph connection
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, Field

from auth import secrets as secret_cipher
from auth.security import require_api_access, require_user
from config.settings import get_settings
from emailing.store import get_email_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    provider_type: str = Field("graph", pattern="^graph$")
    from_name: str = ""
    from_email: str = ""
    reply_to: str = ""
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""  # write-only; never returned
    graph_user_principal_name: str = ""
    daily_send_limit: int = 20
    hourly_send_limit: int = 10
    status: str = "active"


class AccountUpdate(BaseModel):
    from_name: Optional[str] = None
    from_email: Optional[str] = None
    reply_to: Optional[str] = None
    graph_tenant_id: Optional[str] = None
    graph_client_id: Optional[str] = None
    graph_client_secret: Optional[str] = None
    graph_user_principal_name: Optional[str] = None
    daily_send_limit: Optional[int] = None
    hourly_send_limit: Optional[int] = None
    status: Optional[str] = None


class ReorderRequest(BaseModel):
    account_ids: list[str] = Field(..., min_length=1, max_length=500)


class TestRequest(BaseModel):
    kind: str = Field("graph", pattern="^graph$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_graph_tested() -> None:
    """Persist GRAPH_LAST_TEST_AT after a successful Graph connectivity test.

    Mirrors what the settings-page graph-test endpoint records, so the
    campaign-start / scheduler / reply-detection "verified connection"
    gates accept a test performed from this page too. Best-effort: a
    persistence hiccup must not fail the test response itself.
    """
    try:
        import os

        from config.settings_store import update_settings

        tested_at = _now_iso()
        update_settings({"GRAPH_LAST_TEST_AT": tested_at})
        os.environ["GRAPH_LAST_TEST_AT"] = tested_at
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record GRAPH_LAST_TEST_AT after successful test")


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) < 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _encrypt_graph_secret(secret: str) -> str:
    """Encrypt a per-account Graph client secret for storage.

    Returns an empty string for an empty input so the column stays
    consistent with rows that don't override the global env-var
    secret. The encrypted blob is the same shape used elsewhere
    (base64-wrapped Fernet token).
    """
    if not secret:
        return ""
    return str(secret_cipher.encrypt_dict({"graph_secret": secret}).decode("utf-8", errors="replace"))


def _strip_blob(account: dict[str, Any]) -> dict[str, Any]:
    """Return the account as JSON-safe, secrets scrubbed."""
    out = dict(account)
    out.pop("secrets_ciphertext", None)
    # The legacy SMTP/IMAP secret fields are still encrypted in some rows
    # for historical reasons; surface a "has_*" flag so the UI can warn
    # the user to rotate them. They are no longer used to send mail.
    secrets = secret_cipher.decrypt_dict(account.get("secrets_ciphertext"))
    out["has_smtp_password"] = bool(secrets.get("smtp_secret"))
    out["has_imap_password"] = bool(secrets.get("imap_secret"))
    out["smtp_password_masked"] = _mask_secret(secrets.get("smtp_secret", "")) if secrets.get("smtp_secret") else ""
    out["imap_password_masked"] = _mask_secret(secrets.get("imap_secret", "")) if secrets.get("imap_secret") else ""
    out["has_graph_secret"] = bool(account.get("graph_client_secret_encrypted"))
    return out


def _account_to_response(account: dict[str, Any]) -> dict[str, Any]:
    out = _strip_blob(account)
    out["daily_send_limit"] = int(account.get("daily_send_limit", 0) or 0)
    return out


def _with_sent_today(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Augment the list response with `sent_today` per account.

    Computed against the same UTC midnight boundary the scheduler uses.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    store = get_email_store()
    enriched: list[dict[str, Any]] = []
    for acc in accounts:
        resp = _account_to_response(acc)
        try:
            resp["sent_today"] = store.count_sent_today_for_account(
                str(acc.get("id", "") or ""), now_iso=now.isoformat()
            )
        except Exception:  # noqa: BLE001
            resp["sent_today"] = 0
        enriched.append(resp)
    return enriched


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
def list_accounts(request: Request) -> dict:
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    raw = store.list_accounts()
    accounts = _with_sent_today(raw)
    return {"accounts": accounts, "count": len(accounts)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(
    request: Request,
    payload: AccountCreate = Body(...),
) -> dict:
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    account_id = f"acct_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    # Graph client secret, if provided, is encrypted and stored on the
    # row's `graph_client_secret_encrypted` column (same column the
    # global `GRAPH_CLIENT_SECRET` env var is keyed against).
    graph_secret = payload.graph_client_secret.strip() if payload.graph_client_secret else ""
    row = {
        "id": account_id,
        "provider_type": "graph",
        "from_name": payload.from_name or "",
        "from_email": payload.from_email or "",
        "reply_to": payload.reply_to or "",
        # Legacy SMTP/IMAP columns are kept on the row with empty
        # values so the schema doesn't have to migrate.
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_username": "",
        "smtp_secret_encrypted": "",
        "imap_host": "",
        "imap_port": 993,
        "imap_username": "",
        "imap_secret_encrypted": "",
        "use_tls": 1,
        "status": payload.status or "active",
        "daily_send_limit": payload.daily_send_limit,
        "hourly_send_limit": payload.hourly_send_limit,
        "last_test_at": "",
        "created_at": now,
        "updated_at": now,
        "secrets_ciphertext": b"",
        "graph_tenant_id": payload.graph_tenant_id or "",
        "graph_user_principal_name": payload.graph_user_principal_name or "",
        "graph_client_secret_encrypted": _encrypt_graph_secret(graph_secret),
        # Append new accounts to the end of the rotation. The quotas page
        # lets the user drag them up later via /reorder.
        "sort_order": store.next_sort_order(),
    }
    store.upsert_account(row)
    return _account_to_response(store.get_account(account_id) or row)


@router.post("/reorder")
def reorder_accounts(
    payload: ReorderRequest,
    request: Request,
) -> dict:
    """Persist a new manual rotation order for the user's email accounts.

    The i-th id in `account_ids` gets `sort_order = i`. Any existing account
    that the caller omits is left at its current sort_order (and will end
    up at the tail of the rotation, after the listed ones). The frontend
    is expected to send the full visible list to keep the table stable.

    Tolerance policy:
    - Duplicate ids in the list are silently de-duped (the store layer
      also does this, so this is just defense in depth).
    - Unknown ids (e.g. an account that was deleted in another tab
      between the user's drag and this request landing) are silently
      dropped, and the known ids are still reordered. This is much
      friendlier than a 404 that wipes out the user's manual order.
    """
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    existing_ids = {str(a.get("id", "")) for a in store.list_accounts()}
    requested: list[str] = []
    seen: set[str] = set()
    for raw in payload.account_ids:
        aid = str(raw or "").strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        requested.append(aid)
    # Drop ids the server doesn't know about. We still proceed with the
    # rest so a single stale id from a parallel tab doesn't kill the
    # whole reorder.
    known = [aid for aid in requested if aid in existing_ids]
    if not known:
        # Nothing to do, but it's not an error — just a no-op success.
        # (Returning 4xx here would make the UI throw "保存顺序失败" on
        # every harmless race.)
        raw = store.list_accounts()
        return {"ok": True, "count": len(raw), "accounts": _with_sent_today(raw), "ignored_unknown": requested}
    store.reorder_accounts(known)
    raw = store.list_accounts()
    accounts = _with_sent_today(raw)
    return {"ok": True, "count": len(accounts), "accounts": accounts}


@router.patch("/{account_id}")
def update_account(
    account_id: str,
    request: Request,
    patch: AccountUpdate = Body(...),
) -> dict:
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    existing = store.get_account(account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Email account not found")
    updates: dict[str, Any] = {}
    for field in (
        "from_name",
        "from_email",
        "reply_to",
        "graph_tenant_id",
        "graph_user_principal_name",
        "daily_send_limit",
        "hourly_send_limit",
        "status",
    ):
        value = getattr(patch, field)
        if value is not None:
            updates[field] = value
    if updates:
        updates["updated_at"] = _now_iso()
        existing.update(updates)
        store.upsert_account(existing)
    # Per-account Graph client secret is encrypted separately. An
    # empty string clears the override and falls back to the global
    # GRAPH_CLIENT_SECRET env var.
    if patch.graph_client_secret is not None:
        encrypted = _encrypt_graph_secret(patch.graph_client_secret.strip())
        store.upsert_account({**existing, "graph_client_secret_encrypted": encrypted, "updated_at": _now_iso()})
        existing["graph_client_secret_encrypted"] = encrypted
    return _account_to_response(store.get_account(account_id) or existing)


@router.delete("/{account_id}")
def delete_account(account_id: str, request: Request) -> dict:
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    existing = store.get_account(account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Email account not found")
    # Cascade policy: if every campaign referencing this account is empty
    # (no sequences and no messages), they are orphaned test data and can
    # be deleted along with the account. Non-empty campaigns still block
    # deletion so we never drop real leads/sends.
    with store._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            """
            SELECT c.id,
                   (SELECT COUNT(*) FROM lead_email_sequences s WHERE s.campaign_id = c.id) AS seq_count
            FROM email_campaigns c
            WHERE c.email_account_id = ?
            """,
            (account_id,),
        ).fetchall()
    non_empty = [cid for (cid, seq_count) in rows if int(seq_count or 0) > 0]
    if non_empty:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Account is used by {len(non_empty)} non-empty campaign(s); "
                "delete or reassign them first."
            ),
        )
    orphaned_campaigns = [cid for (cid, _) in rows]
    with store._connect() as conn:
        for cid in orphaned_campaigns:
            conn.execute("DELETE FROM email_campaigns WHERE id = ?", (cid,))
        conn.execute("DELETE FROM email_accounts WHERE id = ?", (account_id,))
        conn.commit()
    return {
        "ok": True,
        "id": account_id,
        "cascade_campaigns_deleted": len(orphaned_campaigns),
    }


@router.post("/{account_id}/test")
async def test_account(
    account_id: str,
    request: Request,
    payload: TestRequest = Body(...),
) -> dict:
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    account = store.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Email account not found")
    # The legacy ``smtp`` / ``imap`` test kinds are no longer accepted —
    # the TestRequest schema already restricts ``kind`` to ``"graph"``,
    # so anything else is a 422 from pydantic before we get here.
    result = await _test_graph(account)
    if result.get("ok"):
        # Graph credentials are tenant-global, so a successful account
        # test also satisfies the "verify before auto send" gate that
        # the settings-page Graph test records. Without this, users
        # who set up Graph here would be bounced to the settings page
        # for a second, redundant connectivity test.
        _record_graph_tested()
    return result


# ---------------------------------------------------------------------------
# Send / Receive test (round-trip a real email + read the inbox back)
# ---------------------------------------------------------------------------

class TestSendRequest(BaseModel):
    to_email: str = Field(..., min_length=3, max_length=255)
    subject: str = ""
    body: str = ""


@router.post("/{account_id}/test-send")
async def test_send_email(
    account_id: str,
    payload: TestSendRequest,
    request: Request,
) -> dict:
    """Send a real test email through the configured provider, return send result.

    Test sends count against the account's `daily_send_limit` (and
    show up in the quotas page "今日 / 上限" bar) so a flood of
    test-sends from the operator doesn't burn the real campaign
    budget on a shared mailbox. The pre-send check refuses with 429
    if the account is already at its daily cap; on success the send
    is recorded in `email_test_send_log` via `store.record_test_send`.
    """
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    account = store.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Email account not found")
    from emailing import email_sender

    # Pre-send quota guard. We deliberately don't enforce `hourly_send_limit`
    # here — the scheduler itself doesn't track that today and we want the
    # operator's smoke tests to be predictable. Just block at the daily cap
    # so the user gets a clear "you've used today's budget" error instead
    # of the mailbox bouncing mid-test.
    daily_limit = int(account.get("daily_send_limit", 0) or 0)
    if daily_limit > 0:
        now_iso = datetime.now(timezone.utc).isoformat()
        used_today = store.count_sent_today_for_account(account_id, now_iso=now_iso)
        if used_today >= daily_limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "daily_limit_reached",
                    "account_id": account_id,
                    "daily_send_limit": daily_limit,
                    "sent_today": used_today,
                    "message": (
                        f"今日已发送 {used_today} 封，已达上限 {daily_limit}。"
                        "测试发送同样计入限额，可明天 00:00 UTC 后再试，"
                        "或到「发送限额」调高每日上限。"
                    ),
                },
            )

    subject = (payload.subject or "").strip() or f"[测试邮件] from {account.get('from_email')}"
    body = (payload.body or "").strip() or (
        "这是一封测试邮件。\n\n"
        f"账号: {account.get('from_email')}\n"
        f"时间: {datetime.now(timezone.utc).isoformat()}\n\n"
        "如果收到说明 Microsoft Graph 发送链路工作正常。"
    )
    sent = await email_sender.send_email(
        account,
        to_email=payload.to_email.strip(),
        subject=subject,
        body_text=body,
        reply_to=str(account.get("reply_to") or account.get("from_email") or ""),
        thread_key=f"test-{uuid.uuid4().hex[:12]}",
    )

    # Record the attempt in the test-send log regardless of success.
    # The daily-quota count helper only picks up `ok=1` rows, so failed
    # attempts are visible in audit but don't burn the budget.
    sent_at = str(sent.get("sent_at") or datetime.now(timezone.utc).isoformat())
    store.record_test_send(
        account_id=account_id,
        to_email=payload.to_email.strip(),
        subject=subject,
        body_text=body,
        provider=str(sent.get("provider") or account.get("provider_type") or ""),
        provider_message_id=str(sent.get("provider_message_id") or ""),
        thread_key=str(sent.get("thread_key") or ""),
        ok=bool(sent.get("ok")),
        failure_reason=str(sent.get("error") or "") if not sent.get("ok") else "",
        sent_at=sent_at,
    )

    return {
        "account_id": account_id,
        "to_email": payload.to_email,
        "subject": subject,
        "sent": sent,
    }


@router.get("/{account_id}/test-inbox")
async def test_inbox(
    account_id: str,
    request: Request,
    recent_minutes: int = 10,
    limit: int = 10,
) -> dict:
    """Fetch the most recent messages from this account's inbox via Microsoft Graph.

    Polls Graph for the latest N messages received in the last `recent_minutes`.
    """
    require_api_access(request)
    require_user(request)
    store = get_email_store()
    account = store.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Email account not found")
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, min(recent_minutes, 1440)))
    since_iso = cutoff.isoformat()
    from emailing import graph_client
    # Graph fetcher exposes (raw_ref, message_id, from_email, from_name, subject,
    # in_reply_to, references, received_at, snippet, headers, conversation_id)
    inbound = await graph_client.fetch_graph_replies(
        None, now_iso=since_iso, recent_days=max(1, recent_minutes // (60 * 24) + 1), limit=limit
    )
    items = [
        {
            "id": m.get("message_id") or m.get("raw_ref"),
            "from_email": m.get("from_email"),
            "from_name": m.get("from_name"),
            "subject": m.get("subject"),
            "received_at": m.get("received_at"),
            "snippet": m.get("snippet"),
            "conversation_id": m.get("conversation_id"),
        }
        for m in (inbound or [])[:limit]
    ]
    return {"account_id": account_id, "provider": "graph", "since": since_iso, "items": items}


async def _test_graph(account: dict[str, Any]) -> dict:
    from emailing import graph_client
    return await graph_client.test_graph_connection()


# ---------------------------------------------------------------------------
# Sync from Azure AD (Application permission, no per-user OAuth needed)
# ---------------------------------------------------------------------------

class GraphBulkAddRequest(BaseModel):
    """Body for `POST /email-accounts/graph/bulk-add`."""
    emails: list[str] = Field(default_factory=list, max_length=500)
    default_name: str = ""  # optional name prefix for created accounts


@router.get("/graph/users")
async def list_graph_users(request: Request) -> dict:
    """List Azure AD users in the configured tenant (Application permission).

    Requires `User.Read.All` (or `User.ReadBasic.All`) granted + admin-consented
    to the Azure AD App.
    """
    require_api_access(request)
    from emailing import graph_client
    result = await graph_client.list_tenant_users(limit=200)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400 if result.get("error_type") == "auth_error" else 502,
            detail=result.get("error") or "graph_users_failed",
        )
    return {"users": result.get("users", []), "count": result.get("count", 0)}


@router.post("/graph/bulk-add")
def bulk_add_graph_accounts(
    payload: GraphBulkAddRequest,
    request: Request,
) -> dict:
    """Create one `email_accounts` row per email, all as `provider_type=graph`.

    Existing rows with the same `from_email` are left untouched (status=exists).
    """
    require_api_access(request)
    store = get_email_store()
    settings = get_settings()
    tenant = settings.graph_tenant_id.strip()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    existing_emails = {
        (a.get("from_email") or "").strip().lower()
        for a in store.list_accounts()
        if a.get("from_email")
    }
    for raw_email in payload.emails:
        email = (raw_email or "").strip().lower()
        if not email or "@" not in email:
            skipped.append({"email": raw_email, "status": "invalid"})
            continue
        if email in existing_emails:
            skipped.append({"email": email, "status": "exists"})
            continue
        account_id = f"acct_{uuid.uuid4().hex[:16]}"
        now = _now_iso()
        # Default from_name: UPN local part, or the override passed in
        from_name = payload.default_name.strip() if payload.default_name else email.split("@", 1)[0]
        row = {
            "id": account_id,
            "provider_type": "graph",
            "from_name": from_name,
            "from_email": email,
            "reply_to": email,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_secret_encrypted": "",
            "imap_host": "",
            "imap_port": 993,
            "imap_username": "",
            "imap_secret_encrypted": "",
            "use_tls": 1,
            "status": "active",
            "daily_send_limit": 50,
            "hourly_send_limit": 10,
            "last_test_at": "",
            "created_at": now,
            "updated_at": now,
            "secrets_ciphertext": b"",
            "graph_tenant_id": tenant,
            "graph_user_principal_name": email,
        }
        store.upsert_account(row)
        created.append({"email": email, "status": "created", "id": account_id})
        existing_emails.add(email)
    return {
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }


# ---------------------------------------------------------------------------
# Graph configuration snapshot (for settings UI)
# ---------------------------------------------------------------------------

@router.get("/graph/config")
def graph_config(request: Request) -> dict:
    require_api_access(request)
    require_user(request)
    from emailing import graph_client
    return graph_client.graph_config_status()
