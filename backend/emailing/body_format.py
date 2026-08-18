"""Helpers for formatting plain-text outbound email bodies."""

from __future__ import annotations

import re

_CLOSING_PATTERNS = (
    "kind regards",
    "best regards",
    "regards",
    "sincerely",
    "yours sincerely",
    "yours faithfully",
    "mit freundlichen grüßen",
    "cordiali saluti",
    "atentamente",
    "atenciosamente",
    "z poważaniem",
    "с уважением",
    "此致",
    "敬礼",
    "敬祝",
    "期待您的回复",
    "よろしくお願いいたします",
    "감사합니다",
    "saygılarımla",
    "مع خالص التقدير",
)

# Default closings by locale. Used when the LLM omits a closing
# (or signs off with a non-standard phrase) so the email still
# reads like a complete outreach message. The signature (if any)
# is appended on the next line.
_DEFAULT_CLOSING_BY_LOCALE: dict[str, str] = {
    "en": "Best regards",
    "de": "Mit freundlichen Grüßen",
    "fr": "Cordialement",
    "es": "Atentamente",
    "pt": "Atenciosamente",
    "it": "Cordiali saluti",
    "nl": "Met vriendelijke groet",
    "pl": "Z poważaniem",
    "ru": "С уважением",
    "ja": "よろしくお願いいたします",
    "ko": "감사합니다",
    "zh": "此致 敬礼",
    "tw": "敬祝 商祺",
    "ar": "مع خالص التقدير",
    "tr": "Saygılarımla",
}


# Placeholder URL that the email preview renders. At send time
# `emailing.unsubscribe.append_footer` detects the footer block
# (recognised by the `不再接收此类邮件：` marker) and replaces the
# placeholder URL with the real per-recipient token, so the recipient
# always sees a working link without us having to mutate the stored
# body ahead of time.
_UNSUBSCRIBE_PLACEHOLDER_URL = "https://api.nineluan.com/api/unsubscribe/__preview__"


def _normalize_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    compact: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            compact.append(line.strip())
        else:
            blank_run += 1
            if blank_run == 1:
                compact.append("")
    return "\n".join(compact).strip()


def _split_sentences(text: str) -> list[str]:
    collapsed = re.sub(r"\s+", " ", text.strip())
    if not collapsed:
        return []
    parts = re.split(r"(?<=[.!?])\s+", collapsed)
    return [part.strip() for part in parts if part.strip()]


def _has_known_closing(text: str) -> bool:
    """True when ``text`` already ends with one of the recognised
    closing phrases (case-insensitive substring match)."""
    if not text:
        return False
    lowered = text.lower()
    return any(pattern in lowered for pattern in _CLOSING_PATTERNS)


def _extract_closing(sentences: list[str]) -> tuple[list[str], str]:
    if not sentences:
        return [], ""
    last = sentences[-1].strip()
    lowered = last.lower()
    if any(lowered.startswith(pattern) for pattern in _CLOSING_PATTERNS):
        return sentences[:-1], last
    return sentences, ""


def _closing_for_locale(locale: str | None) -> str:
    """Return a default closing phrase for the given locale. Falls
    back to English when the locale is unknown."""
    if not locale:
        return _DEFAULT_CLOSING_BY_LOCALE["en"]
    lang = locale.lower().split("_", 1)[0]
    return _DEFAULT_CLOSING_BY_LOCALE.get(lang, _DEFAULT_CLOSING_BY_LOCALE["en"])


def _append_closing_if_missing(text: str, locale: str | None, signature: str | None) -> str:
    """Append a localised closing (and optional signature) when the
    body text is missing one. No-op when a closing is already present
    or when ``text`` is empty.
    """
    normalized = (text or "").rstrip()
    if not normalized:
        return text
    if _has_known_closing(normalized):
        return text
    closing = _closing_for_locale(locale)
    pieces = [normalized, closing]
    sig = (signature or "").strip()
    if sig:
        pieces.append(sig)
    return "\n\n".join(pieces)


def _append_unsubscribe_placeholder(text: str) -> str:
    """Append a placeholder unsubscribe footer so the preview shows
    the recipient where the opt-out link will land. The placeholder
    URL is replaced with a real per-recipient token at send time by
    :func:`emailing.unsubscribe.append_footer` (which recognises the
    marker and re-uses the existing ``--`` separator instead of
    stacking a second footer).
    """
    if not text:
        return text
    sep = "\n\n--\n"
    placeholder = f"不再接收此类邮件：{_UNSUBSCRIBE_PLACEHOLDER_URL}\n"
    cleaned = text.rstrip()
    # Avoid double-appending if the body already has a placeholder
    # (e.g. from a prior review-and-rewrite loop).
    if "\n\n--\n" in cleaned and "不再接收此类邮件：" in cleaned:
        return text
    return cleaned + sep + placeholder


def format_plaintext_email_body(
    body_text: str,
    locale: str | None = None,
    signature: str | None = None,
) -> str:
    """Format email body as readable plain text with paragraphs.

    If the model already returned paragraph breaks, keep them.
    Otherwise apply a conservative sentence-based grouping so the
    body reads like a standard outreach email rather than one block.

    When ``locale`` (and optionally ``signature``) is provided and
    the body is missing a recognised closing, append a localised
    default closing before the signature. This is the safety net
    that catches the "邮件未完整 / 结尾部分缺失" review issue.
    """
    normalized = _normalize_lines(str(body_text or ""))
    if not normalized:
        return ""
    if "\n\n" in normalized:
        body = normalized
    else:
        sentences = _split_sentences(normalized)
        if len(sentences) < 2:
            body = normalized
        else:
            body_sentences, closing = _extract_closing(sentences)
            if len(body_sentences) >= 5:
                groups = [body_sentences[:2], body_sentences[2:4], body_sentences[4:]]
            elif len(body_sentences) == 4:
                groups = [body_sentences[:2], body_sentences[2:]]
            elif len(body_sentences) == 3:
                groups = [body_sentences[:1], body_sentences[1:2], body_sentences[2:]]
            else:
                groups = [body_sentences[:1], body_sentences[1:]]
            paragraphs = [" ".join(group).strip() for group in groups if group]
            if closing:
                paragraphs.append(closing)
            body = "\n\n".join(part for part in paragraphs if part).strip()

    if locale or signature:
        body = _append_closing_if_missing(body, locale, signature)
    return body


def format_email_sequence_bodies(
    emails: list[dict],
    locale: str | None = None,
    signature: str | None = None,
    append_unsubscribe_footer: bool = True,
) -> list[dict]:
    """Return a copy of emails with normalized plain-text paragraph spacing.

    When ``locale``/``signature`` is provided, missing closings are
    back-filled (see :func:`format_plaintext_email_body`).

    When ``append_unsubscribe_footer`` is true (default) every email
    body is given a placeholder unsubscribe footer so the preview
    shows the recipient where the opt-out link will land. The real
    per-recipient token is swapped in at send time.
    """
    formatted: list[dict] = []
    for email in emails:
        if not isinstance(email, dict):
            formatted.append(email)
            continue
        item = dict(email)
        body = format_plaintext_email_body(
            str(item.get("body_text", "") or ""),
            locale=locale,
            signature=signature,
        )
        if append_unsubscribe_footer:
            body = _append_unsubscribe_placeholder(body)
        item["body_text"] = body
        formatted.append(item)
    return formatted
