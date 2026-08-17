"""Public unsubscribe endpoints — no auth required.

When a recipient clicks the unsubscribe link (or a mail client uses
RFC 8058 one-click-unsubscribe POST), this handler verifies the HMAC
token, records the opt-out, and returns a small HTML confirmation page.

Keeping these endpoints unauthenticated is intentional: unsubscribe
links must work for anyone, including recipients who were never logged
in to the app and never will be. The HMAC token is the proof of
identity; the user is identified by the email baked into the token.
"""

from __future__ import annotations

import logging
from html import escape as html_escape

from fastapi import APIRouter, Response

from emailing.store import get_email_store
from emailing.unsubscribe import token_hash, verify_token

logger = logging.getLogger(__name__)

router = APIRouter()


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} — AI Hunter</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    margin: 0;
    padding: 24px;
  }}
  .card {{
    background: #fff;
    border-radius: 12px;
    padding: 32px 40px;
    max-width: 480px;
    box-shadow: 0 4px 12px rgba(0,0,0,.06);
    text-align: center;
  }}
  h1 {{ font-size: 20px; margin: 0 0 12px; }}
  p {{ font-size: 14px; line-height: 1.6; color: #555; margin: 0 0 8px; }}
  .email {{ font-family: monospace; background: #f5f5f7; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  {body}
</div>
</body>
</html>"""


def _render(title: str, body_html: str, *, status: int = 200) -> Response:
    return Response(
        content=_HTML_TEMPLATE.format(title=html_escape(title), body=body_html),
        status_code=status,
        media_type="text/html; charset=utf-8",
    )


def _do_unsubscribe(token: str) -> tuple[str, str, int]:
    """Verify token + record. Returns (title, body_html, status)."""
    if not token:
        return ("链接无效", "<p>未提供退订 token。</p>", 400)

    payload = verify_token(token)
    if not payload:
        return (
            "链接无效或已过期",
            "<p>这个退订链接无效、已过期或被篡改。</p>"
            "<p>如果你确实想退订，请在邮件中点击原始的退订链接重新尝试。</p>",
            400,
        )

    email = str(payload.get("email", "")).strip().lower()
    scope = str(payload.get("scope", "all")).strip() or "all"
    if not email:
        return ("链接无效", "<p>token 缺少收件人信息。</p>", 400)

    store = get_email_store()
    store.record_unsubscribe(
        email=email,
        scope=scope,
        token_hash=token_hash(token),
        source="link",
    )
    logger.info("Unsubscribe recorded email=%s scope=%s", email, scope)

    scope_text = "该邮件活动" if scope != "all" else "所有来自本系统的邮件"
    return (
        "已退订",
        f"<p>地址 <span class=\"email\">{html_escape(email)}</span> 已成功退订 {html_escape(scope_text)}。</p>"
        "<p>我们不会再向你发送任何后续邮件。</p>",
        200,
    )


@router.get("/api/unsubscribe/{token}")
def unsubscribe_get(token: str) -> Response:
    """GET — recipient clicked the unsubscribe link in their email client."""
    title, body, status = _do_unsubscribe(token)
    return _render(title, body, status=status)


@router.post("/api/unsubscribe/{token}")
def unsubscribe_post(token: str) -> Response:
    """POST — RFC 8058 one-click unsubscribe header.

    Same effect as GET but doesn't render a confirmation page; the
    mail client doesn't need to show anything to the user.
    """
    title, body, status = _do_unsubscribe(token)
    if status == 200:
        # Return a tiny 200 with no body so the mail client doesn't
        # try to render the HTML.
        return Response(status_code=200, content="", media_type="text/plain")
    return _render(title, body, status=status)
