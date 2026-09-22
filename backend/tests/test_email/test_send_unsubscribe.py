from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from emailing.email_sender import send_email
from emailing.html_format import render_preview_html


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [None, render_preview_html("Body", locale="de_DE")])
async def test_manual_send_issues_real_unsubscribe_link(preview):
    transport = AsyncMock(return_value={"ok": True, "provider_message_id": "real-id"})
    with (
        patch("config.settings.get_settings", return_value=SimpleNamespace(public_base_url="https://mail.example.org")),
        patch("emailing.unsubscribe.issue_token", return_value="signed-token") as issue,
        patch("emailing.graph_client.send_via_graph", transport),
    ):
        result = await send_email({}, to_email="buyer@business.io", subject="Hello", body_text="Body", body_html=preview)
    assert result["ok"]
    issue.assert_called_once_with("buyer@business.io")
    payload = transport.await_args.kwargs
    assert payload["list_unsubscribe_url"] == "https://mail.example.org/api/unsubscribe/signed-token"
    assert "__preview__" not in payload["body_html"]
    assert payload["list_unsubscribe_url"] in payload["body_html"]
