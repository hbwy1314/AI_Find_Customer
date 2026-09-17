"""Stable lead identity and deduplication helpers shared by the pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from tools.url_filter import classify_url

_TRACKING_QUERY_PREFIXES = ("utm_", "ga_", "gclid", "fbclid", "mc_")
_LEGAL_SUFFIXES = {
    "ab", "ag", "bv", "co", "corp", "corporation", "gmbh", "inc", "incorporated",
    "kg", "limited", "llc", "ltd", "nv", "oy", "plc", "pte", "pty", "sa",
    "sas", "sp z oo", "spa", "srl", "ug",
}


def normalize_domain(value: str) -> str:
    """Normalize a URL or hostname to a lower-case IDNA hostname."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    try:
        host = str(parsed.hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def official_domain(value: str) -> str:
    """Return a domain only when the URL looks like an official company site."""
    domain = normalize_domain(value)
    if not domain:
        return ""
    raw = str(value or "").strip()
    url = raw if "://" in raw else f"https://{raw}"
    return domain if classify_url(url) == "company_site" else ""


def normalize_url(value: str) -> str:
    """Canonicalize a source URL for exact-source deduplication."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    domain = normalize_domain(raw)
    if not domain:
        return ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = "&".join(
        f"{key}={val}" if val else key
        for key, val in sorted(parse_qsl(parsed.query, keep_blank_values=True))
        if not any(key.casefold().startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES)
    )
    return urlunsplit(("https", domain, path, query, ""))


def normalize_email(value: str) -> str:
    """Normalize a plain email and ignore explicitly inferred addresses."""
    email = str(value or "").strip().casefold()
    email = re.sub(r"\s*\(inferred\)\s*$", "", email)
    if "@" not in email:
        return ""
    return email


def normalize_phone(value: str) -> str:
    """Normalize a phone to digits; short fragments are not identities."""
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits if len(digits) >= 7 else ""


def normalize_company_name(value: str) -> str:
    """Normalize company names across case, accents, punctuation, and legal suffixes."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ")
    text = "".join(char if char.isalnum() else " " for char in text)
    tokens = text.split()
    while tokens and " ".join(tokens[-2:]) in _LEGAL_SUFFIXES:
        tokens = tokens[:-2]
    while tokens and (tokens[-1] in _LEGAL_SUFFIXES or tokens[-1] == "and"):
        tokens.pop()
    return " ".join(tokens)


def _maps_data(lead: dict[str, Any]) -> dict[str, Any]:
    value = lead.get("maps_data")
    return value if isinstance(value, dict) else {}


def lead_identity_keys(lead: dict[str, Any]) -> list[str]:
    """Return all durable aliases that identify one extracted lead."""
    keys: list[str] = []
    domain = official_domain(str(lead.get("website", "") or ""))
    if domain:
        keys.append(f"domain:{domain}")

    raw_source_url = str(lead.get("source_url", "") or "")
    source_url = normalize_url(raw_source_url)
    if source_url and official_domain(raw_source_url):
        keys.append(f"source_url:{source_url}")

    maps = _maps_data(lead)
    place_id = str(maps.get("place_id") or maps.get("cid") or "").strip().casefold()
    if place_id:
        keys.append(f"place:{place_id}")

    emails = lead.get("emails") or []
    if isinstance(emails, list):
        for email in emails:
            normalized = normalize_email(str(email or ""))
            if normalized:
                keys.append(f"email:{normalized}")

    phones = lead.get("phone_numbers") or []
    if isinstance(phones, list):
        for phone in phones:
            normalized = normalize_phone(str(phone or ""))
            if normalized:
                keys.append(f"phone:{normalized}")

    # Company name is a WEAK identity: only use it as a fallback when we have
    # NO other strong identities (domain, email, phone, place_id, source_url).
    # This prevents same-name companies in different regions from incorrectly
    # deduping each other while still allowing fallback identity for leads
    # with truly no other contact info.
    if not keys:
        company = normalize_company_name(str(lead.get("company_name", "") or ""))
        if company:
            keys.append(f"company:{company}")

    if not keys:
        raw = json.dumps(lead, sort_keys=True, ensure_ascii=False, default=str)
        keys.append("raw:" + hashlib.sha256(raw.encode("utf-8")).hexdigest())
    return list(dict.fromkeys(keys))


def candidate_identity_keys(item: dict[str, Any]) -> list[str]:
    """Return identities available before an expensive deep scrape."""
    keys: list[str] = []
    maps = item.get("maps_data") if isinstance(item.get("maps_data"), dict) else {}

    website = str(maps.get("website") or item.get("link") or "")
    domain = official_domain(website)
    if domain:
        keys.append(f"domain:{domain}")

    raw_source_url = str(item.get("link", "") or "")
    source_url = normalize_url(raw_source_url)
    if source_url and official_domain(raw_source_url):
        keys.append(f"source_url:{source_url}")

    place_id = str(maps.get("place_id") or maps.get("cid") or "").strip().casefold()
    if place_id:
        keys.append(f"place:{place_id}")

    email = normalize_email(str(maps.get("email", "") or ""))
    if email:
        keys.append(f"email:{email}")

    for phone in (maps.get("phone_number"), maps.get("phoneNumber")):
        normalized = normalize_phone(str(phone or ""))
        if normalized:
            keys.append(f"phone:{normalized}")

    # Company name is a WEAK identity: only use it when we have strong
    # geographic anchors (Maps place_id or address). Without these, same-name
    # companies in different regions would incorrectly dedupe each other.
    # Already have domain/email/phone/place_id above as strong keys.
    # Titles from articles/directories are not identities.

    return list(dict.fromkeys(keys))


def _merge_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    result = list(existing)
    for item in incoming:
        if item not in result:
            result.append(item)
    return result


def _merge_lead(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Keep one row while retaining richer contact/evidence fields."""
    for field in ("emails", "phone_numbers", "decision_makers", "evidence", "customs_records"):
        left = existing.get(field) if isinstance(existing.get(field), list) else []
        right = incoming.get(field) if isinstance(incoming.get(field), list) else []
        existing[field] = _merge_list(left, right)
    for field in ("social_media",):
        left = existing.get(field) if isinstance(existing.get(field), dict) else {}
        right = incoming.get(field) if isinstance(incoming.get(field), dict) else {}
        existing[field] = {**right, **left}
    for field, value in incoming.items():
        if field not in existing or existing.get(field) in (None, "", [], {}):
            existing[field] = value
    for field in ("match_score", "fit_score", "contactability_score", "customs_score"):
        try:
            existing[field] = max(float(existing.get(field, 0) or 0), float(incoming.get(field, 0) or 0))
        except (TypeError, ValueError):
            pass


def dedupe_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate leads transitively by any shared identity alias."""
    result: list[dict[str, Any] | None] = []
    key_to_index: dict[str, int] = {}
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        keys = lead_identity_keys(lead)
        matching_indices = {key_to_index[key] for key in keys if key in key_to_index}
        if not matching_indices:
            index = len(result)
            result.append(dict(lead))
        else:
            index = min(matching_indices)
            for other_index in sorted(matching_indices - {index}):
                other = result[other_index]
                if other is None:
                    continue
                _merge_lead(result[index], other)
                result[other_index] = None
                for known_key, known_index in list(key_to_index.items()):
                    if known_index == other_index:
                        key_to_index[known_key] = index
            _merge_lead(result[index], lead)
        for key in keys:
            key_to_index[key] = index
        for key in lead_identity_keys(result[index]):
            key_to_index[key] = index
    return [lead for lead in result if lead is not None]
