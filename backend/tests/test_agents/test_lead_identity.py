from agents.lead_identity import candidate_identity_keys, dedupe_leads, lead_identity_keys, normalize_company_name


def test_exact_normalized_company_names():
    assert normalize_company_name("  ＡＣＭＥ   GmbH ") == "acme gmbh"
    assert normalize_company_name("Acme GmbH") != normalize_company_name("Acme")
    assert normalize_company_name("München") != normalize_company_name("Munchen")


def test_only_exact_normalized_company_name_is_identity():
    assert len(dedupe_leads([
        {"website": "http://www.acme.de/about", "company_name": "Acme"},
        {"website": "https://acme.de/contact?utm_source=x", "company_name": "Other"},
        {"website": "https://different.de", "company_name": " ACME "},
    ])) == 2


def test_contact_details_are_not_identity():
    leads = [{"company_name": name, "emails": ["shared@example.com"],
              "phone_numbers": ["123456789"], "maps_data": {"place_id": "shared"}}
             for name in ("Acme", "Other")]
    assert len(dedupe_leads(leads)) == 2
    assert lead_identity_keys({}) == []
    assert len(dedupe_leads([{}, {}])) == 2


def test_same_company_names_merge_without_domain_matching():
    merged = dedupe_leads([
        {"company_name": "A", "website": "https://a.de", "emails": ["a@a.de"]},
        {"company_name": "A", "website": "https://other.de", "emails": ["b@other.de"]},
        {"company_name": "B", "website": "https://a.de"},
    ])
    assert len(merged) == 2
    assert merged[0]["emails"] == ["a@a.de", "b@other.de"]
    assert len(dedupe_leads(merged + [{"company_name": "A", "website": "https://third.de"}])) == 2


def test_search_title_is_not_company_identity():
    assert candidate_identity_keys({"title": "Best vape wholesalers", "link": ""}) == []
    assert candidate_identity_keys({"maps_data": {"title": "Acme"}}) == ["company:acme"]


def test_public_platform_is_not_customer_domain():
    assert lead_identity_keys({"website": "https://www.linkedin.com/company/acme", "company_name": "Acme"}) == ["company:acme"]
