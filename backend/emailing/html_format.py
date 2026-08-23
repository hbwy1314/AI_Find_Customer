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


# Localised strings for the unsubscribe card. Keys mirror the
# closing-phrase table in ``body_format._DEFAULT_CLOSING_BY_LOCALE``
# so a single ``locale`` value drives both the body's salutation
# *and* the unsubscribe prompt. Every entry is 3 strings:
#
#   ``prompt``   — small text above the button ("No longer want …?")
#   ``button``   — the button label (kept short — fits the button)
#   ``fallback`` — small text below the button (the "or reply
#                  'unsubscribe'" hint)
#
# Arabic uses RTL; we keep the layout direction in the card's
# ``dir`` attribute so clients mirror the prompt + fallback.
_UNSUBSCRIBE_CARD_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "prompt": "No longer want to receive these emails?",
        "button": "Unsubscribe",
        "fallback": "Or simply reply with “unsubscribe”.",
    },
    "de": {
        "prompt": "Diese E-Mails nicht mehr erhalten?",
        "button": "Abmelden",
        "fallback": "Oder antworten Sie einfach mit „Abmelden“.",
    },
    "fr": {
        "prompt": "Vous ne souhaitez plus recevoir ces e-mails ?",
        "button": "Se désabonner",
        "fallback": "Ou répondez simplement « désabonner ».",
    },
    "es": {
        "prompt": "¿Ya no deseas recibir estos correos?",
        "button": "Cancelar suscripción",
        "fallback": "O simplemente responde con “cancelar”.",
    },
    "pt": {
        "prompt": "Não quer mais receber estes e-mails?",
        "button": "Cancelar inscrição",
        "fallback": "Ou simplesmente responda com “cancelar”.",
    },
    "it": {
        "prompt": "Non vuoi più ricevere queste email?",
        "button": "Annulla iscrizione",
        "fallback": "O rispondi semplicemente con “annulla”.",
    },
    "nl": {
        "prompt": "Wil je deze e-mails niet meer ontvangen?",
        "button": "Afmelden",
        "fallback": "Of reageer eenvoudig met “afmelden”.",
    },
    "pl": {
        "prompt": "Nie chcesz już otrzymywać tych e-maili?",
        "button": "Anuluj subskrypcję",
        "fallback": "Lub po prostu odpowiedz „anuluj”.",
    },
    "ru": {
        "prompt": "Больше не хотите получать эти письма?",
        "button": "Отписаться",
        "fallback": "Или просто ответьте «отписаться».",
    },
    "ja": {
        "prompt": "これらのメールを受信しない場合は？",
        "button": "配信停止",
        "fallback": "または「配信停止」と返信してください。",
    },
    "ko": {
        "prompt": "이러한 이메일을 더 이상 받고 싶지 않으신가요?",
        "button": "수신 거부",
        "fallback": "또는 간단히 '수신 거부'로 회신하세요.",
    },
    "zh": {
        "prompt": "不再希望收到此类邮件？",
        "button": "一键退订",
        "fallback": "或直接回复「退订」即可。",
    },
    "tw": {
        "prompt": "不再希望收到此類郵件？",
        "button": "一鍵退訂",
        "fallback": "或直接回覆「退訂」即可。",
    },
    "ar": {
        "prompt": "هل لا تريد تلقي هذه الرسائل بعد الآن؟",
        "button": "إلغاء الاشتراك",
        "fallback": "أو ببساطة رد بكلمة «إلغاء الاشتراك».",
    },
    "tr": {
        "prompt": "Bu e-postaları artık almak istemiyor musunuz?",
        "button": "Abonelikten çık",
        "fallback": "Veya sadece \"aboneliği iptal et\" ile yanıtlayın.",
    },
}


def _unsubscribe_strings_for_locale(locale: str | None) -> dict[str, str]:
    """Return the localised ``{prompt, button, fallback}`` for the
    unsubscribe card, falling back to English when the locale is
    unknown.

    Tries (in order):
    1. Exact match (``"zh_TW"`` → ``"zh_TW"`` key if present).
    2. Base language (``"zh_TW"`` → ``"tw"`` for traditional Chinese,
       or ``"zh"`` for simplified — whichever we have).
    3. English fallback.
    """
    if not locale:
        return _UNSUBSCRIBE_CARD_STRINGS["en"]
    norm = locale.lower().replace("-", "_")
    if norm in _UNSUBSCRIBE_CARD_STRINGS:
        return _UNSUBSCRIBE_CARD_STRINGS[norm]
    lang = norm.split("_", 1)[0]
    # Special-case the zh split: zh_TW should map to "tw" (we have
    # a traditional-Chinese entry), zh_CN / zh_SG etc. map to "zh".
    if lang == "zh" and norm.startswith("zh_tw"):
        return _UNSUBSCRIBE_CARD_STRINGS["tw"]
    return _UNSUBSCRIBE_CARD_STRINGS.get(lang, _UNSUBSCRIBE_CARD_STRINGS["en"])


_RTL_LOCALES = {"ar"}


def _unsubscribe_card_html(url: str, locale: str | None = None) -> str:
    """Render the unsubscribe card in the recipient's language.

    Uses ``_unsubscribe_strings_for_locale`` to pick the prompt /
    button / fallback text. Arabic locales get a ``dir="rtl"``
    attribute on the wrapping ``<div>`` so the card mirrors in RTL
    mail clients.
    """
    s = _unsubscribe_strings_for_locale(locale)
    escaped_url = _html.escape(url, quote=True)
    escaped_prompt = _escape_text(s["prompt"])
    escaped_button = _escape_text(s["button"])
    escaped_fallback = _escape_text(s["fallback"])
    dir_attr = ' dir="rtl"' if (locale or "").lower().replace("-", "_").split("_", 1)[0] in _RTL_LOCALES else ""
    return (
        f'<div{dir_attr} style="margin:24px 0 8px 0;padding:16px 20px;'
        f'background:#f5f5f7;border-radius:8px;text-align:center;">'
        f'<p style="margin:0 0 4px 0;font-size:13px;color:#555;">{escaped_prompt}</p>'
        f'<p style="margin:8px 0;">'
        f'<a href="{escaped_url}" style="display:inline-block;padding:10px 20px;'
        f'background:#1d1d1f;color:#ffffff;text-decoration:none;border-radius:6px;'
        f'font-weight:500;font-size:14px;">{escaped_button}</a>'
        f'</p>'
        f'<p style="margin:8px 0 0 0;font-size:12px;color:#888;">{escaped_fallback}</p>'
        f"</div>"
    )


def plaintext_to_html(
    body_text: str,
    unsubscribe_url: Optional[str] = None,
    *,
    extra_footer_text: Optional[str] = None,
    locale: Optional[str] = None,
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
        locale: Recipient's locale (e.g. ``"en"``, ``"de_DE"``).
            Drives the unsubscribe card's prompt / button / fallback
            text — so the card stays in the same language as the
            body and never lands a Chinese button on an English
            email. Falls back to English when unknown.

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
            _unsubscribe_card_html(unsubscribe_url, locale=locale)
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


def render_preview_html(body_text: str, locale: Optional[str] = None) -> str:
    """Render a preview-friendly HTML body (with a placeholder URL).

    Used by the in-product preview so the UI can show the recipient
    what the actual email will look like. The placeholder URL is
    swapped for a real per-recipient token at send time, so this
    preview does NOT need to be regenerated per message.

    Args:
        body_text: Same as ``plaintext_to_html``.
        locale: Drives the unsubscribe card's language. Defaults to
            ``None`` (English fallback) when the caller doesn't have
            the locale handy — UI previews can pass it through from
            the lead's metadata.
    """
    return plaintext_to_html(
        body_text,
        unsubscribe_url=_UNSUBSCRIBE_PLACEHOLDER_URL,
        locale=locale,
    )
