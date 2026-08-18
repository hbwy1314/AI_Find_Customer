from emailing.body_format import (
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


def test_format_email_sequence_bodies_appends_unsubscribe_placeholder():
    emails = [
        {
            "subject": "Potential fit",
            "body_text": "Dear Sir/Madam, we manufacture micro switches.\n\nKind regards,",
        },
    ]

    formatted = format_email_sequence_bodies(emails)
    body = formatted[0]["body_text"]
    # The preview should expose a placeholder unsubscribe URL so
    # the recipient knows where the opt-out link will land.
    assert "不再接收此类邮件" in body
    assert "__preview__" in body
    assert body.rstrip().endswith("__preview__")


def test_format_email_sequence_bodies_no_double_footer():
    """Calling the helper twice must not stack two footer blocks."""
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
    assert once.count("不再接收此类邮件：") == 1
    assert twice.count("不再接收此类邮件：") == 1


def test_format_email_sequence_bodies_can_skip_unsubscribe_footer():
    emails = [
        {
            "subject": "Potential fit",
            "body_text": "Dear Sir/Madam, we manufacture micro switches.\n\nKind regards,",
        },
    ]

    formatted = format_email_sequence_bodies(
        emails, append_unsubscribe_footer=False
    )
    assert "不再接收此类邮件" not in formatted[0]["body_text"]
