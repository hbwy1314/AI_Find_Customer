"""Tests for ``emailing.html_format`` — the HTML body renderer used
when sending emails through Microsoft Graph.

These tests pin down three things:

1. **Escaping** — user-supplied text never produces executable HTML
   (no raw ``<script>`` / attribute injection).
2. **Paragraph splitting** — the plain-text body is mapped to
   ``<p>`` blocks with intra-paragraph ``<br>`` preserved.
3. **Unsubscribe card** — the rendered card carries the *real*
   per-recipient URL (placeholder is replaced) and exposes a
   clickable ``<a>`` so mail clients render it as a button.
"""

import re

import pytest

from emailing.html_format import (
    _LEGACY_FOOTER_MARKER,
    _LEGACY_FOOTER_SEP,
    _strip_legacy_footer,
    plaintext_to_html,
    render_preview_html,
)


# ── escaping ───────────────────────────────────────────────────────


class TestEscaping:
    def test_lt_gt_amp_escaped(self):
        html = plaintext_to_html(
            "Hello <script>alert(1)</script> & \"quotes\" test",
            unsubscribe_url="https://x.com/u",
        )
        # The angle brackets around the script tag must be escaped so
        # the recipient's mail client treats it as text, not a tag.
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        # The literal `alert(1)` is content *inside* an escaped tag,
        # so it stays as-is — the browser sees the surrounding
        # `&lt;script&gt;` as text, not a real script.
        assert "alert(1)" in html

    def test_url_with_query_params_escaped(self):
        html = plaintext_to_html(
            "Body",
            unsubscribe_url="https://x.com/u?a=1&b=2",
        )
        # The unsubscribe URL's `&` must be escaped in the href so the
        # recipient's mail client doesn't truncate the link at the
        # unescaped `&`.
        assert "a=1&amp;b=2" in html
        # And the original & must not appear in raw form in the href
        # attribute (it would be allowed outside attribute context, so
        # we re-check only the href).
        hrefs = re.findall(r'href="([^"]*)"', html)
        assert any("a=1&amp;b=2" in h for h in hrefs)

    def test_ampersand_in_body_text_escaped(self):
        html = plaintext_to_html("AT&T is great", unsubscribe_url="https://x.com/u")
        assert "AT&amp;T" in html


# ── paragraph splitting ────────────────────────────────────────────


class TestParagraphSplitting:
    def _body_paragraph_count(self, html: str) -> int:
        """Count <p> tags *outside* the unsubscribe card.

        The card also uses <p> for its three text lines, so a naive
        count would conflate body and card. We strip the card before
        counting.
        """
        # Cut from the first <div style="margin:24px...background:
        # #f5f5f7... to the closing </div>. The card is the only block
        # with that specific background colour so the cut is stable.
        cut = re.sub(
            r'<div style="margin:24px 0 8px 0;padding:16px 20px;.*?</div>',
            "",
            html,
            count=1,
            flags=re.DOTALL,
        )
        return cut.count("<p ")

    def test_single_paragraph_no_split(self):
        html = plaintext_to_html("Just one line.", unsubscribe_url="https://x.com/u")
        # Should be wrapped in a single <p>.
        assert self._body_paragraph_count(html) == 1
        assert "Just one line." in html

    def test_blank_line_makes_new_paragraph(self):
        html = plaintext_to_html(
            "First paragraph.\n\nSecond paragraph.",
            unsubscribe_url="https://x.com/u",
        )
        # Two <p> tags for two paragraphs.
        assert self._body_paragraph_count(html) == 2
        assert "First paragraph." in html
        assert "Second paragraph." in html

    def test_single_newline_renders_as_br(self):
        html = plaintext_to_html(
            "Line one\nLine two",
            unsubscribe_url="https://x.com/u",
        )
        assert "<br>" in html
        # One paragraph, two lines.
        assert self._body_paragraph_count(html) == 1

    def test_three_newlines_collapse_to_one_blank(self):
        html = plaintext_to_html(
            "Para one\n\n\n\nPara two",
            unsubscribe_url="https://x.com/u",
        )
        # 3+ newlines become a single blank-line separator, so still
        # 2 paragraphs (not 3).
        assert self._body_paragraph_count(html) == 2


# ── unsubscribe card ───────────────────────────────────────────────


class TestUnsubscribeCard:
    def test_card_uses_real_url(self):
        html = plaintext_to_html(
            "Body",
            unsubscribe_url="https://api.nineluan.com/api/unsubscribe/abc",
        )
        # The card's <a> tag must carry the real token, not the
        # placeholder.
        assert "https://api.nineluan.com/api/unsubscribe/abc" in html
        assert "__preview__" not in html
        # The card uses inline-block + border-radius to look like a
        # button in mail clients.
        assert "display:inline-block" in html
        assert "border-radius" in html
        # The text label is human-readable.
        assert "一键退订" in html

    def test_no_card_without_url(self):
        html = plaintext_to_html("Body", unsubscribe_url=None)
        # No unsubscribe card when the operator didn't provide a URL
        # (used when sending a draft that doesn't have a token yet).
        assert "一键退订" not in html
        assert "border-radius" not in html

    def test_placeholder_url_substitution(self):
        # The preview helper should emit a placeholder URL — callers
        # use the result to render the in-product preview, and the
        # scheduler swaps the placeholder for the real per-recipient
        # token right before send.
        html = render_preview_html("Body")
        assert "__preview__" in html

    def test_legacy_footer_stripped_before_rendering(self):
        legacy_body = (
            "Body line 1.\n\n"
            "Body line 2.\n\n"
            f"{_LEGACY_FOOTER_SEP}"
            f"{_LEGACY_FOOTER_MARKER}https://api.nineluan.com/api/unsubscribe/__preview__"
        )
        html = plaintext_to_html(legacy_body, unsubscribe_url="https://x.com/u?token=REAL")
        # The legacy text and placeholder URL must NOT appear in the
        # rendered output (we strip the whole legacy footer before
        # adding our own card).
        assert "不再接收此类邮件：" not in html
        assert "__preview__" not in html
        # But the real URL is in the card.
        assert "token=REAL" in html
        # And the original body is preserved.
        assert "Body line 1." in html
        assert "Body line 2." in html


# ── autolink bare URLs ─────────────────────────────────────────────


class TestAutolinkUrls:
    def test_bare_http_url_becomes_anchor(self):
        html = plaintext_to_html(
            "Visit https://example.com for more.",
            unsubscribe_url="https://x.com/u",
        )
        # The bare URL should be wrapped in an <a>.
        assert '<a href="https://example.com"' in html

    def test_unsubscribe_link_in_card_uses_real_url(self):
        # The card's link should also be a proper <a> tag, not just
        # pasted text.
        html = plaintext_to_html(
            "Body",
            unsubscribe_url="https://api.nineluan.com/api/unsubscribe/abc",
        )
        assert (
            '<a href="https://api.nineluan.com/api/unsubscribe/abc"'
            in html
        )


# ── strip helper (unit-level) ──────────────────────────────────────


class TestStripLegacyFooter:
    def test_legacy_with_separator_and_marker(self):
        body = f"Para{_LEGACY_FOOTER_SEP}{_LEGACY_FOOTER_MARKER}https://x"
        out = _strip_legacy_footer(body)
        assert _LEGACY_FOOTER_MARKER not in out
        assert "Para" in out

    def test_no_legacy_returns_unchanged(self):
        body = "Just body text, no footer."
        out = _strip_legacy_footer(body)
        assert out == body

    def test_only_separator_no_marker_keeps_unchanged(self):
        body = f"Body{_LEGACY_FOOTER_SEP}no marker here"
        out = _strip_legacy_footer(body)
        # Without the marker we leave it alone (the separator is too
        # common a pattern to be a reliable footer signal on its own).
        assert out == body
