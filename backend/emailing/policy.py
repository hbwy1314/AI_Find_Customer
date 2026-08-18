"""Lead -> email target selection rules."""

from __future__ import annotations

import re
from typing import Any

# Generic / role-based local parts. These reach a shared inbox, not a
# specific person, and are treated as last-resort fallback. The LLM
# is told (in the email prompt) that replies from these are likely to
# be slow or routed by an assistant.
_GENERIC_LOCAL_PARTS = {
    # primary group
    "info", "sales", "contact", "office", "hello", "support",
    "admin", "service", "team", "enquiries", "inquiries",
    # marketing / press
    "press", "media", "marketing", "pr",
    # business ops
    "billing", "accounts", "finance", "hr", "careers", "jobs",
    "recruiting", "recruitment", "people", "operations",
    # legal / privacy / abuse
    "legal", "privacy", "abuse", "postmaster", "webmaster",
    "noreply", "no-reply", "donotreply",
    # channel partners
    "wholesale", "retail", "distributor", "partners", "partnerships",
    "resellers", "reseller", "suppliers",
    # regional / language variants
    "kontakt", "contacto", "contatto", "contactez", "kontaktieren",
}


def is_role_based_email(email: str) -> bool:
    """True when the address routes to a shared inbox rather than a
    specific person. Operators can use this to flag the lead in the
    UI and tell the LLM to expect a slower / less personal reply.
    """
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    if not local:
        return False
    if local in _GENERIC_LOCAL_PARTS:
        return True
    # Heuristics: local parts that are a single common noun with no
    # personal name pattern (no dot, no number) are treated as
    # role-based. e.g. "frontdesk", "switchboard", "reception".
    if re.match(r"^[a-z]{4,}$", local) and local not in {"john", "mike", "david"}:
        # very weak signal; only flag obvious role nouns to avoid
        # false positives on real first names.
        if local in {
            "frontdesk", "switchboard", "reception", "helpdesk",
            "concierge", "reservations", "bookings", "orders",
            "returns", "refunds", "shipping", "warehouse",
        }:
            return True
    return False


_TITLE_PRIORITIES = [
    "purchasing",
    "procurement",
    "sourcing",
    "owner",
    "ceo",
    "sales director",
    "sales manager",
    "product",
    "engineering",
    "general manager",
]


def _normalize_email(email: str) -> str:
    return re.sub(r"\s*\(inferred\)\s*$", "", str(email or ""), flags=re.I).strip().lower()


def _email_status(email: str) -> str:
    text = str(email or "").strip()
    if not text:
        return "none"
    if re.search(r"\(inferred\)\s*$", text, flags=re.I) or text.lower() == "inferred":
        return "inferred-from-pattern"
    return "verified"


def _title_rank(title: str) -> int:
    normalized = str(title or "").lower()
    for idx, keyword in enumerate(_TITLE_PRIORITIES):
        if keyword in normalized:
            return idx
    return len(_TITLE_PRIORITIES)


def choose_email_target(lead: dict[str, Any]) -> dict[str, str]:
    """Choose the best outbound target email for a lead."""
    targets = expand_email_targets(lead)
    return targets[0] if targets else {
        "target_email": "",
        "target_name": "",
        "target_title": "",
        "target_type": "none",
        "is_role_based": False,
    }


def expand_email_targets(lead: dict[str, Any]) -> list[dict[str, str]]:
    """Return all sendable recipient targets for a lead in stable priority order.

    Each target dict carries an ``is_role_based`` flag so the preview
    UI and the LLM prompt can flag shared inboxes (info@, sales@, …)
    distinctly from named decision makers.
    """
    decision_makers = lead.get("decision_makers") or []
    ranked_dm: list[tuple[int, int, dict[str, Any], str, str]] = []
    for dm in decision_makers:
        if not isinstance(dm, dict):
            continue
        email = _normalize_email(str(dm.get("email", "") or ""))
        if not email or "@" not in email:
            continue
        status = _email_status(str(dm.get("email", "") or ""))
        status_rank = 0 if status == "verified" else 1
        ranked_dm.append((status_rank, _title_rank(str(dm.get("title", "") or "")), dm, email, status))

    targets: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    if ranked_dm:
        ranked_dm.sort(key=lambda item: (item[0], item[1], item[3], item[2].get("name", "")))
        for _, _, dm, email, status in ranked_dm:
            if email in seen_emails:
                continue
            seen_emails.add(email)
            role_based = is_role_based_email(email)
            base_type = f"decision_maker_{status.replace('-', '_')}"
            targets.append({
                "target_email": email,
                "target_name": str(dm.get("name", "") or ""),
                "target_title": str(dm.get("title", "") or ""),
                "target_type": f"{base_type}_role_based" if role_based else base_type,
                "is_role_based": role_based,
            })

    company_emails = []
    for email in lead.get("emails") or []:
        normalized = _normalize_email(str(email or ""))
        if "@" not in normalized:
            continue
        local = normalized.split("@", 1)[0]
        role_based = is_role_based_email(normalized)
        # Role-based addresses come first (still want a sender target),
        # but the UI / prompt should treat them as last-resort.
        priority = 0 if role_based else 1
        company_emails.append((priority, normalized, role_based))
    if company_emails:
        company_emails.sort(key=lambda item: (item[0], item[1]))
        for priority, email, role_based in company_emails:
            if email in seen_emails:
                continue
            seen_emails.add(email)
            base_type = "role_based_email" if role_based else "company_email"
            targets.append({
                "target_email": email,
                "target_name": str(lead.get("contact_person", "") or ""),
                "target_title": "",
                "target_type": base_type,
                "is_role_based": role_based,
            })

    if not targets:
        return [{
            "target_email": "",
            "target_name": "",
            "target_title": "",
            "target_type": "none",
            "is_role_based": False,
        }]
    return targets
