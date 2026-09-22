from scripts import repair_unsubscribe_links as repair


def test_link_issue_classifies_missing_and_preview_links():
    assert repair._link_issue("<p>Body</p>", "buyer@example.com") == "missing"
    assert repair._link_issue(
        '<a href="https://api.example.com/api/unsubscribe/__preview__">Unsubscribe</a>',
        "buyer@example.com",
    ) == "placeholder"


def test_link_issue_rejects_non_https_link(monkeypatch):
    monkeypatch.setattr(
        repair,
        "verify_token",
        lambda _token: {"email": "buyer@example.com", "scope": "all"},
    )
    html = '<a href="http://api.example.com/api/unsubscribe/signed-token">Unsubscribe</a>'

    assert repair._link_issue(html, "buyer@example.com") == "insecure"


def test_link_issue_classifies_invalid_and_mismatched_tokens(monkeypatch):
    html = '<a href="https://api.example.com/api/unsubscribe/signed-token">Unsubscribe</a>'

    monkeypatch.setattr(repair, "verify_token", lambda _token: None)
    assert repair._link_issue(html, "buyer@example.com") == "invalid_or_expired"

    monkeypatch.setattr(
        repair,
        "verify_token",
        lambda _token: {"email": "other@example.com", "scope": "all"},
    )
    assert repair._link_issue(html, "buyer@example.com") == "recipient_mismatch"


def test_link_issue_preserves_valid_recipient_bound_token(monkeypatch):
    html = '<a href="https://api.example.com/api/unsubscribe/signed-token">Unsubscribe</a>'
    monkeypatch.setattr(
        repair,
        "verify_token",
        lambda _token: {"email": "buyer@example.com", "scope": "all"},
    )

    assert repair._link_issue(html, "BUYER@EXAMPLE.COM") == ""


def test_recipient_falls_back_to_first_lead_email():
    sequence = {
        "target": {},
        "lead": {"emails": ["FIRST@EXAMPLE.COM", "second@example.com"]},
    }

    assert repair._recipient_for_sequence(sequence) == "first@example.com"
