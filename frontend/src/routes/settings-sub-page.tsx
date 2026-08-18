/**
 * Shared layout for the /settings sub-pages.
 *
 * Each sub-page (llm / graph / search / notifications / performance)
 * uses `useSettingsForm()` from `lib/settingsForm` for state, then renders
 * a `SettingsSubPage` with its own save button.
 */

import { Link } from "@tanstack/react-router";
import { ArrowLeft, CheckCircle2, Loader2, Save } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useSettingsForm, stripUnchangedSecrets } from "@/lib/settingsForm";

const SECRET_KEYS: readonly string[] = [
  "openai_api_key", "anthropic_api_key", "openrouter_api_key",
  "groq_api_key", "zai_api_key", "moonshot_api_key", "minimax_api_key",
  "email_openai_api_key", "email_anthropic_api_key", "email_openrouter_api_key",
  "email_groq_api_key", "email_zai_api_key", "email_moonshot_api_key", "email_minimax_api_key",
  "serper_api_key", "tavily_api_key", "jina_api_key",
  "amap_api_key", "baidu_api_key", "hunter_api_key",
  "graph_client_secret", "automation_feishu_webhook_url",
];

export function SettingsSubPage({
  title,
  description,
  back,
  children,
  extraActions,
  saveKeys,
}: {
  title: string;
  description: string;
  back?: { to: string; label: string };
  children: (state: {
    values: Record<string, string>;
    handleChange: (key: string, value: string) => void;
    isLoading: boolean;
  }) => ReactNode;
  extraActions?: ReactNode;
  /**
   * Optional whitelist of keys to save. When omitted, ALL changed values
   * are persisted. Use this to scope the save to a sub-page's own fields
   * even when the same form hook loads everything.
   */
  saveKeys?: readonly string[];
}) {
  const form = useSettingsForm();
  const onSave = () => {
    const payload = stripUnchangedSecrets(form.values, SECRET_KEYS);
    if (saveKeys && saveKeys.length) {
      const filtered: Record<string, string> = {};
      for (const k of saveKeys) {
        if (k in payload) filtered[k] = payload[k];
      }
      form.save(filtered);
    } else {
      form.save(payload);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {back ? (
        <Link to={back.to} className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4 mr-1" /> {back.label}
        </Link>
      ) : null}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        <p className="text-muted-foreground mt-1">{description}</p>
      </div>
      {form.isLoading ? (
        <div className="flex items-center justify-center py-20 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin mr-2" /> 正在加载设置…
        </div>
      ) : (
        <>
          {children({
            values: form.values,
            handleChange: form.handleChange,
            isLoading: form.isLoading,
          })}
          <div className="flex items-center justify-between gap-3 pb-8">
            <div>{extraActions}</div>
            <Button
              onClick={onSave}
              disabled={form.isSaving}
              size="lg"
            >
              {form.isSaving ? (
                <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 保存中…</>
              ) : form.saved ? (
                <><CheckCircle2 className="h-4 w-4 mr-2 text-green-500" /> 已保存</>
              ) : (
                <><Save className="h-4 w-4 mr-2" /> 保存设置</>
              )}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
