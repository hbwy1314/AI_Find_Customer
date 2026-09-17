from __future__ import annotations

from agents.lead_identity import (
    candidate_identity_keys,
    dedupe_leads,
    lead_identity_keys,
    normalize_company_name,
    normalize_domain,
)


def test_normalizes_domain_and_company_variants():
    assert normalize_domain("https://WWW.Example.com:443/about/") == "example.com"
    assert normalize_domain("https://münchen.example/") == "xn--mnchen-3ya.example"
    assert normalize_company_name("ACME GmbH & Co. KG") == "acme"


def test_candidate_keys_match_saved_maps_lead():
    lead = {
        "company_name": "Acme GmbH",
        "website": "https://www.acme.example/about",
        "emails": ["Sales@acme.example"],
        "phone_numbers": ["+49 (30) 1234-567"],
        "maps_data": {"place_id": "PID-1", "title": "Acme", "address": "Berlin"},
        "source_url": "https://maps.example/place/acme",
    }
    candidate = {
        "title": "Acme",
        "link": "https://acme.example/",
        "source": "google_maps",
        "maps_data": {
            "place_id": "pid-1",
            "website": "https://acme.example/",
            "phone_number": "+49 30 1234567",
        },
    }

    assert set(candidate_identity_keys(candidate)) & set(lead_identity_keys(lead))


def test_dedupe_leads_merges_richer_duplicate_data():
    leads = dedupe_leads([
        {"company_name": "Acme GmbH", "website": "https://acme.example/about", "emails": ["a@acme.example"]},
        {"company_name": "Acme", "website": "https://www.acme.example/", "emails": ["b@acme.example"], "phone_numbers": ["1234567"]},
    ])

    assert len(leads) == 1
    assert leads[0]["emails"] == ["a@acme.example", "b@acme.example"]
    assert leads[0]["phone_numbers"] == ["1234567"]


def test_dedupe_leads_merges_transitive_aliases_in_one_pass():
    """
    Transitive merge: A-B share domain, B-C share email → all merge.
    Company name alone is NOT an identity (weak key), so leads with
    different emails won't merge just because they share a company name.
    """
    leads = [
        {"company_name": "Acme", "website": "https://acme.example", "emails": ["first@acme.example"]},
        {"company_name": "Acme", "website": "https://acme.example", "emails": ["shared@other.example"]},
        {"company_name": "Other", "emails": ["shared@other.example"]},
    ]

    deduped = dedupe_leads(leads)

    # All three should merge: lead1-lead2 share domain, lead2-lead3 share email
    assert len(deduped) == 1
    assert dedupe_leads(deduped) == deduped


def test_company_name_is_weak_identity():
    """
    Company name is only used as identity when NO strong keys exist.
    Leads with different emails/domains should NOT merge just because
    they share a company name (prevents same-name companies in different
    regions from incorrectly deduping).
    """
    leads = [
        {"company_name": "Vape Store", "emails": ["germany@vapestore.de"]},
        {"company_name": "Vape Store", "emails": ["usa@vapestore.com"]},
    ]

    deduped = dedupe_leads(leads)

    # Should remain separate - different emails, company name doesn't merge them
    assert len(deduped) == 2


def test_company_name_as_fallback_identity():
    """
    Company name IS used as identity when the lead has NO other contact info.
    """
    leads = [
        {"company_name": "Mystery Company"},
        {"company_name": "Mystery Company"},
    ]

    deduped = dedupe_leads(leads)

    # Should merge - no other identities available, company name is the fallback
    assert len(deduped) == 1
