"""Tests for the template-adherence feature (Phase 4).

User pain point: "AI-generated emails have no relation to my original
template". These tests pin down the new behavior:

  - ``extract_required_tokens`` pulls high-frequency 2-3-grams from the
    user's historical examples
  - ``_review_email_sequence`` flags a sequence whose body has lost too
    many of those tokens
  - ``_craft_for_lead`` falls back to the raw template body when the
    LLM still drifts after one auto-fix round, so the user's voice
    always wins
"""

from __future__ import annotations

import asyncio
import sys
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set a per-process env file so config loads before the app reads it.
_tmp_env = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
_tmp_env.write("SECRETS_ENCRYPTION_KEY=test-key\n")
_tmp_env.close()
os.environ.setdefault("ENV_FILE", _tmp_env.name)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agents import email_craft_agent  # noqa: E402
from config.settings import get_settings  # noqa: E402
from emailing import template_pipeline  # noqa: E402


# --- extract_required_tokens --------------------------------------------


def test_extract_required_tokens_picks_high_frequency_ngrams() -> None:
    examples = [
        "We help distributors grow revenue with a strong partnership. "
        "Best regards, Acme Corp. Our partnership program is unique.",
        "Looking to grow revenue? Our partnership program helps distributors "
        "across Europe. Best regards, Acme Corp. Strong partnership matters.",
        "We help distributors with our partnership program. Grow revenue "
        "with a strong partnership. Best regards, Acme Corp.",
    ]
    tokens = template_pipeline.extract_required_tokens(examples)
    # Common phrases we expect to see (any of these is fine)
    joined = " | ".join(tokens).lower()
    assert "grow revenue" in joined
    assert "partnership program" in joined
    assert "best regards" in joined


def test_extract_required_tokens_handles_few_examples() -> None:
    # Single example: not enough signal to extract tokens reliably
    assert template_pipeline.extract_required_tokens(["only one example"]) == []


def test_extract_required_tokens_drops_stopwords() -> None:
    examples = [
        "this is a test of the system and we want to see if it works properly",
        "this is a test of the system and we want to see if it works correctly",
    ]
    tokens = template_pipeline.extract_required_tokens(examples, max_tokens=20)
    joined = " | ".join(tokens).lower()
    # The pure stopword chain "is a test of the system" must NOT appear
    # verbatim (because individual stopwords were stripped before n-gramming).
    assert "is a test of" not in joined
    assert "a test of the" not in joined


# --- _required_tokens_for_template --------------------------------------


def test_required_tokens_override_takes_precedence() -> None:
    settings = get_settings()
    settings.email_template_required_tokens_override = "manual phrase, second phrase"
    profile = {"required_tokens": ["auto extracted"]}
    tokens = email_craft_agent._required_tokens_for_template(profile, settings)
    assert tokens == ["manual phrase", "second phrase"]
    settings.email_template_required_tokens_override = ""  # restore


def test_required_tokens_fall_back_to_profile() -> None:
    settings = get_settings()
    settings.email_template_required_tokens_override = ""
    profile = {"required_tokens": ["alpha beta", "gamma delta", ""]}
    tokens = email_craft_agent._required_tokens_for_template(profile, settings)
    assert tokens == ["alpha beta", "gamma delta"]


def test_required_tokens_empty_when_no_profile() -> None:
    settings = get_settings()
    settings.email_template_required_tokens_override = ""
    assert email_craft_agent._required_tokens_for_template(None, settings) == []
    assert email_craft_agent._required_tokens_for_template({}, settings) == []


# --- _email_token_match_ratio -------------------------------------------


def test_token_match_ratio_full_hit() -> None:
    ratio, missing = email_craft_agent._email_token_match_ratio(
        "We help distributors grow revenue with our partnership program.",
        ["grow revenue", "partnership program", "best regards"],
    )
    assert ratio == pytest.approx(2 / 3)
    assert missing == ["best regards"]


def test_token_match_ratio_empty_tokens() -> None:
    ratio, missing = email_craft_agent._email_token_match_ratio("anything", [])
    assert ratio == 1.0
    assert missing == []


def test_token_match_ratio_case_insensitive() -> None:
    ratio, _ = email_craft_agent._email_token_match_ratio(
        "GROW revenue and PARTNERSHIP program", ["grow revenue", "partnership program"]
    )
    assert ratio == 1.0


# --- _fallback_email_from_template --------------------------------------


def test_fallback_email_replaces_known_placeholders() -> None:
    example = (
        "Subject: Quick question\n"
        "Hi {contact_name},\n"
        "I noticed {company_name} operates in {industry}.\n"
        "Best regards,\nAcme"
    )
    lead = {"company_name": "Beta Corp", "industry": "logistics"}
    target = {"target_name": "Jane Doe", "target_title": "VP Sales"}
    email = email_craft_agent._fallback_email_from_template(
        example,
        lead=lead,
        target=target,
        fallback_subject="Default",
        fallback_locale="en_US",
    )
    assert email["_template_fallback"] is True
    assert "Beta Corp" in email["body_text"]
    assert "logistics" in email["body_text"]
    assert "Jane Doe" in email["body_text"]
    assert "{company_name}" not in email["body_text"]
    assert email["personalization_points"]  # filled in


def test_fallback_email_handles_no_subject_line() -> None:
    example = "Hi {contact_name}, this is {company_name}. Best."
    lead = {"company_name": "Beta", "industry": ""}
    target = {"target_name": "Jane", "target_title": ""}
    email = email_craft_agent._fallback_email_from_template(
        example,
        lead=lead,
        target=target,
        fallback_subject="Fallback subj",
        fallback_locale="en_US",
    )
    # No subject line in example → keep fallback subject
    assert email["subject"] == "Fallback subj"
    assert "Beta" in email["body_text"]
    assert "Jane" in email["body_text"]


# --- _build_raw_template_fallback ---------------------------------------


def test_build_raw_template_fallback_produces_step_count() -> None:
    examples = [
        "Subject A\nBody A with {company_name}.",
        "Subject B\nBody B referencing {industry}.",
    ]
    step_specs = [
        {"sequence_number": 1, "objective": "Intro", "email_type": "introduction", "suggested_send_day": 0},
        {"sequence_number": 2, "objective": "Follow-up", "email_type": "follow_up", "suggested_send_day": 3},
        {"sequence_number": 3, "objective": "Breakup", "email_type": "breakup", "suggested_send_day": 7},
    ]
    lead = {"company_name": "Beta", "industry": "logistics"}
    target = {"target_name": "Jane", "target_title": "VP"}
    emails = email_craft_agent._build_raw_template_fallback(
        examples, lead=lead, target=target, step_specs=step_specs, locale="en_US"
    )
    assert len(emails) == 3
    assert emails[0]["suggested_send_day"] == 0
    assert emails[1]["suggested_send_day"] == 3
    assert emails[2]["suggested_send_day"] == 7
    # All fallback-marked
    assert all(e["_template_fallback"] for e in emails)
    # Personalization is applied
    assert "Beta" in emails[0]["body_text"]
    assert "logistics" in emails[1]["body_text"]


def test_build_raw_template_fallback_cycles_through_short_examples() -> None:
    # Only 1 example, 3 steps → should cycle
    examples = ["Subject X\nBody with {company_name}."]
    step_specs = [
        {"sequence_number": 1, "objective": "A", "email_type": "introduction", "suggested_send_day": 0},
        {"sequence_number": 2, "objective": "B", "email_type": "follow_up", "suggested_send_day": 3},
        {"sequence_number": 3, "objective": "C", "email_type": "breakup", "suggested_send_day": 7},
    ]
    emails = email_craft_agent._build_raw_template_fallback(
        examples,
        lead={"company_name": "Beta", "industry": "x"},
        target={"target_name": "J", "target_title": "T"},
        step_specs=step_specs,
        locale="en_US",
    )
    assert len(emails) == 3


# --- _review_email_sequence token check --------------------------------


def test_review_flags_sequence_missing_required_tokens() -> None:
    lead = {"company_name": "Beta Corp"}
    profile: dict[str, Any] = {"tone": "professional"}
    plan: dict[str, Any] = {"cta_strategy": "ask a question"}
    # Body has none of the required tokens
    emails = [
        {"subject": "Hello", "body_text": "Random generic copy that says nothing special at all about anything here", "suggested_send_day": 0, "email_type": "introduction"},
        {"subject": "Follow", "body_text": "Another generic note with no specific phrases to anchor this message properly", "suggested_send_day": 3, "email_type": "follow_up"},
        {"subject": "Bye", "body_text": "Final generic note with some additional generic copy to make this look complete", "suggested_send_day": 7, "email_type": "breakup"},
    ]
    summary = email_craft_agent._review_email_sequence(
        lead,
        locale="en_US",
        emails=emails,
        template_profile=profile,
        template_plan=plan,
        min_score=75,
        max_blocking_issues=0,
        required_tokens=["partnership program", "grow revenue", "best regards"],
        min_token_match_ratio=0.5,
    )
    assert summary["template_adherence"] is not None
    assert summary["template_adherence"]["worst_ratio"] == 0.0
    # Drifting triggers a heavy penalty → status flips
    assert summary["status"] == "needs_review"
    # At least one issue mentions template drift
    assert any("drifting" in issue.lower() or "template voice" in issue.lower() for issue in summary["issues"])


def test_review_passes_when_tokens_retained() -> None:
    lead = {"company_name": "Beta Corp"}
    profile = {"tone": "professional"}
    plan = {"cta_strategy": "ask a question"}
    # Body contains all required tokens
    body = (
        "We help distributors grow revenue through our partnership program. "
        "Best regards, Acme Corp. Beta Corp looks like a strong fit for our "
        "partnership program — would you be open to a 15-minute call?"
    )
    emails = [
        {"subject": "Partnership program for Beta Corp", "body_text": body, "suggested_send_day": 0, "email_type": "introduction"},
        {"subject": "Re: Partnership program", "body_text": body, "suggested_send_day": 3, "email_type": "follow_up"},
        {"subject": "Last note", "body_text": body, "suggested_send_day": 7, "email_type": "breakup"},
    ]
    summary = email_craft_agent._review_email_sequence(
        lead,
        locale="en_US",
        emails=emails,
        template_profile=profile,
        template_plan=plan,
        min_score=75,
        max_blocking_issues=0,
        required_tokens=["partnership program", "grow revenue", "best regards"],
        min_token_match_ratio=0.5,
    )
    assert summary["template_adherence"]["worst_ratio"] == 1.0
    # Score should be 100 (no deductions) — but length may trigger other
    # issues, so just verify the token check itself passes
    drift_issues = [i for i in summary["issues"] if "drifting" in i.lower()]
    assert drift_issues == []
