/**
 * Shared hook + helpers for the /settings sub-pages.
 *
 * Every sub-page (smtp, llm, search, graph, notifications, performance) loads
 * the same /api/settings payload and saves back a subset. The shape and the
 * `masked` flag handling are identical — they live here.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

// Single source of truth for env_key → frontend key mapping. The backend
// returns env-var names; we surface camelCase names to the rest of the UI.
export const SETTINGS_KEY_MAP: Record<string, string> = {
  LLM_MODEL: "llm_model",
  REASONING_MODEL: "reasoning_model",
  LLM_API_BASE: "llm_api_base",
  EMAIL_LLM_MODEL: "email_llm_model",
  EMAIL_REASONING_MODEL: "email_reasoning_model",
  EMAIL_LLM_API_BASE: "email_llm_api_base",
  OPENAI_API_KEY: "openai_api_key",
  ANTHROPIC_API_KEY: "anthropic_api_key",
  OPENROUTER_API_KEY: "openrouter_api_key",
  GROQ_API_KEY: "groq_api_key",
  ZAI_API_KEY: "zai_api_key",
  MOONSHOT_API_KEY: "moonshot_api_key",
  MINIMAX_API_KEY: "minimax_api_key",
  EMAIL_OPENAI_API_KEY: "email_openai_api_key",
  EMAIL_ANTHROPIC_API_KEY: "email_anthropic_api_key",
  EMAIL_OPENROUTER_API_KEY: "email_openrouter_api_key",
  EMAIL_GROQ_API_KEY: "email_groq_api_key",
  EMAIL_ZAI_API_KEY: "email_zai_api_key",
  EMAIL_MOONSHOT_API_KEY: "email_moonshot_api_key",
  EMAIL_MINIMAX_API_KEY: "email_minimax_api_key",
  SERPER_API_KEY: "serper_api_key",
  TAVILY_API_KEY: "tavily_api_key",
  JINA_API_KEY: "jina_api_key",
  AMAP_API_KEY: "amap_api_key",
  BAIDU_API_KEY: "baidu_api_key",
  HUNTER_API_KEY: "hunter_api_key",
  EMAIL_PROVIDER_TYPE: "email_provider_type",
  EMAIL_FROM_NAME: "email_from_name",
  EMAIL_FROM_ADDRESS: "email_from_address",
  EMAIL_REPLY_TO: "email_reply_to",
  EMAIL_SMTP_HOST: "email_smtp_host",
  EMAIL_SMTP_PORT: "email_smtp_port",
  EMAIL_SMTP_USERNAME: "email_smtp_username",
  EMAIL_SMTP_PASSWORD: "email_smtp_password",
  EMAIL_SMTP_LAST_TEST_AT: "email_smtp_last_test_at",
  EMAIL_IMAP_HOST: "email_imap_host",
  EMAIL_IMAP_PORT: "email_imap_port",
  EMAIL_IMAP_USERNAME: "email_imap_username",
  EMAIL_IMAP_PASSWORD: "email_imap_password",
  EMAIL_IMAP_LAST_TEST_AT: "email_imap_last_test_at",
  EMAIL_USE_TLS: "email_use_tls",
  EMAIL_AUTO_SEND_ENABLED: "email_auto_send_enabled",
  EMAIL_REPLY_DETECTION_ENABLED: "email_reply_detection_enabled",
  EMAIL_REPLY_CHECK_INTERVAL_SECONDS: "email_reply_check_interval_seconds",
  EMAIL_LLM_REQUESTS_PER_MINUTE: "email_llm_requests_per_minute",
  EMAIL_REASONING_REQUESTS_PER_MINUTE: "email_reasoning_requests_per_minute",
  EMAIL_REQUIRE_APPROVAL_BEFORE_SEND: "email_require_approval_before_send",
  AUTOMATION_FEISHU_WEBHOOK_URL: "automation_feishu_webhook_url",
  AUTOMATION_SUMMARY_ENABLED: "automation_summary_enabled",
  AUTOMATION_SUMMARY_INTERVAL_SECONDS: "automation_summary_interval_seconds",
  AUTOMATION_ALERTS_ENABLED: "automation_alerts_enabled",
  AUTOMATION_ALERT_INTERVAL_SECONDS: "automation_alert_interval_seconds",
  AUTOMATION_ALERT_BACKLOG_THRESHOLD: "automation_alert_backlog_threshold",
  AUTOMATION_ALERT_FAILED_MESSAGES_THRESHOLD: "automation_alert_failed_messages_threshold",
  SEARCH_CONCURRENCY: "search_concurrency",
  SCRAPE_CONCURRENCY: "scrape_concurrency",
  GRAPH_TENANT_ID: "graph_tenant_id",
  GRAPH_CLIENT_ID: "graph_client_id",
  GRAPH_CLIENT_SECRET: "graph_client_secret",
  GRAPH_MAILBOX_UPN: "graph_mailbox_upn",
  GRAPH_DEFAULT_SCOPES: "graph_default_scopes",
};

/** Build a `Record<frontendKey, rawValue>` from the backend masked payload. */
export function valuesFromSettings(
  raw: Record<string, string> | undefined
): Record<string, string> {
  if (!raw) return {};
  const out: Record<string, string> = {};
  for (const [envKey, fieldKey] of Object.entries(SETTINGS_KEY_MAP)) {
    if (raw[envKey] !== undefined) {
      out[fieldKey] = raw[envKey];
    }
  }
  return out;
}

/**
 * Drop entries that the user did not touch (still masked as "****" from the
 * backend) so a save roundtrip doesn't overwrite real secrets with the masked
 * placeholder. The /api/settings endpoint already does this server-side, but
 * stripping here keeps payloads small.
 */
export function stripUnchangedSecrets(
  values: Record<string, string>,
  secretKeys: readonly string[]
): Record<string, string> {
  const out: Record<string, string> = { ...values };
  for (const k of secretKeys) {
    if (out[k] && out[k].includes("****")) {
      delete out[k];
    }
  }
  return out;
}

export function useSettingsForm() {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  const query = useQuery({
    queryKey: ["app-settings"],
    queryFn: api.getSettings,
  });

  useEffect(() => {
    if (query.data?.settings) {
      setValues(valuesFromSettings(query.data.settings));
    }
  }, [query.data]);

  const saveMutation = useMutation({
    mutationFn: api.saveSettings,
    onSuccess: () => {
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ["app-settings"] });
      setTimeout(() => setSaved(false), 3000);
    },
  });

  const handleChange = (key: string, value: string) => {
    setValues((prev) => {
      const next = { ...prev, [key]: value };
      // Clearing the SMTP/IMAP "last tested at" stamp when relevant fields
      // change, so the "verified" badge reverts to "unconfigured".
      if (
        [
          "email_smtp_host", "email_smtp_port", "email_smtp_username",
          "email_smtp_password", "email_use_tls",
        ].includes(key)
      ) {
        next.email_smtp_last_test_at = "";
      }
      if (
        ["email_imap_host", "email_imap_port", "email_imap_username", "email_imap_password"].includes(key)
      ) {
        next.email_imap_last_test_at = "";
      }
      return next;
    });
  };

  return {
    values,
    setValues,
    handleChange,
    isLoading: query.isLoading,
    save: saveMutation.mutate,
    isSaving: saveMutation.isPending,
    saved,
  };
}
