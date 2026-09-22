from unittest.mock import MagicMock, patch

import pytest

from emailing.email_sender import send_email
from emailing.graph_client import send_via_graph


@pytest.mark.asyncio
async def test_send_email_missing_recipient():
    result = await send_email({}, to_email="", subject="Hi", body_text="Hello")
    assert result["ok"] is False
    assert result["error_type"] == "invalid_recipient"


@pytest.mark.asyncio
async def test_send_email_legacy_smtp_provider_is_coerced_to_graph(monkeypatch):
    """Old rows with provider_type='smtp' must flow through Graph (not error out)."""
    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        if url.endswith("/messages"):
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        return (202, "")

    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    account = {
        "provider_type": "smtp",  # legacy value, should be coerced to graph
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "",
    }
    result = await send_email(account, to_email="buyer@recipient-test.io", subject="Hi", body_text="Hello")
    assert result["ok"] is True
    assert result["provider"] == "graph"


# ---------------------------------------------------------------------------
# Microsoft Graph — two-step send (regression for InvalidInternetMessageHeader)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_send_uses_two_step_create_then_send(monkeypatch):
    """Graph's `sendMail` rejects `Message-ID` set via `internetMessageHeaders`.

    We side-step that by first POSTing to `/users/{upn}/messages` (which
    gives us back the real `internetMessageId` / `conversationId`) and then
    POSTing `/users/{upn}/messages/{id}/send`. This test pins that flow.
    """
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "sales@example.com",
    }

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        if method == "POST" and url.endswith("/messages"):
            return (201, {
                "id": "AAMkAGI2TG9",
                "internetMessageId": "<abc123@mail.example.com>",
                "conversationId": "conv-xyz",
            })
        if method == "POST" and url.endswith("/send"):
            return (202, "")
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    result = await send_via_graph(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert result["provider"] == "graph"
    # The real internetMessageId flows through to provider_message_id so
    # reply_detector's `find_message_by_provider_message_id` can match it.
    assert result["provider_message_id"] == "<abc123@mail.example.com>"
    # conversationId is the stable thread_key for Graph replies.
    assert result["thread_key"] == "conv-xyz"
    assert "sendMail" not in (result.get("error") or "")


@pytest.mark.asyncio
async def test_graph_send_does_not_set_message_id_header(monkeypatch):
    """The bug was: `internetMessageHeaders: [{name: "Message-ID", ...}]`
    in the sendMail payload, which Graph 400s with `InvalidInternetMessageHeader`.

    Verify the new flow NEVER puts Message-ID into any request body.
    """
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "",
    }
    seen_bodies: list[dict] = []

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        if json_body:
            seen_bodies.append(json_body)
        if url.endswith("/messages"):
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        return (202, "")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    await send_via_graph(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    # No request body should carry internetMessageHeaders at all —
    # Message-ID is a reserved trace header that Graph won't let us set.
    for body in seen_bodies:
        message = body.get("message", body)
        assert "internetMessageHeaders" not in message, (
            f"Graph rejected Message-ID via internetMessageHeaders; "
            f"got body: {body!r}"
        )


@pytest.mark.asyncio
async def test_graph_send_create_failure_surfaces_as_error(monkeypatch):
    """If the create-draft step fails, the caller sees a clear error code
    instead of a generic 400 from the second-step send."""
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        return (401, {"error": {"code": "ErrorInvalidAuthenticationToken"}})

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    result = await send_via_graph(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is False
    assert result["error_type"] == "auth_error"
    assert "graph_create_failed" in result["error"]
    assert "ErrorInvalidAuthenticationToken" in result["error"]


@pytest.mark.asyncio
async def test_graph_send_sets_configured_representative_from(monkeypatch):
    """The create payload must carry the configured representative identity.

    The actual Graph account is still selected by the request URL, while
    Exchange Send As permissions enforce whether the representative address
    is usable.
    """
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        "from_email": "alias@example.com",
        "reply_to": "",
    }
    create_payloads: list[dict] = []

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        if method == "POST" and url.endswith("/messages"):
            if json_body:
                create_payloads.append(json_body)
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        if method == "POST" and url.endswith("/send"):
            return (202, "")
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)
    # Isolate from the operator's real .env: `representative_identity`
    # prefers EMAIL_REPRESENTATIVE_ADDRESS / EMAIL_FROM_ADDRESS over the
    # shared mailbox, which would leak local config into the assertion.
    monkeypatch.setattr(
        "emailing.graph_client.get_settings",
        lambda: type("S", (), {
            "email_representative_name": "",
            "email_from_name": "",
            "email_representative_address": "",
            "email_from_address": "",
            "email_representative_reply_to": "",
            "email_reply_to": "",
            "email_shared_inbox_upn": "",
            "graph_mailbox_upn": "",
        })(),
    )

    result = await send_via_graph(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert len(create_payloads) == 1
    assert create_payloads[0]["from"]["emailAddress"]["address"] == "sales@example.com"


@pytest.mark.asyncio
async def test_graph_send_falls_back_to_sendmail_when_two_step_send_fails(monkeypatch):
    """If the two-step create+send fails on the SEND step (e.g. a
    tenant-specific validation rule), we transparently fall back to
    single-shot sendMail so the user isn't stuck with a broken test-send.
    """
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0, account=None):
        if method == "POST" and url.endswith("/messages") and "/send" not in url:
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        if method == "POST" and url.endswith("/send"):
            return (400, {"error": {"code": "InvalidInternetMessageHeader"}})
        if method == "POST" and url.endswith("/sendMail"):
            return (202, "")
        if method == "DELETE":
            return (204, "")
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    result = await send_via_graph(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True, f"expected fallback to succeed; got: {result}"
    # The provider_message_id stays empty in the sendMail path (no id back).
    assert result["provider_message_id"] == ""
    assert result["provider"] == "graph"


# ---------------------------------------------------------------------------
# Stub-guard: refuse to accept placeholder provider_message_id values.
#
# Background: a previous version of the scheduler hard-coded
# `provider_message_id="x" / thread_key="y"` and wrote `status='sent'`
# rows that never went through Graph. That polluted the daily-quota
# counter and reply detection. The guard below is the last line of
# defence — if any future code path returns a known stub token, we
# refuse to mark the send successful so the operator can see a clean
# failure instead of a fake success.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_email_rejects_stub_provider_message_id(monkeypatch):
    """If `send_via_graph` ever returns `provider_message_id="x"` (or
    any other known stub token), `send_email` flips the result to
    `ok=False` with a clear error type so the scheduler doesn't mark
    the row as sent.
    """
    account = {
        "provider_type": "graph",
        "from_name": "",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    async def fake_via_graph(*args, **kwargs):
        return {
            "ok": True,
            "provider": "graph",
            "provider_message_id": "x",  # the bad stub value
            "thread_key": "y",
            "sent_at": "2026-08-24T10:00:00+00:00",
            "error": "",
            "error_type": "",
        }

    # `send_email` does `from emailing.graph_client import send_via_graph`
    # lazily, so patch the symbol on the graph_client module — that's the
    # one the function will resolve at call time.
    monkeypatch.setattr("emailing.graph_client.send_via_graph", fake_via_graph)

    result = await send_email(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is False, f"stub pm_id should be rejected; got: {result}"
    assert result["provider_message_id"] == ""
    assert result["error_type"] == "permanent_failure"
    assert "stub_provider_message_id" in result["error"]


@pytest.mark.asyncio
async def test_send_email_rejects_stub_thread_key(monkeypatch):
    """Symmetric guard for `thread_key` — same stub tokens, same response.
    `thread_key` doesn't drive quota but reply-detector keys on it, so a
    stub value would silently break the conversation linkage.
    """
    account = {
        "provider_type": "graph",
        "from_name": "",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    async def fake_via_graph(*args, **kwargs):
        return {
            "ok": True,
            "provider": "graph",
            "provider_message_id": "real-graph-id-AAMkAGI2",
            "thread_key": "stub",
            "sent_at": "2026-08-24T10:00:00+00:00",
            "error": "",
            "error_type": "",
        }

    monkeypatch.setattr("emailing.graph_client.send_via_graph", fake_via_graph)

    result = await send_email(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is False, f"stub thread_key should be rejected; got: {result}"


@pytest.mark.asyncio
async def test_send_email_allows_empty_provider_message_id(monkeypatch):
    """The single-step `sendMail` fallback path legitimately returns
    `provider_message_id=""` (no id back from sendMail). The stub-guard
    must NOT reject empty string — only known stub tokens.
    """
    account = {
        "provider_type": "graph",
        "from_name": "",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    async def fake_via_graph(*args, **kwargs):
        return {
            "ok": True,
            "provider": "graph",
            "provider_message_id": "",  # legitimate: sendMail fallback
            "thread_key": "graph-sendmail:foo:bar:Hi",
            "sent_at": "2026-08-24T10:00:00+00:00",
            "error": "",
            "error_type": "",
        }

    monkeypatch.setattr("emailing.graph_client.send_via_graph", fake_via_graph)

    result = await send_email(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert result["provider_message_id"] == ""


@pytest.mark.asyncio
async def test_send_email_accepts_real_graph_id(monkeypatch):
    """Real Graph `internetMessageId` looks like `<id@host>` or
    `AAMkAGI2...` — these must pass through unchanged.
    """
    account = {
        "provider_type": "graph",
        "from_name": "",
        "from_email": "sales@example.com",
        "reply_to": "",
    }

    real_id = "<abc123@mail.example.com>"

    async def fake_via_graph(*args, **kwargs):
        return {
            "ok": True,
            "provider": "graph",
            "provider_message_id": real_id,
            "thread_key": "conv-xyz",
            "sent_at": "2026-08-24T10:00:00+00:00",
            "error": "",
            "error_type": "",
        }

    monkeypatch.setattr("emailing.graph_client.send_via_graph", fake_via_graph)

    result = await send_email(
        account,
        to_email="buyer@recipient-test.io",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert result["provider_message_id"] == real_id


# ── reserved-recipient guard ─────────────────────────────────────
# Short-circuits the send before Graph is called when the recipient
# is a known test/reserved domain (RFC 2606 / 6761). Catches the
# "buyer@acme.com / hello@acme.com" smoke-test case that was leaking
# into the tenant's mailbox as 550 bounces.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked",
    [
        "buyer@acme.com",
        "hello@acme.com",
        "anyone@example.com",
        "anyone@example.org",
        "anything@test.com",
        "anyone@host.invalid",
        "anyone@service.localhost",
        "anyone@subdomain.test",  # reserved TLD
    ],
)
async def test_send_email_rejects_reserved_recipient_domains(monkeypatch, blocked):
    # If the guard ever leaks, Graph will be called — that would mean
    # the test environment is wrong, not that the guard is wrong.
    called = {"count": 0}

    async def fake_send(*_args, **_kwargs):
        called["count"] += 1
        return {"ok": True, "provider_message_id": "graph-real-id", "thread_key": "tk"}

    monkeypatch.setattr(
        "emailing.graph_client.send_via_graph", fake_send, raising=False
    )

    result = await send_email(
        {"provider_type": "graph", "from_email": "ops@real-tenant.com"},
        to_email=blocked,
        subject="test",
        body_text="test",
    )
    assert result["ok"] is False
    assert result["error_type"] == "invalid_recipient"
    assert "reserved_recipient" in result["error"]
    # And — critically — Graph was NOT called.
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_send_email_allows_normal_recipient(monkeypatch):
    async def fake_send(*_args, **_kwargs):
        return {
            "ok": True,
            "provider_message_id": "graph-real-id",
            "thread_key": "tk",
        }

    monkeypatch.setattr(
        "emailing.graph_client.send_via_graph", fake_send, raising=False
    )

    result = await send_email(
        {"provider_type": "graph", "from_email": "ops@real-tenant.com"},
        to_email="real-buyer@some-real-company.com",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert result["provider_message_id"] == "graph-real-id"
