from emailing.policy import (
    choose_email_target,
    expand_email_targets,
    is_role_based_email,
)


def test_choose_verified_decision_maker_first():
    lead = {
        "decision_makers": [
            {"name": "Owner", "title": "Owner", "email": "owner@acme.com"},
            {"name": "Buyer", "title": "Purchasing Manager", "email": "buyer@acme.com"},
        ],
        "emails": ["info@acme.com"],
    }
    target = choose_email_target(lead)
    assert target["target_email"] == "buyer@acme.com"
    assert target["target_type"] == "decision_maker_verified"


def test_choose_inferred_decision_maker_before_generic():
    lead = {
        "decision_makers": [
            {"name": "John Doe", "title": "Sales Director", "email": "john.doe@acme.com (inferred)"},
        ],
        "emails": ["info@acme.com"],
    }
    target = choose_email_target(lead)
    assert target["target_email"] == "john.doe@acme.com"
    assert target["target_type"] == "decision_maker_inferred_from_pattern"


def test_falls_back_to_generic_company_email():
    lead = {
        "decision_makers": [],
        "emails": ["info@acme.com", "contact@acme.com"],
    }
    target = choose_email_target(lead)
    assert target["target_email"] in {"contact@acme.com", "info@acme.com"}
    # Both addresses are role-based, so the type is flagged
    # accordingly (was `generic_company_email` before the role
    # labelling was added).
    assert target["target_type"] == "role_based_email"
    assert target["is_role_based"] is True


def test_returns_none_when_no_email_available():
    target = choose_email_target({"decision_makers": [], "emails": []})
    assert target["target_type"] == "none"
    assert target["target_email"] == ""


def test_expand_email_targets_keeps_all_unique_business_emails():
    lead = {
        "decision_makers": [
            {"name": "Buyer", "title": "Purchasing Manager", "email": "buyer@acme.com"},
            {"name": "Owner", "title": "Owner", "email": "owner@acme.com"},
        ],
        "emails": ["info@acme.com", "sales@acme.com", "buyer@acme.com"],
    }
    targets = expand_email_targets(lead)
    assert [item["target_email"] for item in targets] == [
        "buyer@acme.com",
        "owner@acme.com",
        "info@acme.com",
        "sales@acme.com",
    ]


def test_is_role_based_email():
    assert is_role_based_email("info@acme.com") is True
    assert is_role_based_email("sales@example.org") is True
    assert is_role_based_email("support@noreply.io") is True
    assert is_role_based_email("press@news.co") is True
    assert is_role_based_email("noreply@billing.io") is True
    # Non-role addresses
    assert is_role_based_email("john.doe@acme.com") is False
    assert is_role_based_email("ceo@startup.io") is False
    assert is_role_based_email("carla@vendor.com") is False
    # Empty / invalid
    assert is_role_based_email("") is False
    assert is_role_based_email("not-an-email") is False


def test_expand_email_targets_marks_role_based_flag():
    lead = {
        "decision_makers": [
            {"name": "Buyer", "title": "Purchasing Manager", "email": "buyer@acme.com"},
            {"name": "Sales", "title": "Sales Director", "email": "sales@acme.com"},
        ],
        "emails": ["info@acme.com"],
    }
    targets = expand_email_targets(lead)
    by_email = {t["target_email"]: t for t in targets}
    assert by_email["buyer@acme.com"]["is_role_based"] is False
    assert by_email["sales@acme.com"]["is_role_based"] is True
    assert "role_based" in by_email["sales@acme.com"]["target_type"]
    assert by_email["info@acme.com"]["is_role_based"] is True
    assert by_email["info@acme.com"]["target_type"] == "role_based_email"
