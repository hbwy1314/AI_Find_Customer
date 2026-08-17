"""Hunter.io API client — Domain Search, Email Finder, Email Verifier.

Three of Hunter's v2 endpoints are wired in (Discover is intentionally NOT
used — it's expensive, overlaps with Brave/Tavily search, and we already
have cheaper signals from the lead pipeline).

Authentication: every request carries ``?api_key=...`` (Hunter's only auth
mode). The key is read from settings at call time so operators can rotate
it without a restart; empty key → ``HunterDisabled`` is raised so callers
can fall back to local regex without a hard error.

Rate limits: Hunter allows ~50 req/s on the v2 API. We add a sliding-window
limiter in ``HunterClient`` to keep us well under that ceiling, plus a
monthly quota counter to keep an accidental loop from racking up charges.

The client is intentionally async + stateless; create one with
``HunterClient.from_settings()`` at startup and inject it into the tools
that need it (``email_finder``, ``email_verifier``, ``lead_extract_agent``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.hunter.io/v2"

# Endpoint-level defaults
_DOMAIN_SEARCH_PATH = "/domain-search"
_EMAIL_FINDER_PATH = "/email-finder"
_EMAIL_VERIFIER_PATH = "/email-verifier"
_ACCOUNT_PATH = "/account"

# Verifier status strings Hunter returns. We map them to a small enum so
# downstream code doesn't depend on Hunter's exact wording.
_STATUS_VALID = {"valid", "accept_all"}
_STATUS_RISKY = {"risky"}
_STATUS_INVALID = {"invalid", "disposable", "unknown"}


class HunterError(Exception):
    """Base class for Hunter API errors."""


class HunterDisabled(HunterError):
    """API key not configured — caller should fall back to a non-Hunter path."""


class HunterAuthError(HunterError):
    """401 / 403 — API key is wrong or revoked."""


class HunterQuotaExhausted(HunterError):
    """Monthly quota exhausted — caller should fall back and stop calling."""


class HunterRateLimited(HunterError):
    """429 — back off and retry (the client handles this internally)."""


@dataclass
class DomainSearchResult:
    """A single email Hunter returned for a domain search."""

    email: str
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    seniority: str = ""
    department: str = ""
    confidence: int = 0  # 0-100
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DomainSearchResponse:
    domain: str
    organization: str = ""
    emails: list[DomainSearchResult] = field(default_factory=list)
    pattern: str = ""  # e.g. "{first}.{last}"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailFinderResponse:
    email: str = ""
    score: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailVerifierResponse:
    email: str
    status: str = "unknown"  # valid | accept_all | risky | invalid | disposable | unknown
    score: int = 0
    regexp_valid: bool = False
    gibberish: bool = False
    disposable: bool = False
    webmail: bool = False
    mx_records: bool = False
    smtp_server: bool = False
    smtp_check: bool = False
    accept_all: bool = False
    block: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_deliverable(self) -> bool:
        return self.status in _STATUS_VALID and not self.block

    @property
    def is_risky(self) -> bool:
        return self.status in _STATUS_RISKY or self.gibberish or self.disposable


class _SlidingWindowLimiter:
    """Simple in-process sliding-window limiter (per-key)."""

    def __init__(self, max_per_second: int) -> None:
        self._max = max(1, int(max_per_second))
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._max <= 0:
            return
        while True:
            wait = 0.0
            async with self._lock:
                now = time.monotonic()
                # drop anything older than 1s
                while self._timestamps and now - self._timestamps[0] >= 1.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                wait = 1.0 - (now - self._timestamps[0])
            if wait > 0:
                await asyncio.sleep(wait)


class HunterClient:
    """Async client for the 3 Hunter v2 endpoints we use.

    Construct via ``HunterClient.from_settings()`` so the API key + quota
    config stay in one place.
    """

    def __init__(
        self,
        api_key: str,
        *,
        requests_per_second: int = 10,
        monthly_quota: int = 500,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._monthly_quota = max(0, int(monthly_quota))
        self._calls_this_month = 0
        self._month_started_at = time.gmtime()
        self._limiter = _SlidingWindowLimiter(requests_per_second)
        self._http = httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http = True

    @classmethod
    def from_settings(cls) -> "HunterClient":
        s = get_settings()
        key = str(getattr(s, "hunter_api_key", "") or "").strip()
        quota = int(getattr(s, "hunter_monthly_quota", 500) or 500)
        rps = int(getattr(s, "hunter_requests_per_second", 10) or 10)
        return cls(api_key=key, requests_per_second=rps, monthly_quota=quota)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Quota bookkeeping
    # ------------------------------------------------------------------

    def _reset_quota_if_new_month(self) -> None:
        gm = time.gmtime()
        if gm.tm_year != self._month_started_at.tm_year or gm.tm_mon != self._month_started_at.tm_mon:
            self._month_started_at = gm
            self._calls_this_month = 0

    def _check_quota(self) -> None:
        self._reset_quota_if_new_month()
        if self._monthly_quota > 0 and self._calls_this_month >= self._monthly_quota:
            raise HunterQuotaExhausted(
                f"Hunter monthly quota exhausted ({self._calls_this_month}/{self._monthly_quota})"
            )

    def _bump_quota(self) -> None:
        self._reset_quota_if_new_month()
        self._calls_this_month += 1

    @property
    def quota_remaining(self) -> int:
        self._reset_quota_if_new_month()
        return max(0, self._monthly_quota - self._calls_this_month)

    # ------------------------------------------------------------------
    # Core HTTP
    # ------------------------------------------------------------------

    def _require_key(self) -> str:
        if not self._api_key:
            raise HunterDisabled("HUNTER_API_KEY not configured")
        return self._api_key

    async def _get(self, path: str, params: dict[str, Any], *, max_retries: int = 2) -> dict[str, Any]:
        params = {**params, "api_key": self._require_key()}
        self._check_quota()
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(f"{_BASE_URL}{path}", params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
            if resp.status_code == 401 or resp.status_code == 403:
                raise HunterAuthError(f"Hunter auth failed: {resp.text[:200]}")
            if resp.status_code == 429:
                # Honor Retry-After if present, otherwise 1s backoff.
                retry_after = float(resp.headers.get("Retry-After", "1") or "1")
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_after, 5.0))
                    continue
                raise HunterRateLimited("Hunter returned 429 repeatedly")
            if resp.status_code == 402:
                # Hunter uses 402 for "payment required / quota exhausted"
                raise HunterQuotaExhausted(f"Hunter returned 402: {resp.text[:200]}")
            if resp.status_code >= 500:
                last_exc = HunterError(f"Hunter 5xx: {resp.status_code} {resp.text[:200]}")
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise last_exc
            if resp.status_code != 200:
                raise HunterError(f"Hunter {resp.status_code}: {resp.text[:200]}")
            self._bump_quota()
            data = resp.json()
            if not isinstance(data, dict):
                raise HunterError("Hunter returned non-dict body")
            return data
        # unreachable
        raise last_exc or HunterError("Hunter request failed without an exception")

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def domain_search(
        self,
        domain: str,
        *,
        limit: int = 10,
        offset: int = 0,
        seniority: str = "",
        department: str = "",
    ) -> DomainSearchResponse:
        """Return the emails Hunter has seen for ``domain``.

        ``limit`` is capped at 100 (Hunter's max). Use ``offset`` to page.
        """
        domain = (domain or "").strip().lower()
        if not domain:
            raise ValueError("domain is required")
        params: dict[str, Any] = {"domain": domain, "limit": min(int(limit), 100)}
        if offset:
            params["offset"] = int(offset)
        if seniority:
            params["seniority"] = seniority
        if department:
            params["department"] = department
        data = await self._get(_DOMAIN_SEARCH_PATH, params)
        emails_raw = data.get("data", {}).get("emails") or []
        out: list[DomainSearchResult] = []
        for e in emails_raw:
            value = e.get("value", "")
            if not value:
                continue
            out.append(
                DomainSearchResult(
                    email=str(value),
                    first_name=str(e.get("first_name", "") or ""),
                    last_name=str(e.get("last_name", "") or ""),
                    position=str(e.get("position", "") or ""),
                    seniority=str(e.get("seniority", "") or ""),
                    department=str(e.get("department", "") or ""),
                    confidence=int(e.get("confidence", 0) or 0),
                    sources=list(e.get("sources") or []),
                )
            )
        pattern = str(data.get("data", {}).get("pattern", "") or "")
        org = str(data.get("data", {}).get("organization", "") or "")
        return DomainSearchResponse(
            domain=domain,
            organization=org,
            emails=out,
            pattern=pattern,
            raw=data,
        )

    async def email_finder(
        self,
        *,
        domain: str,
        first_name: str,
        last_name: str,
    ) -> EmailFinderResponse:
        """Construct / look up the most likely email for a person at a domain."""
        domain = (domain or "").strip().lower()
        if not domain:
            raise ValueError("domain is required")
        if not (first_name or last_name):
            raise ValueError("first_name and/or last_name required")
        params: dict[str, Any] = {
            "domain": domain,
            "first_name": first_name or "",
            "last_name": last_name or "",
        }
        data = await self._get(_EMAIL_FINDER_PATH, params)
        d = data.get("data", {}) or {}
        return EmailFinderResponse(
            email=str(d.get("email", "") or ""),
            score=int(d.get("score", 0) or 0),
            sources=list(d.get("sources") or []),
            raw=data,
        )

    async def email_verifier(self, email: str) -> EmailVerifierResponse:
        """Verify deliverability of a single email address."""
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("email is required")
        data = await self._get(_EMAIL_VERIFIER_PATH, {"email": email})
        d = data.get("data", {}) or {}
        # Hunter's "result" is the high-level bucket; "score" is 0-100.
        # We also surface individual checks for downstream tuning.
        sources = []
        for s in d.get("sources") or []:
            domain = s.get("domain") if isinstance(s, dict) else None
            if domain:
                sources.append({"domain": str(domain)})
        return EmailVerifierResponse(
            email=email,
            status=str(d.get("status", "unknown") or "unknown").lower(),
            score=int(d.get("score", 0) or 0),
            regexp_valid=bool(d.get("regexp", False)),
            gibberish=bool(d.get("gibberish", False)),
            disposable=bool(d.get("disposable", False)),
            webmail=bool(d.get("webmail", False)),
            mx_records=bool(d.get("mx_records", False)),
            smtp_server=bool(d.get("smtp_server", False)),
            smtp_check=bool(d.get("smtp_check", False)),
            accept_all=bool(d.get("accept_all", False)),
            block=bool(d.get("block", False)),
            sources=sources,
            raw=data,
        )

    async def account_info(self) -> dict[str, Any]:
        """Hit /account to learn remaining quota + plan tier. Called once
        per day by the automation notifier."""
        data = await self._get(_ACCOUNT_PATH, {})
        return data.get("data", {}) or {}
