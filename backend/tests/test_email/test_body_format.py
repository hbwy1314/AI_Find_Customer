from emailing.body_format import (
    _append_closing_if_missing,
    format_email_sequence_bodies,
    format_plaintext_email_body,
)


def test_format_plaintext_email_body_adds_paragraph_breaks():
    raw = (
        "Dear Sir/Madam, I noticed Denney Electric Supply serves contractors and industrial customers with electrical "
        "components in the Pennsylvania area. We are Guangdong Yushun Electrical Co., Ltd., a specialized manufacturer "
        "of micro switches, rotary selectors, and toggle switches with over 10 years of experience. Given your focus on "
        "supplying reliable electrical components to local contractors, there may be a natural fit. If this product "
        "category is of interest, I would be happy to share an overview of the relevant models and certifications. "
        "Kind regards,"
    )

    formatted = format_plaintext_email_body(raw)

    assert "\n\n" in formatted
    assert "Kind regards," in formatted.split("\n\n")[-1]


def test_format_plaintext_email_body_keeps_existing_paragraphs():
    raw = "Dear Sir/Madam,\n\nWe manufacture micro switches for industrial controls.\n\nKind regards,"

    assert format_plaintext_email_body(raw) == raw


def test_appends_default_closing_when_missing():
    # Body that lacks a recognised closing — should be back-filled
    # with the locale's default closing when locale is provided.
    raw = "Dear Sir/Madam, we manufacture micro switches for industrial controls. If relevant, I can share specs."

    formatted = format_plaintext_email_body(raw, locale="en_US")

    assert "Best regards" in formatted
    assert formatted.endswith("Best regards")


def test_appends_locale_specific_closing():
    raw = "Sehr geehrte Damen und Herren, wir fertigen Schalter für industrielle Anwendungen. Bei Interesse sende ich gern eine Übersicht der passenden Modelle."

    formatted = format_plaintext_email_body(raw, locale="de_DE")

    assert "Mit freundlichen Grüßen" in formatted


def test_does_not_duplicate_existing_closing():
    raw = "Dear Sir/Madam, we supply industrial switches. If relevant, I can share a short spec sheet.\n\nKind regards,"

    formatted_with_locale = format_plaintext_email_body(raw, locale="en_US")
    formatted_no_locale = format_plaintext_email_body(raw)

    # No matter whether locale is provided, a known closing must not
    # be duplicated.
    assert formatted_with_locale.count("Kind regards") == 1
    assert formatted_no_locale.count("Kind regards") == 1


def test_signature_appended_after_default_closing():
    raw = "Dear Sir/Madam, we supply industrial switches for buyers who need stable supply."

    formatted = format_plaintext_email_body(
        raw, locale="en_US", signature="Sales Lead\nGuangdong Yushun"
    )

    assert "Best regards" in formatted
    assert "Sales Lead" in formatted
    assert "Guangdong Yushun" in formatted
    # Signature lands on the last line
    assert formatted.strip().splitlines()[-1] == "Guangdong Yushun"


def test_format_email_sequence_bodies_back_fills_closing():
    emails = [
        {
            "subject": "Potential fit for your switch category",
            "body_text": "Dear Sir/Madam, we manufacture micro switches. If relevant, I can share a spec sheet.",
        },
    ]

    formatted = format_email_sequence_bodies(
        emails, locale="en_US", signature="Sales Lead"
    )

    body = formatted[0]["body_text"]
    assert "Best regards" in body
    assert "Sales Lead" in body


def test_format_email_sequence_bodies_no_back_fill_without_locale():
    emails = [
        {
            "subject": "Potential fit",
            "body_text": "Dear Sir/Madam, we manufacture micro switches. If relevant, I can share a spec sheet.",
        },
    ]

    formatted = format_email_sequence_bodies(emails)
    # Without locale/signature, the helper is a no-op for the closing
    # so the body shouldn't be touched beyond the paragraph split.
    body = formatted[0]["body_text"]
    assert "Best regards" not in body


def test_format_email_sequence_bodies_omits_plain_text_unsubscribe_footer():
    """The plain-text body must no longer carry a visible unsubscribe
    line or placeholder URL — the unsubscribe CTA lives in the HTML
    body and in the X-List-Unsubscribe header.
    """
    emails = [
        {
            "subject": "Potential fit",
            "body_text": "Dear Sir/Madam, we manufacture micro switches.\n\nKind regards,",
        },
    ]

    formatted = format_email_sequence_bodies(emails)
    body = formatted[0]["body_text"]
    assert "不再接收此类邮件" not in body
    assert "__preview__" not in body
    # The plain body content must still be intact.
    assert "Dear Sir/Madam" in body
    assert "Kind regards" in body


def test_format_email_sequence_bodies_idempotent_without_footer():
    """Calling the helper twice must not introduce any unsubscribe
    artefacts even on repeated runs.
    """
    emails = [
        {
            "subject": "Potential fit",
            "body_text": "Dear Sir/Madam, we manufacture micro switches.\n\nKind regards,",
        },
    ]

    once = format_email_sequence_bodies(emails)[0]["body_text"]
    twice = format_email_sequence_bodies(
        [{"subject": "Potential fit", "body_text": once}]
    )[0]["body_text"]
    assert once == twice
    assert "不再接收此类邮件" not in once
    assert "不再接收此类邮件" not in twice


# ── Signature back-fill (regression: "部分邮件 SYSTEM 部分没有") ──────
# The LLM sometimes writes its own closing ("Best regards, John"),
# which used to short-circuit the signature back-fill and silently
# drop the operator's configured signature block. The helper now
# appends the signature independently of the closing detection.

def test_signature_appended_when_closing_already_present():
    """Body has 'Best regards' but no signature — the configured
    signature must still be appended. This is the Gr8Vape-class
    inconsistency the operator hit."""
    body = (
        "Dear Manu,\n\n"
        "GR8 VAPE LTD's catalogue of disposable vapes and pods lines up "
        "with our Romio DASH and PILOT ranges.\n\n"
        "Best regards,\nJohn Smith"
    )
    out = _append_closing_if_missing(body, locale="en_US", signature="SYSTEM")
    assert "SYSTEM" in out, f"signature dropped: {out!r}"
    # Existing closing kept, signature appended below it.
    assert "Best regards" in out
    assert out.rstrip().endswith("SYSTEM")


def test_signature_not_duplicated_when_already_present():
    body = "Hi,\n\nWe supply pods in bulk.\n\nBest regards,\nSYSTEM"
    out = _append_closing_if_missing(body, locale="en_US", signature="SYSTEM")
    # Body already ends with the configured signature — must not be
    # appended a second time.
    assert out.count("SYSTEM") == 1


def test_signature_and_closing_both_appended_when_both_missing():
    body = "Hi, we make micro switches for industrial controls."
    out = _append_closing_if_missing(body, locale="en_US", signature="SYSTEM")
    assert "Best regards" in out
    assert "SYSTEM" in out
    # Order: closing before signature.
    assert out.index("Best regards") < out.index("SYSTEM")


def test_signature_skipped_when_setting_is_empty():
    """Empty signature is a no-op even when closing is also missing —
    the locale default closing is still back-filled, no signature
    is appended."""
    body = "Hi, we make micro switches."
    out = _append_closing_if_missing(body, locale="en_US", signature="")
    assert "Best regards" in out
    assert "SYSTEM" not in out


def test_signature_preserved_for_chinese_locale():
    body = "您好,我们供应工业级微动开关。\n\n此致敬礼"
    out = _append_closing_if_missing(body, locale="zh_CN", signature="SYSTEM")
    assert "SYSTEM" in out
    assert out.rstrip().endswith("SYSTEM")


def test_format_plaintext_email_body_end_to_end_with_signature():
    """End-to-end: the LLM wrote a closing but no signature. The
    top-level formatter must still surface the operator's signature
    in the final body."""
    body = (
        "Dear Manu,\n\n"
        "GR8 VAPE LTD distributes vape hardware in the UK; our Romio "
        "DASH has 20000 puffs and could fit your catalogue.\n\n"
        "Best regards,\nJohn"
    )
    out = format_plaintext_email_body(body, locale="en_US", signature="SYSTEM")
    assert "SYSTEM" in out
    assert out.rstrip().endswith("SYSTEM")
