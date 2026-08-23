"""HTML body generation for outbound emails.

We send the same outbound email in two flavours:

- ``body_text``: a plain-text body, used for the in-product preview
  and as the fallback when the recipient's mail client refuses HTML.
- ``body_html``: a small, opinionated HTML rendering of the same
  content, with a clearly clickable unsubscribe block. Generated
  *from* the plain-text body so the two stay in sync without asking
  the LLM to write HTML.

The HTML is intentionally minimal: we wrap paragraphs in ``<p>``,
preserve line breaks inside a paragraph with ``<br>``, and put the
unsubscribe block in its own card with a real ``<a href="...">`` so
mail clients render it as a clickable button (most clients turn
display:inline-block + border-radius into a tap-friendly target).

No external CSS, no JS, no images. Inline styles only — they're
ignored by some clients and stripped by others, so the fallback
``<a>`` text label still works.
"""

from __future__ import annotations

import html as _html
import re
from typing import Optional


# Placeholder URL that the in-product preview renders. At send time
# the scheduler (``emailing.scheduler.run_scheduler_once``) detects
# this exact string in the stored body_html and replaces it with the
# real per-recipient token. Keeping it identical to the plain-text
# variant in ``body_format`` means both previews line up.
_UNSUBSCRIBE_PLACEHOLDER_URL = "https://api.nineluan.com/api/unsubscribe/__preview__"


def _escape_text(text: str) -> str:
    """Escape user-supplied text for safe insertion into HTML.

    We keep newlines (``\\\\n``) as-is so we can post-process them
    into ``<br>`` / paragraph splits later.
    """
    return _html.escape(text or "", quote=True)


# Match links (http/https/unsubscribe/...) that we want to keep
# clickable. We do NOT auto-link plain URLs inside the body — the LLM
# rarely includes them, and the unsubscribe link is the only URL we
# care about turning into a button.
_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


def _autolink_urls_in_text(text: str) -> str:
    """Turn bare http(s) URLs in already-escaped text into <a> tags.

    Operates on text that has already been HTML-escaped; the URL's
    own characters (``:``, ``/``, ``?``) are safe in HTML attribute
    values, so we just wrap them.
    """
    def _sub(match: re.Match[str]) -> str:
        url = match.group(0)
        return f'<a href="{url}" style="color:#0066cc;text-decoration:underline;">{url}</a>'
    return _URL_RE.sub(_sub, text)


def _split_paragraphs(text: str) -> list[str]:
    """Split a body into paragraphs on blank lines.

    Single newlines are kept inside the paragraph (rendered as ``<br>``).
    Double+ newlines (i.e. blank line) mark paragraph break.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ newlines to exactly 2 (single blank line separator)
    text = re.sub(r"\n{3,}", "\n\n", text)
    paragraphs = [p.strip() for p in text.split("\n\n")]
    return [p for p in paragraphs if p]


def _paragraph_html(paragraph: str) -> str:
    """Render one paragraph: turn \\n into <br>, autolink bare URLs."""
    # Already-escaped text
    escaped = _escape_text(paragraph)
    # Autolink first so the URL markup itself doesn't get re-escaped
    linked = _autolink_urls_in_text(escaped)
    # Then convert intra-paragraph \n into <br>
    return linked.replace("\n", "<br>\n")


# Signature for the unsubscribe card. The link is rendered as a
# black button — most mail clients respect display:inline-block +
# border-radius and surface it as a tap target.
_UNSUBSCRIBE_CARD_HTML = """
<div style="margin:24px 0 8px 0;padding:16px 20px;background:#f5f5f7;border-radius:8px;text-align:center;">
  <p style="margin:0 0 4px 0;font-size:13px;color:#555;">不再希望收到此类邮件？</p>
  <p style="margin:8px 0;">
    <a href="{url}" style="display:inline-block;padding:10px 20px;background:#1d1d1f;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:500;font-size:14px;">一键退订</a>
  </p>
  <p style="margin:8px 0 0 0;font-size:12px;color:#888;">或直接回复「退订」即可</p>
</div>
"""


def plaintext_to_html(
    body_text: str,
    unsubscribe_url: Optional[str] = None,
    *,
    extra_footer_text: Optional[str] = None,
) -> str:
    """Render ``body_text`` as a self-contained HTML document.

    Args:
        body_text: The plain-text body (already includes greeting,
            body paragraphs, and closing signature). May already
            include the legacy ``--`` / ``不再接收此类邮件：<url>``
            footer — we strip it before rendering so the new
            unsubscribe card replaces it cleanly.
        unsubscribe_url: HTTPS unsubscribe URL. If provided, a card
            with a clickable button is appended at the bottom. If
            ``None``, the unsubscribe card is omitted (used by the
            preview renderer which shows a placeholder URL).
        extra_footer_text: Optional small-print line below the body
            (e.g. "Sent by Acme Vape"). HTML-escaped before insertion.

    Returns:
        A ``<div>...</div>`` HTML fragment (no ``<html>`` / ``<head>``
        wrapper — Graph API accepts a fragment as the body content).
    """
    cleaned = (body_text or "").strip()
    if not cleaned:
        body_html = ""
    else:
        # Strip a legacy plain-text unsubscribe footer so we don't
        # render it twice (plain text version + HTML card).
        cleaned = _strip_legacy_footer(cleaned)
        paragraphs = _split_paragraphs(cleaned)
        rendered = [_paragraph_html(p) for p in paragraphs]
        body_html = "\n".join(
            f'<p style="margin:0 0 16px 0;line-height:1.6;">{p}</p>'
            for p in rendered
        )

    parts: list[str] = []
    if body_html:
        parts.append(body_html)
    if extra_footer_text:
        parts.append(
            f'<p style="margin:8px 0 0 0;font-size:12px;color:#888;line-height:1.4;">{_escape_text(extra_footer_text)}</p>'
        )
    if unsubscribe_url:
        parts.append(
            _UNSUBSCRIBE_CARD_HTML.format(url=_html.escape(unsubscribe_url, quote=True))
        )

    inner = "\n".join(parts).strip()
    if not inner:
        return "<div></div>"

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,'
        '&#34;Segoe UI&#34;,&#34;PingFang SC&#34;,&#34;Microsoft YaHei&#34;,'
        'sans-serif;font-size:14px;color:#1d1d1f;max-width:600px;line-height:1.6;">'
        f"{inner}"
        "</div>"
    )


# Marker we use to recognise a legacy plain-text footer that was
# appended by the previous code path. Mirrors the constants in
# ``body_format.py`` and ``unsubscribe.py``.
_LEGACY_FOOTER_SEP = "\n\n--\n"
_LEGACY_FOOTER_MARKER = "不再接收此类邮件："


def _strip_legacy_footer(body_text: str) -> str:
    """Remove a legacy plain-text unsubscribe footer if present.

    Recognised pattern::

        ...body...
        <blank line>
        --
        不再接收此类邮件：<url>

    We strip everything from the ``--`` separator to end-of-string
    so the new HTML card can replace it without duplicating content.
    """
    if _LEGACY_FOOTER_SEP in body_text and _LEGACY_FOOTER_MARKER in body_text:
        head, _, _tail = body_text.rpartition(_LEGACY_FOOTER_SEP)
        return head.rstrip()
    return body_text


def render_preview_html(body_text: str) -> str:
    """Render a preview-friendly HTML body (with a placeholder URL).

    Used by the in-product preview so the UI can show the recipient
    what the actual email will look like. The placeholder URL is
    swapped for a real per-recipient token at send time, so this
    preview does NOT need to be regenerated per message.
    """
    return plaintext_to_html(body_text, unsubscribe_url=_UNSUBSCRIBE_PLACEHOLDER_URL)
