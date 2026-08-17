"""Email finder helpers — extract email addresses from raw text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Common email patterns found on web pages
_EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# Filter out common false positives
_BLACKLIST_DOMAINS = {
    "example.com", "example.org", "test.com", "sentry.io",
    "wixpress.com", "w3.org", "schema.org", "googleapis.com",
    "cloudflare.com", "gravatar.com",
}

_BLACKLIST_PREFIXES = {
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "webmaster", "hostmaster", "abuse",
}


def _is_valid_email(email: str) -> bool:
    """Filter out common false-positive emails."""
    email = email.lower().strip()
    local, _, domain = email.partition("@")

    if domain in _BLACKLIST_DOMAINS:
        return False
    if local in _BLACKLIST_PREFIXES:
        return False
    if domain.endswith(".png") or domain.endswith(".jpg") or domain.endswith(".gif"):
        return False
    return True


def extract_emails_from_text(text: str) -> list[str]:
    """Extract unique valid emails from raw text using regex."""
    raw = _EMAIL_REGEX.findall(text)
    seen = set()
    result = []
    for email in raw:
        email_lower = email.lower().strip()
        if email_lower not in seen and _is_valid_email(email_lower):
            seen.add(email_lower)
            result.append(email_lower)
    return result


# ---------------------------------------------------------------------------
# Hunter.io-backed finder
# ---------------------------------------------------------------------------


@dataclass
class FoundEmail:
    """One email with the source it came from (local regex or Hunter API)."""

    email: str
    source: str  # "local" | "hunter_domain_search" | "hunter_email_finder"
    confidence: int = 0
    position: str = ""
    first_name: str = ""
    last_name: str = ""
    domain: str = ""


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower().strip() if "@" in email else ""


async def find_emails_for_lead(
    *,
    website_text: str = "",
    domain: str = "",
    first_name: str = "",
    last_name: str = "",
    hunter_client: Any | None = None,
    max_results: int = 5,
) -> list[FoundEmail]:
    """Best-effort email discovery for one lead.

    Strategy (in order, deduplicated):
      1. Local regex on ``website_text`` (cheap, always available)
      2. Hunter Domain Search for ``domain`` (if a Hunter client is
         provided AND the local pass didn't find anything useful AND
         ``domain`` is non-empty)
      3. Hunter Email Finder for ``first_name`` + ``last_name`` (same
         gating as #2)

    All Hunter calls are best-effort: any Hunter error is logged and
    the function returns whatever it already has. Caller is expected to
    have already constructed ``hunter_client`` (``HunterClient.from_settings()``).
    """
    found: dict[str, FoundEmail] = {}

    # 1. Local regex (always)
    if website_text:
        for raw in extract_emails_from_text(website_text):
            e = raw.lower().strip()
            if e and e not in found:
                found[e] = FoundEmail(
                    email=e, source="local", domain=_domain_of(e)
                )

    if len(found) >= max_results or hunter_client is None:
        return list(found.values())[:max_results]

    domain_norm = (domain or "").strip().lower()
    if not domain_norm:
        return list(found.values())[:max_results]

    # 2. Hunter Domain Search (if not enough local results)
    try:
        from tools.hunter_client import HunterDisabled, HunterError

        result = await hunter_client.domain_search(domain_norm, limit=max_results)
        for hit in result.emails:
            e = hit.email.lower().strip()
            if not e or e in found:
                continue
            found[e] = FoundEmail(
                email=e,
                source="hunter_domain_search",
                confidence=hit.confidence,
                position=hit.position,
                first_name=hit.first_name,
                last_name=hit.last_name,
                domain=_domain_of(e),
            )
    except HunterDisabled:
        # No key configured — that's fine, we just skip the Hunter path.
        pass
    except HunterError as exc:
        logger.warning("Hunter domain_search failed for %s: %s", domain_norm, exc)

    if len(found) >= max_results:
        return list(found.values())[:max_results]

    # 3. Hunter Email Finder (only if we know a name)
    if first_name or last_name:
        try:
            from tools.hunter_client import HunterError as _HE

            fr = await hunter_client.email_finder(
                domain=domain_norm,
                first_name=first_name,
                last_name=last_name,
            )
            e = (fr.email or "").lower().strip()
            if e and e not in found:
                found[e] = FoundEmail(
                    email=e,
                    source="hunter_email_finder",
                    confidence=fr.score,
                    first_name=first_name,
                    last_name=last_name,
                    domain=_domain_of(e),
                )
        except _HE as exc:
            logger.warning(
                "Hunter email_finder failed for %s/%s %s: %s",
                first_name, last_name, domain_norm, exc,
            )

    return list(found.values())[:max_results]


async def verify_email(
    email: str,
    *,
    hunter_client: Any | None = None,
) -> dict[str, Any]:
    """Verify a single email via Hunter (if a client is provided).

    Returns a flat dict with the verifier's status, score, and the
    individual checks. If Hunter is unavailable (no key, quota, or
    network error), the function returns ``{"status": "unknown",
    "error": "..."}`` so callers can fall back to local checks.
    """
    out: dict[str, Any] = {
        "email": email,
        "status": "unknown",
        "score": 0,
        "is_deliverable": False,
        "is_risky": False,
    }
    if hunter_client is None:
        return out
    try:
        from tools.hunter_client import HunterError

        v = await hunter_client.email_verifier(email)
        out["status"] = v.status
        out["score"] = v.score
        out["is_deliverable"] = v.is_deliverable
        out["is_risky"] = v.is_risky
        out["gibberish"] = v.gibberish
        out["disposable"] = v.disposable
        out["webmail"] = v.webmail
        out["mx_records"] = v.mx_records
        out["smtp_check"] = v.smtp_check
        out["block"] = v.block
        return out
    except HunterError as exc:
        out["error"] = str(exc)
        return out
