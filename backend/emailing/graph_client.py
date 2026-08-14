"""Microsoft Graph email client (Application permission / client_credentials).

Used in place of SMTP/IMAP when `Settings.email_provider_type == "graph"`.

Application-permission design:
  - One shared mailbox (`GRAPH_MAILBOX_UPN`, e.g. sales@company.com) is the
    sender of every message and the receiver whose inbox is polled for replies.
  - Admin must grant `Mail.ReadWrite` and `Mail.Send` once at the tenant level
    via the admin-consent URL printed in the Settings page.
  - Tokens are obtained via `client_credentials` and cached in memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from msal import ConfidentialClientApplication

from config.settings import get_settings

logger = logging.getLogger(__name__)


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Application-token cache (singleton per process)
# ---------------------------------------------------------------------------

class _AppTokenCache:
    def __init__(self) -> None:
        self._msal_app: ConfidentialClientApplication | None = None
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _build_app(self) -> ConfidentialClientApplication | None:
        settings = get_settings()
        tenant = settings.graph_tenant_id.strip()
        client_id = settings.graph_client_id.strip()
        secret = settings.graph_client_secret.strip()
        if not (tenant and client_id and secret):
            return None
        authority = f"https://login.microsoftonline.com/{tenant}"
        return ConfidentialClientApplication(
            client_id=client_id,
            client_credential=secret,
            authority=authority,
        )

    async def get_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self._access_token and now < self._expires_at - 300:  # 5 min early refresh
                return self._access_token
            app = self._build_app()
            if app is None:
                raise RuntimeError(
                    "Microsoft Graph is not configured: GRAPH_TENANT_ID / GRAPH_CLIENT_ID / "
                    "GRAPH_CLIENT_SECRET must all be set."
                )
            settings = get_settings()
            scopes = [s.strip() for s in (settings.graph_default_scopes or "").split() if s.strip()]
            if not scopes:
                scopes = ["https://graph.microsoft.com/.default"]
            result = await asyncio.to_thread(
                app.acquire_token_for_client, scopes=scopes
            )
            if "access_token" not in result:
                err = result.get("error") or "unknown"
                desc = result.get("error_description") or ""
                raise RuntimeError(f"Failed to acquire Graph app token: {err} {desc}")
            self._access_token = str(result["access_token"])
            self._expires_at = now + int(result.get("expires_in", 3600))
            return self._access_token

    def reset(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0
        self._msal_app = None


_token_cache = _AppTokenCache()


def reset_graph_token_cache() -> None:
    """Drop the in-memory app token (call when credentials change)."""
    _token_cache.reset()


def _mailbox_upn() -> str:
    return get_settings().graph_mailbox_upn.strip()


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

async def _graph_request(method: str, url: str, *, json_body: dict | None = None,
                         headers: dict[str, str] | None = None,
                         timeout: float = 30.0) -> tuple[int, dict | str]:
    token = await _token_cache.get_token()
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, f"{_GRAPH_BASE}{url}", headers=h, json=json_body)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = resp.text
    return resp.status_code, body


# ---------------------------------------------------------------------------
# Send (replaces SMTP for `provider_type == "graph"`)
# ---------------------------------------------------------------------------

async def send_via_graph(
    account: dict[str, Any],
    *,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: str | None = None,
    thread_key: str | None = None,
) -> dict[str, Any]:
    """Send `body_text` to `to_email` using the shared Graph mailbox.

    Returns the SAME 7-key dict shape as `_send_via_smtp_sync` so the
    scheduler/reply_detector pipeline is provider-agnostic.

    Implementation note — two-step send with a sendMail fallback:

        Primary:  `POST /users/{upn}/messages` (create draft) →
                  `POST /users/{upn}/messages/{id}/send` (actually send)
        Fallback: `POST /users/{upn}/sendMail` (single-shot, no id back)

    We need the create step because Graph will not let us pin a custom
    `Message-ID` via `internetMessageHeaders` (Message-ID is a reserved
    trace header — Graph rejects it with `InvalidInternetMessageHeader`).
    The create response hands us the real `internetMessageId` (RFC822
    Message-ID) and `conversationId`, which are exactly what
    `reply_detector` already keys on via `provider_message_id` /
    `thread_key`. So the create step is effectively free — the send
    step is the same sendMail under the hood — and we get a real id
    back instead of one that the receiving MTA would reject.

    Why we DON'T set `from` in the draft payload: with Application
    permissions, Graph forces the sender to be the configured shared
    mailbox. If we set `from` to the per-account `from_email` (which
    might be a friendly alias or a different address), Graph silently
    uses our value to build the auto-generated `Message-ID` but then
    fails validation on the `/send` step with
    `InvalidInternetMessageHeader` because the Message-ID's domain
    doesn't match the actual sender mailbox. The safe move is to let
    Graph assign `from` itself.
    """
    if not to_email.strip():
        return _err(account, "missing_recipient", "invalid_recipient", thread_key=thread_key, subject=subject)

    upn = _mailbox_upn()
    if not upn:
        return _err(account, "graph_not_configured", "auth_error", thread_key=thread_key, subject=subject)

    # ── Primary: two-step create + send ───────────────────────────
    two_step_result = await _send_two_step(
        account, upn=upn, to_email=to_email, subject=subject,
        body_text=body_text, reply_to=reply_to, thread_key=thread_key,
    )
    if two_step_result.get("ok"):
        return two_step_result
    # Only fall back to single-shot sendMail for send-side 4xx/5xx
    # errors. Auth failures, missing config, and network errors should
    # surface immediately — retrying them with sendMail won't help and
    # would just add latency + a misleading second error.
    err_type = two_step_result.get("error_type", "")
    err = two_step_result.get("error", "")
    if err_type in {"auth_error", "network_error"} or "missing_recipient" in err:
        return two_step_result
    if "graph_create_failed" in err:
        # Create itself failed — sendMail with the same payload is just
        # going to fail the same way, so don't waste a round trip.
        return two_step_result

    logger.warning(
        "Graph two-step send failed (%s); falling back to single-shot sendMail",
        err,
    )
    fallback = await _send_single_step(
        account, upn=upn, to_email=to_email, subject=subject,
        body_text=body_text, reply_to=reply_to, thread_key=thread_key,
    )
    if fallback.get("ok"):
        return fallback
    # Both paths failed — surface the original two-step error (more
    # context) and stash the fallback failure in the result for logs.
    combined = dict(two_step_result)
    fb_err = str(fallback.get("error") or "")
    if fb_err:
        combined["error"] = f"{combined.get('error', '')} | fallback:{fb_err}"
    return combined


async def _send_two_step(
    account: dict[str, Any],
    *,
    upn: str,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: str | None,
    thread_key: str | None,
) -> dict[str, Any]:
    try:
        # ── Step 1: create the draft message ──────────────────────
        # Note: deliberately OMIT `from` so Graph assigns the shared
        # mailbox as the sender. See the docstring above for why.
        draft_payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text or ""},
            "toRecipients": [{"emailAddress": {"name": "", "address": to_email}}],
        }
        if reply_to:
            draft_payload["replyTo"] = [{"emailAddress": {"name": "", "address": reply_to}}]
        create_status, create_body = await _graph_request(
            "POST",
            f"/users/{upn}/messages",
            json_body=draft_payload,
            timeout=30.0,
        )
        if create_status not in (200, 201) or not isinstance(create_body, dict):
            err_code = ""
            if isinstance(create_body, dict):
                err_code = (create_body.get("error") or {}).get("code", "") if isinstance(create_body.get("error"), dict) else str(create_body.get("error", ""))
            err_type = "auth_error" if create_status in (401, 403) else "permanent_failure"
            logger.warning(
                "Graph create draft failed status=%s code=%s body=%s",
                create_status, err_code, _short_body(create_body),
            )
            return _err(
                account,
                f"graph_create_failed:{create_status}:{err_code}",
                err_type,
                thread_key=thread_key,
                subject=subject,
                extra=create_body if isinstance(create_body, (dict, str)) else str(create_body),
            )

        graph_message_id = str(create_body.get("id") or "")
        internet_message_id = str(create_body.get("internetMessageId") or "")
        conversation_id = str(create_body.get("conversationId") or "")
        if not graph_message_id:
            return _err(
                account,
                "graph_create_no_id",
                "permanent_failure",
                thread_key=thread_key,
                subject=subject,
                extra=create_body if isinstance(create_body, (dict, str)) else str(create_body),
            )

        # ── Step 2: actually send the draft ───────────────────────
        send_status, send_body = await _graph_request(
            "POST",
            f"/users/{upn}/messages/{graph_message_id}/send",
            timeout=30.0,
        )
        if send_status in (202, 200):
            return {
                "ok": True,
                "provider": "graph",
                "provider_message_id": internet_message_id or graph_message_id,
                "thread_key": thread_key or conversation_id or internet_message_id or graph_message_id,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "error": "",
                "error_type": "",
            }
        # Send failed — best-effort delete the draft so it doesn't sit
        # in the user's Drafts folder. Ignore errors here; the real
        # error gets surfaced below.
        try:
            await _graph_request(
                "DELETE",
                f"/users/{upn}/messages/{graph_message_id}",
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001
            pass
        err_code = ""
        if isinstance(send_body, dict):
            err_code = (send_body.get("error") or {}).get("code", "") if isinstance(send_body.get("error"), dict) else str(send_body.get("error", ""))
        err_type = "auth_error" if send_status in (401, 403) else "permanent_failure"
        logger.warning(
            "Graph send draft failed status=%s code=%s body=%s",
            send_status, err_code, _short_body(send_body),
        )
        return _err(
            account,
            f"graph_send_failed:{send_status}:{err_code}",
            err_type,
            thread_key=thread_key,
            subject=subject,
            extra=send_body if isinstance(send_body, (dict, str)) else str(send_body),
        )
    except RuntimeError as exc:
        return _err(account, f"graph_token:{exc}", "auth_error", thread_key=thread_key, subject=subject)
    except (httpx.HTTPError, TimeoutError) as exc:
        return _err(account, f"graph_network:{exc}", "network_error", thread_key=thread_key, subject=subject)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Graph two-step send unexpected error")
        return _err(account, f"graph_exception:{exc}", "permanent_failure", thread_key=thread_key, subject=subject)


async def _send_single_step(
    account: dict[str, Any],
    *,
    upn: str,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: str | None,
    thread_key: str | None,
) -> dict[str, Any]:
    """Single-shot sendMail — fallback when the two-step create+send fails.

    We don't get an internetMessageId back from sendMail (it returns 202
    with no body), so reply detection has to fall back to
    `from_email + subject` matching in `_match_sent_message`. That's
    already implemented and works fine for this case.
    """
    try:
        from_email = str(account.get("from_email") or upn)
        from_name = str(account.get("from_name") or "Ai Hunter")
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text or ""},
            "toRecipients": [{"emailAddress": {"name": "", "address": to_email}}],
            "from": {"emailAddress": {"name": from_name, "address": from_email}},
        }
        if reply_to:
            message["replyTo"] = [{"emailAddress": {"name": "", "address": reply_to}}]
        payload = {"message": message, "saveToSentItems": True}
        status_code, body = await _graph_request(
            "POST",
            f"/users/{upn}/sendMail",
            json_body=payload,
            timeout=30.0,
        )
        if status_code in (202, 200):
            return {
                "ok": True,
                "provider": "graph",
                # No id back from sendMail; leave empty so the store row
                # is still distinct and reply detection uses the
                # from_email+subject fallback path.
                "provider_message_id": "",
                "thread_key": thread_key or f"graph-sendmail:{upn}:{to_email}:{subject}",
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "error": "",
                "error_type": "",
            }
        err_code = ""
        if isinstance(body, dict):
            err_code = (body.get("error") or {}).get("code", "") if isinstance(body.get("error"), dict) else str(body.get("error", ""))
        err_type = "auth_error" if status_code in (401, 403) else "permanent_failure"
        logger.warning(
            "Graph sendMail fallback failed status=%s code=%s body=%s",
            status_code, err_code, _short_body(body),
        )
        return _err(
            account,
            f"graph_sendmail_failed:{status_code}:{err_code}",
            err_type,
            thread_key=thread_key,
            subject=subject,
            extra=body if isinstance(body, (dict, str)) else str(body),
        )
    except RuntimeError as exc:
        return _err(account, f"graph_token:{exc}", "auth_error", thread_key=thread_key, subject=subject)
    except (httpx.HTTPError, TimeoutError) as exc:
        return _err(account, f"graph_network:{exc}", "network_error", thread_key=thread_key, subject=subject)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Graph sendMail fallback unexpected error")
        return _err(account, f"graph_exception:{exc}", "permanent_failure", thread_key=thread_key, subject=subject)


def _short_body(body: Any, limit: int = 500) -> str:
    """Stringify a Graph response body for logging, with a length cap."""
    try:
        text = body if isinstance(body, str) else str(body)
    except Exception:  # noqa: BLE001
        text = "<unstringifiable>"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _err(
    account: dict[str, Any],
    error: str,
    error_type: str,
    *,
    thread_key: str | None,
    subject: str,
    extra: Any = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": str(account.get("provider_type", "graph") or "graph"),
        "provider_message_id": "",
        "thread_key": thread_key or subject,
        "sent_at": "",
        "error": error,
        "error_type": error_type,
    }


# ---------------------------------------------------------------------------
# Reply fetch (replaces IMAP for `provider_type == "graph"`)
# ---------------------------------------------------------------------------

async def fetch_graph_replies(
    account: dict[str, Any] | None = None,  # kept for parity with imap_client; not used
    *,
    now_iso: str,
    recent_days: int = 14,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a list of reply dicts in the same shape as `fetch_imap_replies()`."""
    upn = _mailbox_upn()
    if not upn:
        return []
    try:
        # Compute SINCE cutoff in ISO 8601 UTC.
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=recent_days)
        cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # NOTE: do NOT include `inReplyToId` here — it doesn't exist on the
        # Graph `Message` resource and causes the whole `$select` to 400
        # with `RequestBroker--ParseUri`. The `In-Reply-To` header lives in
        # `internetMessageHeaders` (a navigation property we can't include
        # in a cheap `$select`); the matching loop falls back to
        # conversationId + from_email + subject, which is enough for our
        # use case.
        select_fields = (
            "internetMessageId,conversationId,from,subject,receivedDateTime,"
            "bodyPreview"
        )
        url = (
            f"/users/{upn}/mailFolders/Inbox/messages"
            f"?$filter=receivedDateTime ge {cutoff_iso}"
            f"&$orderby=receivedDateTime desc"
            f"&$top={limit}"
            f"&$select={select_fields}"
        )
        status_code, body = await _graph_request("GET", url, timeout=30.0)
        if status_code != 200 or not isinstance(body, dict):
            logger.warning("Graph fetch_replies status=%s body=%s", status_code, body)
            return []
        results: list[dict[str, Any]] = []
        for item in body.get("value", []) or []:
            internet_message_id = str(item.get("internetMessageId") or "")
            conversation_id = str(item.get("conversationId") or "")
            sender = (item.get("from") or {}).get("emailAddress") or {}
            from_email = str(sender.get("address") or "")
            from_name = str(sender.get("name") or "")
            subject = str(item.get("subject") or "")
            received_at = str(item.get("receivedDateTime") or "")
            body_preview = str(item.get("bodyPreview") or "")
            # `in_reply_to` is intentionally empty here. To resolve it for
            # stronger matching, callers can hit
            # `/users/{upn}/messages/{id}?$expand=internetMessageHeaders`
            # but that's a per-message round trip — not worth doing in
            # the bulk poll loop. conversationId + subject matching is
            # good enough for the scheduler's "did the lead reply?" check.
            in_reply_to = ""

            raw_ref = f"graph:{conversation_id or internet_message_id or uuid.uuid4()}"
            results.append({
                "raw_ref": raw_ref,
                "message_id": internet_message_id,
                "conversation_id": conversation_id,
                "in_reply_to": in_reply_to,
                "references": [],  # Graph doesn't expose this in the basic select
                "from_email": from_email,
                "from_name": from_name,
                "subject": subject,
                "received_at": received_at,
                "snippet": body_preview[:500],
                "headers": {
                    "From": f"{from_name} <{from_email}>" if from_name else from_email,
                    "Subject": subject,
                    "Message-ID": internet_message_id,
                    "In-Reply-To": in_reply_to,
                },
            })
        return results
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("Graph fetch_replies network error: %s", exc)
        return []
    except Exception:  # noqa: BLE001
        logger.exception("Graph fetch_replies unexpected error")
        return []


# ---------------------------------------------------------------------------
# Connection test (used by /email-accounts/{id}/test)
# ---------------------------------------------------------------------------

async def test_graph_connection() -> dict[str, Any]:
    upn = _mailbox_upn()
    if not upn:
        return {"ok": False, "error": "graph_mailbox_upn_not_set"}
    try:
        status_code, body = await _graph_request("GET", f"/users/{upn}", timeout=15.0)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "error_type": "auth_error"}
    except (httpx.HTTPError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc), "error_type": "network_error"}
    if status_code == 200 and isinstance(body, dict):
        return {
            "ok": True,
            "provider": "graph",
            "mailbox": upn,
            "upn": body.get("userPrincipalName", upn),
            "display_name": body.get("displayName", ""),
        }
    err = ""
    if isinstance(body, dict):
        err = (body.get("error") or {}).get("code", "") if isinstance(body.get("error"), dict) else str(body.get("error", ""))
    return {
        "ok": False,
        "provider": "graph",
        "error": f"graph_test_failed:{status_code}:{err}",
        "error_type": "auth_error" if status_code in (401, 403) else "permanent_failure",
    }


# ---------------------------------------------------------------------------
# Sync from Azure AD
# ---------------------------------------------------------------------------

async def list_tenant_users(*, limit: int = 200) -> dict[str, Any]:
    """List enabled users in the Azure AD tenant via Graph `/users`.

    Requires the Azure AD App to have at least `User.Read.All` (or the lighter
    `User.ReadBasic.All`) Application permission, granted via admin consent.
    """
    if not _mailbox_upn():
        return {"ok": False, "error": "graph_mailbox_upn_not_set", "users": [], "count": 0}
    select_fields = "id,userPrincipalName,displayName,mail,jobTitle,department,accountEnabled"
    url = (
        f"/users?$top={int(limit)}"
        f"&$filter=accountEnabled eq true"
        f"&$select={select_fields}"
    )
    try:
        status_code, body = await _graph_request("GET", url, timeout=30.0)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "error_type": "auth_error", "users": [], "count": 0}
    except (httpx.HTTPError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc), "error_type": "network_error", "users": [], "count": 0}
    if status_code != 200 or not isinstance(body, dict):
        err = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"].get("code", "")
        elif isinstance(body, dict):
            err = str(body.get("error", ""))
        # The most common failure is missing User.Read.All permission.
        if status_code in (401, 403):
            return {
                "ok": False,
                "error": f"graph_users_failed:{status_code}:{err}:missing User.Read.All?",
                "error_type": "auth_error",
                "hint": "在 Azure Portal 给此 App 添加 Application 权限 User.Read.All 并 admin consent",
                "users": [], "count": 0,
            }
        return {
            "ok": False,
            "error": f"graph_users_failed:{status_code}:{err}",
            "error_type": "permanent_failure",
            "users": [], "count": 0,
        }
    users: list[dict[str, Any]] = []
    for item in body.get("value", []) or []:
        email = (item.get("mail") or item.get("userPrincipalName") or "").strip()
        if not email:
            continue
        users.append({
            "id": str(item.get("id") or ""),
            "user_principal_name": str(item.get("userPrincipalName") or ""),
            "display_name": str(item.get("displayName") or ""),
            "mail": str(item.get("mail") or ""),
            "email": email,
            "job_title": str(item.get("jobTitle") or ""),
            "department": str(item.get("department") or ""),
            "account_enabled": bool(item.get("accountEnabled", True)),
        })
    return {"ok": True, "users": users, "count": len(users)}


# ---------------------------------------------------------------------------
# Helpers exposed for settings UI
# ---------------------------------------------------------------------------

def admin_consent_url() -> str:
    """Return the Azure AD admin-consent URL (one-time grant)."""
    settings = get_settings()
    tenant = settings.graph_tenant_id.strip()
    client_id = settings.graph_client_id.strip()
    if not (tenant and client_id):
        return ""
    return f"https://login.microsoftonline.com/{tenant}/adminconsent?client_id={client_id}"


def graph_config_status() -> dict[str, Any]:
    """Return a snapshot for the settings page (no secrets leaked)."""
    settings = get_settings()
    return {
        "tenant_configured": bool(settings.graph_tenant_id.strip()),
        "client_configured": bool(settings.graph_client_id.strip()),
        "mailbox": settings.graph_mailbox_upn.strip(),
        "scopes": settings.graph_default_scopes.strip() or "https://graph.microsoft.com/.default",
        "admin_consent_url": admin_consent_url(),
    }
