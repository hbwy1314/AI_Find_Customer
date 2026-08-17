"""Dataclasses for email automation state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EmailAccount:
    """Per-account send/receive config.

    All outbound and inbound mail flows through Microsoft Graph, so
    the SMTP/IMAP fields are gone. The corresponding columns remain
    in the SQLite schema for backward compat (existing rows are
    read with default empty values) but no new code touches them.
    """
    id: str
    provider_type: str
    from_name: str
    from_email: str
    reply_to: str
    # Graph fields (per-account overrides; the global
    # ``GRAPH_*`` settings are used when these are empty)
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret_encrypted: str = ""
    graph_user_principal_name: str = ""
    status: str = "active"
    daily_send_limit: int = 0
    hourly_send_limit: int = 0
    last_test_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class EmailCampaign:
    id: str
    hunt_id: str
    email_account_id: str
    name: str
    status: str
    language_mode: str
    default_language: str
    fallback_language: str
    tone: str
    step1_delay_days: int
    step2_delay_days: int
    step3_delay_days: int
    min_fit_score: float
    min_contactability_score: float
    created_at: str
    updated_at: str


@dataclass(slots=True)
class LeadEmailSequence:
    id: str
    campaign_id: str
    hunt_id: str
    lead_key: str
    lead_email: str
    lead_name: str
    decision_maker_name: str
    decision_maker_title: str
    locale: str
    status: str
    current_step: int
    stop_reason: str
    replied_at: str
    last_sent_at: str
    next_scheduled_at: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class EmailMessage:
    id: str
    sequence_id: str
    step_number: int
    goal: str
    locale: str
    subject: str
    body_text: str
    status: str
    scheduled_at: str
    sent_at: str
    provider_message_id: str
    thread_key: str
    failure_reason: str
    created_at: str
    updated_at: str

