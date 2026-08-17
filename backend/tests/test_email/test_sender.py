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

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
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
    result = await send_email(account, to_email="buyer@example.com", subject="Hi", body_text="Hello")
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

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
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
        to_email="buyer@example.com",
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

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
        if json_body:
            seen_bodies.append(json_body)
        if url.endswith("/messages"):
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        return (202, "")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    await send_via_graph(
        account,
        to_email="buyer@example.com",
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

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
        return (401, {"error": {"code": "ErrorInvalidAuthenticationToken"}})

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    result = await send_via_graph(
        account,
        to_email="buyer@example.com",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is False
    assert result["error_type"] == "auth_error"
    assert "graph_create_failed" in result["error"]
    assert "ErrorInvalidAuthenticationToken" in result["error"]


@pytest.mark.asyncio
async def test_graph_send_does_not_set_from_in_create_payload(monkeypatch):
    """The original InvalidInternetMessageHeader error was triggered by
    setting `from` in the create payload to an address that didn't match
    the shared mailbox. We must NOT set `from` in the create step —
    Graph will assign the mailbox owner itself.
    """
    account = {
        "provider_type": "graph",
        "from_name": "Ai Hunter",
        # Note: from_email is intentionally different from the mailbox
        # upn below. With Application permissions Graph forces the
        # sender to the shared mailbox anyway; setting `from` here
        # would just confuse it.
        "from_email": "alias@example.com",
        "reply_to": "",
    }
    create_payloads: list[dict] = []

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
        if method == "POST" and url.endswith("/messages"):
            if json_body:
                create_payloads.append(json_body)
            return (201, {"id": "m1", "internetMessageId": "<m1@x>", "conversationId": "c1"})
        if method == "POST" and url.endswith("/send"):
            return (202, "")
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr("emailing.graph_client._mailbox_upn", lambda: "sales@example.com")
    monkeypatch.setattr("emailing.graph_client._graph_request", fake_request)

    result = await send_via_graph(
        account,
        to_email="buyer@example.com",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True
    assert len(create_payloads) == 1
    assert "from" not in create_payloads[0], (
        f"create payload must not include `from`; got: {create_payloads[0]!r}"
    )


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

    async def fake_request(method, url, *, json_body=None, headers=None, timeout=30.0):
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
        to_email="buyer@example.com",
        subject="Hi",
        body_text="Hello",
    )
    assert result["ok"] is True, f"expected fallback to succeed; got: {result}"
    # The provider_message_id stays empty in the sendMail path (no id back).
    assert result["provider_message_id"] == ""
    assert result["provider"] == "graph"
