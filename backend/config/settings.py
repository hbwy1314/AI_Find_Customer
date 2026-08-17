"""Application settings managed via pydantic-settings."""

import os
import platform
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _app_data_dir() -> Path | None:
    """Return the user's writable app-data directory when running packaged.

    Returns None in dev mode so callers fall back to relative paths.
    """
    if not getattr(sys, "frozen", False):
        return None
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    app_dir = base / "AIHunter"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def _resolve_env_file() -> str:
    """Return the .env path to use.

    - Packaged (PyInstaller frozen binary): user app-data dir so the file
      survives app updates and is writable by the user.
    - Dev / bare Python: local .env next to the current working directory.
    """
    d = _app_data_dir()
    if d is not None:
        return str(d / ".env")
    return str(_BACKEND_ROOT / ".env")


def _resolve_dir(relative: str) -> str:
    """Resolve a writable directory path (created if missing in packaged mode)."""
    d = _app_data_dir()
    if d is not None:
        resolved = d / relative
        resolved.mkdir(parents=True, exist_ok=True)
        return str(resolved)
    resolved = _BACKEND_ROOT / relative
    resolved.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def _resolve_file(relative: str) -> str:
    """Resolve a writable file path (parent dir created if missing in packaged mode)."""
    d = _app_data_dir()
    if d is not None:
        resolved = d / relative
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return str(resolved)
    resolved = _BACKEND_ROOT / relative
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


class Settings(BaseSettings):
    """AI Hunter configuration — loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=("model_",),
    )

    # --- LLM (litellm model format: "provider/model") ---
    # Active model — uses litellm naming, e.g.:
    #   "gpt-4o"                        (OpenAI)
    #   "anthropic/claude-3-5-sonnet-20241022"  (Anthropic)
    #   "openrouter/google/gemini-pro"  (OpenRouter)
    #   "groq/llama-3.3-70b-versatile"  (Groq)
    #   "zai/glm-4.7"                   (GLM / 智谱 Z.AI)
    #   "moonshot/moonshot-v1-128k"      (Kimi / Moonshot)
    #   "minimax/MiniMax-Text-01"        (MiniMax)
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_requests_per_minute: int = 0

    # Reasoning model — used for ReAct agent decision-making (stronger reasoning)
    #   e.g. "gpt-4o", "anthropic/claude-3-5-sonnet-20241022", "openrouter/deepseek/deepseek-r1"
    reasoning_model: str = "gpt-4o"
    reasoning_temperature: float = 0.2
    reasoning_max_tokens: int = 4096
    reasoning_requests_per_minute: int = 0

    # --- LLM Provider API Keys ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    zai_api_key: str = ""         # GLM / 智谱 Z.AI
    moonshot_api_key: str = ""    # Kimi / Moonshot AI
    minimax_api_key: str = ""
    minimax_api_base: str = "https://api.minimax.io/v1"
    # Optional custom base URL for OpenAI-compatible providers (OpenAI,
    # OpenRouter, Groq, Zai, Moonshot, MiniMax, and any local/self-hosted
    # proxy that speaks the OpenAI /v1/chat/completions schema).
    # Empty = use each provider's built-in default endpoint.
    # When set, this overrides OPENAI_API_BASE and ANTHROPIC_API_BASE
    # (for Anthropic-compatible proxies like one running Claude behind
    # an OpenAI-shaped gateway).
    llm_api_base: str = ""
    email_llm_api_base: str = ""
    email_openai_api_key: str = ""
    email_anthropic_api_key: str = ""
    email_openrouter_api_key: str = ""
    email_groq_api_key: str = ""
    email_zai_api_key: str = ""
    email_moonshot_api_key: str = ""
    email_minimax_api_key: str = ""

    # --- Search ---
    serper_api_key: str = ""
    tavily_api_key: str = ""      # supports multiple keys: "key1,key2"
    jina_api_key: str = ""

    # --- Email ---
    email_provider_type: str = "smtp"
    email_from_name: str = "Ai Hunter"
    email_from_address: str = ""
    email_reply_to: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_username: str = ""
    email_smtp_password: str = ""
    email_smtp_last_test_at: str = ""
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_imap_username: str = ""
    email_imap_password: str = ""
    email_imap_last_test_at: str = ""
    email_use_tls: bool = True
    email_sequence_enabled: bool = False
    email_auto_send_enabled: bool = False
    email_step1_delay_days: int = 0
    email_step2_delay_days: int = 3
    email_step3_delay_days: int = 3
    email_business_hours_start: str = "09:00"
    email_business_hours_end: str = "18:00"
    email_weekdays_only: bool = True
    email_timezone: str = "Asia/Shanghai"

    # --- External lead-source APIs (Hunter.io for email finding/verification) ---
    # ``hunter_api_key`` is also exposed in the settings UI; we declare it
    # here too so ``get_settings().hunter_api_key`` works without
    # round-tripping through the settings_routes model.
    hunter_api_key: str = ""
    # Soft monthly quota (Hunter charges per API call beyond the free
    # tier). 0 = unlimited. ``hunter_client`` raises ``HunterQuotaExhausted``
    # once we hit this and stops calling Hunter for the rest of the month.
    hunter_monthly_quota: int = 500
    # Hard ceiling on outbound requests/second to stay well below
    # Hunter's 50/s limit and avoid bursts tripping their WAF.
    hunter_requests_per_second: int = 10

    # --- Waterfall send strategy (multi-email per lead) -----------------
    # When a sequence has multiple candidate recipients, the scheduler
    # tries them one at a time. After the first one has been sitting in
    # ``waiting_reply`` for this many days with no reply, it flips to
    # ``skipped`` and the scheduler advances to the next ``pending``
    # recipient. Set 0 to disable the waterfall (single-email legacy
    # behavior).
    email_recipient_waterfall_days: int = 3
    # Hard cap on the number of recipients per sequence. A lead with 10
    # candidate emails will only ever have the first N tried.
    email_recipient_max_per_lead: int = 3
    email_daily_send_limit: int = 20
    email_hourly_send_limit: int = 10
    email_language_mode: str = "auto_by_region"
    email_default_language: str = "en"
    email_fallback_language: str = "en"
    email_tone: str = "professional"
    email_signature_block: str = ""
    email_llm_model: str = ""
    email_reasoning_model: str = ""
    email_llm_requests_per_minute: int = 0
    email_reasoning_requests_per_minute: int = 0
    email_min_fit_score_to_send: float = 0.6
    email_min_contactability_score_to_send: float = 0.45
    email_allow_inferred_target: bool = True
    email_allow_generic_company_email: bool = False
    email_require_approval_before_send: bool = True
    email_reply_detection_enabled: bool = False
    email_reply_check_interval_seconds: int = 180
    email_template_max_send_count: int = 100
    email_template_underperforming_min_assigned: int = 10
    email_template_underperforming_min_reply_rate: float = 1.0
    email_review_min_score: int = 75
    email_review_max_blocking_issues: int = 0
    email_validation_max_revisions: int = 2
    email_review_auto_fix_rounds: int = 2
    # How many emails each outreach sequence contains (1-3). Default 1:
    # only the first company-intro email is generated and scheduled.
    email_sequence_steps: int = 1
    # When true, every lead gets its own fully independent email draft
    # (per-company personalization) instead of reusing a group template
    # with token substitution. Costs more LLM calls, produces emails
    # that are actually tailored to each target company.
    email_personalize_per_lead: bool = True

    # --- Langfuse (observability) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    langfuse_enabled: bool = False

    # --- Hunt defaults ---
    default_target_lead_count: int = 200
    default_max_rounds: int = 10
    default_keywords_per_round: int = 8
    min_new_leads_threshold: int = 5  # stop if fewer new leads per round

    # --- Concurrency ---
    search_concurrency: int = 10  # max concurrent Serper API calls
    scrape_concurrency: int = 5   # max concurrent Jina Reader calls
    email_gen_concurrency: int = 3  # max concurrent LLM calls for email generation
    react_max_iterations: int = 5   # max ReAct loop iterations per URL

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    api_access_token: str = ""
    settings_api_enabled: bool = True

    # --- Database (checkpointer) ---
    # In packaged mode, redirected to ~/Library/Application Support/AIHunter/
    checkpoint_db_path: str = _resolve_file("hunt_sessions.db")
    email_db_path: str = _resolve_file("email_automation.db")
    automation_queue_db_path: str = _resolve_file("automation_queue.db")
    template_seed_cache_path: str = _resolve_file("template_seed_cache.json")
    automation_feishu_webhook_url: str = ""
    automation_summary_enabled: bool = False
    automation_summary_interval_seconds: int = 7200
    automation_alerts_enabled: bool = False
    automation_alert_interval_seconds: int = 1800
    automation_alert_backlog_threshold: int = 20
    automation_alert_failed_messages_threshold: int = 10
    # Inbound-reply push notifications. When True (default) and a Feishu
    # webhook is configured, every reply matched by the scheduler is
    # pushed to the webhook in a single batched message per poll cycle.
    # Set to False to silence reply alerts while keeping the in-app bell.
    automation_reply_notifications_enabled: bool = True
    automation_event_notifications_enabled: bool = True
    automation_discovery_batch_size: int = 5
    automation_send_batch_size: int = 10
    automation_event_flush_interval_seconds: int = 600
    automation_embedded_consumer_enabled: bool = True
    automation_template_seed_prewarm_enabled: bool = True
    automation_consumer_poll_seconds: int = 5
    automation_consumer_retry_delay_seconds: int = 120
    # A job that still fails after this many attempts is marked failed
    # permanently instead of requeued again — protects the queue from
    # infinite retry loops on non-transient errors.
    automation_consumer_max_attempts: int = 5
    automation_consumer_status_poll_seconds: int = 15
    automation_consumer_request_timeout_seconds: int = 60
    automation_consumer_auto_start_campaign: bool = True

    # --- Hunt persistence ---
    hunts_dir: str = _resolve_dir("data/hunts")  # directory for JSON hunt files

    # --- File upload ---
    upload_dir: str = _resolve_dir("uploads")
    max_upload_size_mb: int = 50

    # --- Runtime environment (dev | production) ---
    # When `production` is selected, `cookie_secure` is forced True and trusted_hosts
    # is restricted to the host portion of `public_base_url` (unless explicitly set).
    app_env: str = "dev"
    public_base_url: str = ""

    # --- Auth (server-side sessions + bcrypt + CSRF double-submit) ---
    session_secret: str = ""           # HMAC key for cookie integrity (itsdangerous)
    session_cookie_name: str = "aih_session"
    csrf_cookie_name: str = "aih_csrf"
    session_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days
    cookie_secure: bool = False        # auto-True when app_env == "production"
    cookie_samesite: str = "lax"
    trusted_hosts: list[str] = ["*"]
    # Public-facing base URL used to build absolute links (e.g. unsubscribe
    # links in outbound emails). Falls back to api.nineluan.com when empty.
    public_base_url: str = ""

    # --- Email unsubscribe (one-click links embedded in every email) ---
    # HMAC secret for signing unsubscribe tokens. Empty = auto-generate on
    # first use (persisted to the settings store). Reusing a stable secret
    # across restarts means outstanding unsubscribe links keep working.
    unsubscribe_token_secret: str = ""

    # --- Secrets at rest (Fernet 32-byte url-safe b64 key) ---
    secrets_encryption_key: str = ""

    # --- Microsoft Graph (Application permission / client_credentials) ---
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_mailbox_upn: str = ""        # shared mailbox, e.g. sales@company.com
    graph_default_scopes: str = "https://graph.microsoft.com/.default"
    # Timestamp of the last successful Graph connectivity test (mirrors
    # EMAIL_SMTP_LAST_TEST_AT / EMAIL_IMAP_LAST_TEST_AT). Recorded by the
    # settings graph-test endpoint and cleared whenever any GRAPH_* field
    # changes, so auto-send / reply detection stay gated on a verified
    # connection regardless of which provider is active.
    graph_last_test_at: str = ""

    @model_validator(mode="after")
    def _apply_production_defaults(self) -> "Settings":
        """In production mode, enforce secure cookies and tighten trusted_hosts.

        The `cookie_secure` override is gated on the env var being
        explicitly absent: setting `COOKIE_SECURE=false` in the
        environment keeps cookies usable on plain-HTTP deployments
        (e.g. behind an nginx reverse-proxy that hasn't been issued a
        cert yet). Re-enable by unsetting the env var once HTTPS is
        in place.
        """
        if str(self.app_env).lower() == "production":
            # Only force `cookie_secure = True` if the env var wasn't
            # explicitly set. pydantic-settings populates the field
            # from env before this validator runs, so checking
            # `os.environ` is a reliable "did the user ask for false?"
            # signal.
            if "COOKIE_SECURE" not in os.environ:
                self.cookie_secure = True
            if not self.trusted_hosts or self.trusted_hosts == ["*"]:
                if self.public_base_url:
                    host = urlparse(self.public_base_url).hostname or ""
                    if host:
                        self.trusted_hosts = [host]
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
