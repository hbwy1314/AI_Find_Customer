"""Settings API routes for reading and writing the user .env configuration."""

from __future__ import annotations

import asyncio
import os as _os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from automation.notifier import send_feishu_text
from config.settings import get_settings
from config.settings_store import is_configured, read_settings, update_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPayload(BaseModel):
    llm_model: str = ""
    reasoning_model: str = ""
    email_llm_model: str = ""
    email_reasoning_model: str = ""
    # Optional custom OpenAI-compatible base URL. When set, the LLM
    # client routes every provider (openai, anthropic, openrouter, groq,
    # zai, moonshot, huggingface, togetherai) through it. Empty =
    # each provider's built-in default endpoint.
    llm_api_base: str = ""
    email_llm_api_base: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    zai_api_key: str = ""
    moonshot_api_key: str = ""
    minimax_api_key: str = ""
    email_openai_api_key: str = ""
    email_anthropic_api_key: str = ""
    email_openrouter_api_key: str = ""
    email_groq_api_key: str = ""
    email_zai_api_key: str = ""
    email_moonshot_api_key: str = ""
    email_minimax_api_key: str = ""
    serper_api_key: str = ""
    tavily_api_key: str = ""
    jina_api_key: str = ""
    email_provider_type: str = ""
    amap_api_key: str = ""
    baidu_api_key: str = ""
    hunter_api_key: str = ""
    email_from_name: str = ""
    email_from_address: str = ""
    email_reply_to: str = ""
    email_smtp_last_test_at: str = ""
    email_imap_last_test_at: str = ""
    email_sequence_enabled: str = ""
    email_auto_send_enabled: str = ""
    email_step1_delay_days: str = ""
    email_step2_delay_days: str = ""
    email_step3_delay_days: str = ""
    email_business_hours_start: str = ""
    email_business_hours_end: str = ""
    email_weekdays_only: str = ""
    email_timezone: str = ""
    email_daily_send_limit: str = ""
    email_hourly_send_limit: str = ""
    email_language_mode: str = ""
    email_default_language: str = ""
    email_fallback_language: str = ""
    email_tone: str = ""
    email_signature_block: str = ""
    email_llm_requests_per_minute: str = ""
    email_reasoning_requests_per_minute: str = ""
    email_min_fit_score_to_send: str = ""
    email_min_contactability_score_to_send: str = ""
    email_allow_inferred_target: str = ""
    email_allow_generic_company_email: str = ""
    email_require_approval_before_send: str = ""
    email_reply_detection_enabled: str = ""
    email_reply_check_interval_seconds: str = ""
    email_template_max_send_count: str = ""
    email_template_underperforming_min_assigned: str = ""
    email_template_underperforming_min_reply_rate: str = ""
    automation_feishu_webhook_url: str = ""
    automation_summary_enabled: str = ""
    automation_summary_interval_seconds: str = ""
    automation_alerts_enabled: str = ""
    automation_alert_interval_seconds: str = ""
    automation_alert_backlog_threshold: str = ""
    automation_alert_failed_messages_threshold: str = ""
    search_concurrency: str = ""
    scrape_concurrency: str = ""

    # --- Runtime / auth / Graph (added with the user-auth + Graph upgrade) ---
    app_env: str = ""
    public_base_url: str = ""
    session_secret: str = ""
    session_ttl_seconds: str = ""
    secrets_encryption_key: str = ""
    trusted_hosts: str = ""
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_mailbox_upn: str = ""
    graph_default_scopes: str = ""


class SettingsResponse(BaseModel):
    settings: dict[str, str]
    is_configured: bool


class ActivateRequest(BaseModel):
    license_key: str = ""
    machine_label: str = ""


def _ensure_settings_api_enabled() -> None:
    if get_settings().settings_api_enabled:
        return
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings API is disabled")


class SaveTokenRequest(BaseModel):
    token: str
    expires_at: str | None = None


class LicenseStatusResponse(BaseModel):
    status: Literal["valid"]
    message: str
    plan: str
    customer_name: str
    expires_at: str | None


class GraphTestResponse(BaseModel):
    status: str
    message: str
    mailbox: str
    upn: str
    display_name: str = ""


class FeishuTestResponse(BaseModel):
    status: str
    message: str
    webhook_url: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Settings routes ───────────────────────────────────────────────────────────
@router.get("", response_model=SettingsResponse)
async def get_settings_api():
    """Return current settings with sensitive values partially masked."""
    _ensure_settings_api_enabled()
    raw = read_settings()
    masked = {}
    for key, value in raw.items():
        if key in _SECRET_KEYS:
            masked[key] = _mask(value)
        else:
            masked[key] = value
    return SettingsResponse(
        settings=masked,
        is_configured=is_configured(),
    )


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def save_settings(payload: SettingsPayload):
    """Save settings to the user's .env file. Empty strings are skipped."""
    _ensure_settings_api_enabled()
    provided_fields = payload.model_dump(exclude_unset=True)
    field_map = {
        "llm_model": "LLM_MODEL",
        "reasoning_model": "REASONING_MODEL",
        "llm_api_base": "LLM_API_BASE",
        "email_llm_model": "EMAIL_LLM_MODEL",
        "email_reasoning_model": "EMAIL_REASONING_MODEL",
        "email_llm_api_base": "EMAIL_LLM_API_BASE",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "groq_api_key": "GROQ_API_KEY",
        "zai_api_key": "ZAI_API_KEY",
        "moonshot_api_key": "MOONSHOT_API_KEY",
        "minimax_api_key": "MINIMAX_API_KEY",
        "email_openai_api_key": "EMAIL_OPENAI_API_KEY",
        "email_anthropic_api_key": "EMAIL_ANTHROPIC_API_KEY",
        "email_openrouter_api_key": "EMAIL_OPENROUTER_API_KEY",
        "email_groq_api_key": "EMAIL_GROQ_API_KEY",
        "email_zai_api_key": "EMAIL_ZAI_API_KEY",
        "email_moonshot_api_key": "EMAIL_MOONSHOT_API_KEY",
        "email_minimax_api_key": "EMAIL_MINIMAX_API_KEY",
        "serper_api_key": "SERPER_API_KEY",
        "tavily_api_key": "TAVILY_API_KEY",
        "jina_api_key": "JINA_API_KEY",
        "email_provider_type": "EMAIL_PROVIDER_TYPE",
        "amap_api_key": "AMAP_API_KEY",
        "baidu_api_key": "BAIDU_API_KEY",
        "hunter_api_key": "HUNTER_API_KEY",
        "email_from_name": "EMAIL_FROM_NAME",
        "email_from_address": "EMAIL_FROM_ADDRESS",
        "email_reply_to": "EMAIL_REPLY_TO",
        "email_smtp_last_test_at": "EMAIL_SMTP_LAST_TEST_AT",
        "email_imap_last_test_at": "EMAIL_IMAP_LAST_TEST_AT",
        "email_sequence_enabled": "EMAIL_SEQUENCE_ENABLED",
        "email_auto_send_enabled": "EMAIL_AUTO_SEND_ENABLED",
        "email_step1_delay_days": "EMAIL_STEP1_DELAY_DAYS",
        "email_step2_delay_days": "EMAIL_STEP2_DELAY_DAYS",
        "email_step3_delay_days": "EMAIL_STEP3_DELAY_DAYS",
        "email_business_hours_start": "EMAIL_BUSINESS_HOURS_START",
        "email_business_hours_end": "EMAIL_BUSINESS_HOURS_END",
        "email_weekdays_only": "EMAIL_WEEKDAYS_ONLY",
        "email_timezone": "EMAIL_TIMEZONE",
        "email_daily_send_limit": "EMAIL_DAILY_SEND_LIMIT",
        "email_hourly_send_limit": "EMAIL_HOURLY_SEND_LIMIT",
        "email_language_mode": "EMAIL_LANGUAGE_MODE",
        "email_default_language": "EMAIL_DEFAULT_LANGUAGE",
        "email_fallback_language": "EMAIL_FALLBACK_LANGUAGE",
        "email_tone": "EMAIL_TONE",
        "email_signature_block": "EMAIL_SIGNATURE_BLOCK",
        "email_llm_requests_per_minute": "EMAIL_LLM_REQUESTS_PER_MINUTE",
        "email_reasoning_requests_per_minute": "EMAIL_REASONING_REQUESTS_PER_MINUTE",
        "email_min_fit_score_to_send": "EMAIL_MIN_FIT_SCORE_TO_SEND",
        "email_min_contactability_score_to_send": "EMAIL_MIN_CONTACTABILITY_SCORE_TO_SEND",
        "email_allow_inferred_target": "EMAIL_ALLOW_INFERRED_TARGET",
        "email_allow_generic_company_email": "EMAIL_ALLOW_GENERIC_COMPANY_EMAIL",
        "email_require_approval_before_send": "EMAIL_REQUIRE_APPROVAL_BEFORE_SEND",
        "email_reply_detection_enabled": "EMAIL_REPLY_DETECTION_ENABLED",
        "email_reply_check_interval_seconds": "EMAIL_REPLY_CHECK_INTERVAL_SECONDS",
        "email_template_max_send_count": "EMAIL_TEMPLATE_MAX_SEND_COUNT",
        "email_template_underperforming_min_assigned": "EMAIL_TEMPLATE_UNDERPERFORMING_MIN_ASSIGNED",
        "email_template_underperforming_min_reply_rate": "EMAIL_TEMPLATE_UNDERPERFORMING_MIN_REPLY_RATE",
        "automation_feishu_webhook_url": "AUTOMATION_FEISHU_WEBHOOK_URL",
        "automation_summary_enabled": "AUTOMATION_SUMMARY_ENABLED",
        "automation_summary_interval_seconds": "AUTOMATION_SUMMARY_INTERVAL_SECONDS",
        "automation_alerts_enabled": "AUTOMATION_ALERTS_ENABLED",
        "automation_alert_interval_seconds": "AUTOMATION_ALERT_INTERVAL_SECONDS",
        "automation_alert_backlog_threshold": "AUTOMATION_ALERT_BACKLOG_THRESHOLD",
        "automation_alert_failed_messages_threshold": "AUTOMATION_ALERT_FAILED_MESSAGES_THRESHOLD",
        "search_concurrency": "SEARCH_CONCURRENCY",
        "scrape_concurrency": "SCRAPE_CONCURRENCY",
        "app_env": "APP_ENV",
        "public_base_url": "PUBLIC_BASE_URL",
        "session_secret": "SESSION_SECRET",
        "session_ttl_seconds": "SESSION_TTL_SECONDS",
        "secrets_encryption_key": "SECRETS_ENCRYPTION_KEY",
        "trusted_hosts": "TRUSTED_HOSTS",
        "graph_tenant_id": "GRAPH_TENANT_ID",
        "graph_client_id": "GRAPH_CLIENT_ID",
        "graph_client_secret": "GRAPH_CLIENT_SECRET",
        "graph_mailbox_upn": "GRAPH_MAILBOX_UPN",
        "graph_default_scopes": "GRAPH_DEFAULT_SCOPES",
        "graph_last_test_at": "GRAPH_LAST_TEST_AT",
    }

    updates: dict[str, str] = {}
    graph_fields_changed = False
    for field, env_key in field_map.items():
        if field not in provided_fields:
            continue
        value = provided_fields[field]
        if isinstance(value, str) and _is_masked(value):
            continue
        updates[env_key] = str(value)
        if field in {"graph_tenant_id", "graph_client_id", "graph_client_secret", "graph_mailbox_upn"}:
            graph_fields_changed = True

    if graph_fields_changed:
        # Any credential change invalidates the previous verified test.
        updates["GRAPH_LAST_TEST_AT"] = ""

    if updates:
        update_settings(updates)
        for env_key, value in updates.items():
            _os.environ[env_key] = value

    # If Graph credentials changed, drop the cached app token so the next
    # sendMail call re-acquires it with the new credentials.
    if any(k.startswith("GRAPH_") for k in updates.keys()):
        try:
            from emailing.graph_client import reset_graph_token_cache
            reset_graph_token_cache()
        except Exception:  # noqa: BLE001
            pass

    get_settings.cache_clear()


@router.post("/email/graph-test", response_model=GraphTestResponse)
async def test_graph_settings():
    """Test Microsoft Graph connectivity using the current saved settings.

    Reads the GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET /
    GRAPH_MAILBOX_UPN from .env (already saved by /api/settings) and calls
    `GET /users/{mailbox}` to verify the app can acquire an app-only token
    and reach the mailbox. This works without first creating an email account.
    """
    from emailing import graph_client

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.graph_mailbox_upn.strip():
        raise HTTPException(
            status_code=400,
            detail="GRAPH_MAILBOX_UPN is not set. Save the Graph settings first.",
        )
    # Clear the in-memory app token so we always pick up the latest creds.
    graph_client.reset_graph_token_cache()
    try:
        result = await graph_client.test_graph_connection()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Graph test failed",
        )
    # Record the verified-test timestamp (mirrors EMAIL_SMTP_LAST_TEST_AT).
    # The campaign-start / scheduler / reply-detection gates check this so
    # Graph deployments get the same "test before auto send" protection.
    tested_at = _now_iso()
    update_settings({"GRAPH_LAST_TEST_AT": tested_at})
    _os.environ["GRAPH_LAST_TEST_AT"] = tested_at
    get_settings.cache_clear()
    return GraphTestResponse(
        status="ok",
        message="Microsoft Graph connection successful",
        mailbox=str(result.get("mailbox", "")),
        upn=str(result.get("upn", "")),
        display_name=str(result.get("display_name", "")),
    )


@router.post("/automation/feishu-test", response_model=FeishuTestResponse)
async def test_automation_feishu_webhook():
    """Send a test message to the configured Feishu webhook."""
    get_settings.cache_clear()
    settings = get_settings()
    webhook_url = str(settings.automation_feishu_webhook_url or "").strip()
    if not webhook_url:
        raise HTTPException(status_code=400, detail="Missing Feishu webhook URL in Settings")
    try:
        await asyncio.to_thread(
            send_feishu_text,
            webhook_url,
            "\n".join(
                [
                    "AI Hunter 飞书测试",
                    "这是一条测试消息。",
                    "如果你能看到这条消息，说明自动化通知 webhook 已生效。",
                ]
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FeishuTestResponse(
        status="ok",
        message="Feishu webhook test sent",
        webhook_url=webhook_url,
    )


# ── License routes ────────────────────────────────────────────────────────────

def _license_removed_response() -> LicenseStatusResponse:
    return LicenseStatusResponse(
        status="valid",
        message="License verification has been removed; all features are available.",
        plan="lifetime",
        customer_name="Local User",
        expires_at=None,
    )


@router.get("/license/status", response_model=LicenseStatusResponse)
async def license_status():
    """Return a compatibility response after license verification removal."""
    return _license_removed_response()


@router.post("/license/activate", response_model=LicenseStatusResponse)
async def activate_license(req: ActivateRequest):
    """Keep the old activation endpoint stable after license removal."""
    return _license_removed_response()


@router.post("/license/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_license():
    """Keep the old deactivation endpoint stable after license removal."""
    return None


@router.post("/license/save-token", status_code=status.HTTP_204_NO_CONTENT)
async def save_license_token(req: SaveTokenRequest):
    """Accept legacy save-token calls as a no-op after license removal."""
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────
# Settings keys whose value is a credential — must always be masked on read
# and skipped on write when the user has not provided a new value.
_SECRET_KEYS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
    "GROQ_API_KEY", "ZAI_API_KEY", "MOONSHOT_API_KEY", "MINIMAX_API_KEY",
    "EMAIL_OPENAI_API_KEY", "EMAIL_ANTHROPIC_API_KEY", "EMAIL_OPENROUTER_API_KEY",
    "EMAIL_GROQ_API_KEY", "EMAIL_ZAI_API_KEY", "EMAIL_MOONSHOT_API_KEY", "EMAIL_MINIMAX_API_KEY",
    "SERPER_API_KEY", "TAVILY_API_KEY", "JINA_API_KEY",
    "AMAP_API_KEY", "BAIDU_API_KEY", "HUNTER_API_KEY",
    "SESSION_SECRET", "SECRETS_ENCRYPTION_KEY",
    "GRAPH_CLIENT_SECRET",
}


def _mask(value: str) -> str:
    """Partially mask a secret value for display."""
    if not value or len(value) < 8:
        return value
    return value[:4] + "****" + value[-4:]


def _is_masked(value: str) -> bool:
    return "****" in value
