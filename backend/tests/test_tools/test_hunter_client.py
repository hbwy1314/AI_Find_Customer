"""Tests for the Hunter.io client and the email_finder Hunter integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.email_finder import (
    FoundEmail,
    find_emails_for_lead,
    verify_email,
)
from tools.hunter_client import (
    DomainSearchResponse,
    EmailVerifierResponse,
    HunterAuthError,
    HunterClient,
    HunterDisabled,
    HunterError,
    HunterQuotaExhausted,
    HunterRateLimited,
)


def _mock_response(status: int, body: dict | None = None, headers: dict | None = None):
    """Build a fake httpx Response-like object."""
    import json

    class _R:
        def __init__(self):
            self.status_code = status
            self.headers = headers or {}
            self.text = json.dumps(body) if body else ""
            self._json = body or {}

        def json(self):
            return self._json

    return _R()


# ---------------------------------------------------------------------------
# HunterClient unit tests
# ---------------------------------------------------------------------------


def test_disabled_when_no_key() -> None:
    c = HunterClient(api_key="")
    import asyncio

    with pytest.raises(HunterDisabled):
        asyncio.run(c.domain_search("example.com"))


def test_quota_tracking_and_exhaustion() -> None:
    c = HunterClient(api_key="test", monthly_quota=2)
    fake = _mock_response(200, {"data": {"domain": "x.com", "emails": []}})

    async def fake_get(*a, **kw):
        return fake

    with patch.object(c._http, "get", fake_get):
        import asyncio

        assert c.quota_remaining == 2
        asyncio.run(c.domain_search("a.com"))
        assert c.quota_remaining == 1
        asyncio.run(c.domain_search("b.com"))
        assert c.quota_remaining == 0
        with pytest.raises(HunterQuotaExhausted):
            asyncio.run(c.domain_search("c.com"))


def test_auth_error_raises() -> None:
    c = HunterClient(api_key="bad")
    fake = _mock_response(401, {"detail": "unauthorized"})

    async def fake_get(*a, **kw):
        return fake

    with patch.object(c._http, "get", fake_get):
        import asyncio

        with pytest.raises(HunterAuthError):
            asyncio.run(c.domain_search("a.com"))


def test_quota_402_raises() -> None:
    c = HunterClient(api_key="k")
    fake = _mock_response(402, {"errors": "quota"})

    async def fake_get(*a, **kw):
        return fake

    with patch.object(c._http, "get", fake_get):
        import asyncio

        with pytest.raises(HunterQuotaExhausted):
            asyncio.run(c.domain_search("a.com"))


def test_429_retries_then_succeeds() -> None:
    c = HunterClient(api_key="k", monthly_quota=10)
    seq = [
        _mock_response(429, {}, {"Retry-After": "0.01"}),
        _mock_response(200, {"data": {"domain": "a.com", "emails": []}}),
    ]
    idx = [0]

    async def fake_get(*a, **kw):
        r = seq[min(idx[0], len(seq) - 1)]
        idx[0] += 1
        return r

    with patch.object(c._http, "get", fake_get):
        import asyncio

        r = asyncio.run(c.domain_search("a.com"))
        assert r.domain == "a.com"
        assert idx[0] == 2  # 1 fail + 1 success


def test_domain_search_parses_response() -> None:
    c = HunterClient(api_key="k")
    body = {
        "data": {
            "domain": "acme.com",
            "organization": "Acme Inc",
            "pattern": "{first}.{last}",
            "emails": [
                {
                    "value": "ceo@acme.com",
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "position": "CEO",
                    "seniority": "executive",
                    "department": "executive",
                    "confidence": 92,
                    "sources": [{"domain": "acme.com"}],
                }
            ],
        }
    }
    fake = _mock_response(200, body)

    async def fake_get(*a, **kw):
        return fake

    with patch.object(c._http, "get", fake_get):
        import asyncio

        r = asyncio.run(c.domain_search("acme.com"))
        assert isinstance(r, DomainSearchResponse)
        assert r.domain == "acme.com"
        assert r.organization == "Acme Inc"
        assert r.pattern == "{first}.{last}"
        assert len(r.emails) == 1
        e = r.emails[0]
        assert e.email == "ceo@acme.com"
        assert e.first_name == "Jane"
        assert e.position == "CEO"
        assert e.confidence == 92


def test_email_verifier_parses_response() -> None:
    c = HunterClient(api_key="k")
    body = {
        "data": {
            "status": "valid",
            "score": 90,
            "regexp": True,
            "gibberish": False,
            "disposable": False,
            "webmail": False,
            "mx_records": True,
            "smtp_server": True,
            "smtp_check": True,
            "accept_all": False,
            "block": False,
        }
    }
    fake = _mock_response(200, body)

    async def fake_get(*a, **kw):
        return fake

    with patch.object(c._http, "get", fake_get):
        import asyncio

        v = asyncio.run(c.email_verifier("test@acme.com"))
        assert isinstance(v, EmailVerifierResponse)
        assert v.status == "valid"
        assert v.is_deliverable
        assert not v.is_risky
        assert not v.block

        # risky case
        body["data"]["status"] = "risky"
        body["data"]["gibberish"] = True
        v2 = asyncio.run(c.email_verifier("foo@bar.com"))
        assert v2.is_risky
        assert not v2.is_deliverable

        # invalid case
        body["data"]["status"] = "invalid"
        body["data"]["gibberish"] = False
        v3 = asyncio.run(c.email_verifier("bad@bad.com"))
        assert not v3.is_deliverable
        assert not v3.is_risky


# ---------------------------------------------------------------------------
# find_emails_for_lead integration
# ---------------------------------------------------------------------------


def test_find_emails_no_hunter() -> None:
    """Without a Hunter client, only local extraction runs."""
    import asyncio

    text = "Reach us at ceo@acme.io or sales@acme.io"
    r = asyncio.run(
        find_emails_for_lead(website_text=text, domain="acme.io", hunter_client=None)
    )
    assert len(r) >= 1
    assert all(e.source == "local" for e in r)


def test_find_emails_hunter_fallback_adds_emails() -> None:
    """Hunter fills in what local regex missed."""
    import asyncio

    hunter = HunterClient(api_key="k")

    async def fake_get(path, params, max_retries=2):
        if "domain-search" in path:
            return {
                "data": {
                    "domain": "acme.io",
                    "emails": [
                        {
                            "value": "cfo@acme.io",
                            "first_name": "Mary",
                            "last_name": "Lee",
                            "position": "CFO",
                            "seniority": "executive",
                            "department": "finance",
                            "confidence": 90,
                            "sources": [],
                        }
                    ],
                }
            }
        return {"data": {}}

    with patch.object(hunter, "_get", fake_get):
        # website text is empty → no local results → Hunter kicks in
        r = asyncio.run(
            find_emails_for_lead(
                website_text="",
                domain="acme.io",
                first_name="",
                last_name="",
                hunter_client=hunter,
            )
        )
        emails = {e.email: e for e in r}
        assert "cfo@acme.io" in emails
        assert emails["cfo@acme.io"].source == "hunter_domain_search"
        assert emails["cfo@acme.io"].position == "CFO"


def test_find_emails_hunter_dedup_keeps_local() -> None:
    """If Hunter returns an email we already found locally, keep the local entry."""
    import asyncio

    hunter = HunterClient(api_key="k")

    async def fake_get(*a, **kw):
        return {
            "data": {
                "domain": "acme.io",
                "emails": [
                    {
                        "value": "ceo@acme.io",
                        "first_name": "",
                        "last_name": "",
                        "position": "",
                        "seniority": "",
                        "department": "",
                        "confidence": 80,
                        "sources": [],
                    }
                ],
            }
        }

    with patch.object(hunter, "_get", fake_get):
        r = asyncio.run(
            find_emails_for_lead(
                website_text="Contact ceo@acme.io",
                domain="acme.io",
                hunter_client=hunter,
            )
        )
        ceo = [e for e in r if e.email == "ceo@acme.io"]
        assert len(ceo) == 1
        assert ceo[0].source == "local"


def test_find_emails_hunter_disabled_falls_back_silently() -> None:
    """If Hunter is configured but the key is missing, local still runs."""
    import asyncio

    hunter = HunterClient(api_key="")  # disabled
    r = asyncio.run(
        find_emails_for_lead(
            website_text="hi@acme.io", domain="acme.io", hunter_client=hunter
        )
    )
    assert all(e.source == "local" for e in r)


def test_verify_email_with_hunter() -> None:
    import asyncio

    hunter = HunterClient(api_key="k")

    async def fake_get(*a, **kw):
        return {
            "data": {
                "status": "valid",
                "score": 95,
                "regexp": True,
                "gibberish": False,
                "disposable": False,
                "webmail": False,
                "mx_records": True,
                "smtp_server": True,
                "smtp_check": True,
                "accept_all": False,
                "block": False,
            }
        }

    with patch.object(hunter, "_get", fake_get):
        r = asyncio.run(verify_email("x@acme.io", hunter_client=hunter))
        assert r["status"] == "valid"
        assert r["is_deliverable"] is True
        assert r["is_risky"] is False
        assert r["mx_records"] is True


def test_verify_email_without_hunter_returns_unknown() -> None:
    import asyncio

    r = asyncio.run(verify_email("x@acme.io", hunter_client=None))
    assert r["status"] == "unknown"
    assert r["is_deliverable"] is False
    assert "error" not in r
