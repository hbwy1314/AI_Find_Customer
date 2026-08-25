import { SettingsSubPage } from "./settings-sub-page";
import { LLMProviderPanel, MAIN_API_KEY_FIELDS, EMAIL_API_KEY_FIELDS } from "./settings-panels";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";

const SAVE_KEYS: readonly string[] = [
  ...Object.values(MAIN_API_KEY_FIELDS),
  ...Object.values(EMAIL_API_KEY_FIELDS),
  "llm_model", "reasoning_model", "llm_api_base",
  "email_llm_model", "email_reasoning_model", "email_llm_api_base",
  "llm_system_prompt_override", "llm_system_prompt_enabled",
];

/**
 * Platform-level system-prompt override panel.
 *
 * When the toggle is on, the textarea contents are appended to every
 * agent's system message at LLM-call time (with a "highest priority"
 * marker) and apply to both the main pipeline and the email pipeline.
 * Useful for forcing JSON output, brand voice, banned phrases, or
 * safety guardrails across the entire product without editing code.
 */
function PlatformOverridePanel({
  values,
  handleChange,
}: {
  values: Record<string, string>;
  handleChange: (key: string, value: string) => void;
}) {
  const enabled = String(values.llm_system_prompt_enabled || "").toLowerCase() === "true";
  return (
    <Card>
      <CardHeader>
        <CardTitle>平台级系统 Prompt 覆盖</CardTitle>
        <CardDescription>
          启用后,这里填写的内容会拼接到<strong>所有 Agent</strong>(含 ReAct 循环)的 system message 末尾,
          以"最高优先级"标注,作用于主链路与邮件生成。空内容或关闭开关 = 无任何影响。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-2">
          <input
            id="llm_system_prompt_enabled"
            type="checkbox"
            className="h-4 w-4 rounded border-gray-300"
            checked={enabled}
            onChange={(e) =>
              handleChange("llm_system_prompt_enabled", e.target.checked ? "true" : "false")
            }
          />
          <Label htmlFor="llm_system_prompt_enabled" className="cursor-pointer">
            启用平台级系统 Prompt 覆盖
          </Label>
        </div>
        <div className="space-y-2">
          <Label htmlFor="llm_system_prompt_override">覆盖内容</Label>
          <textarea
            id="llm_system_prompt_override"
            className="w-full min-h-[160px] rounded-md border border-input bg-background px-3 py-2 text-sm font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            placeholder={"例:所有回复必须用简体中文,且以 JSON 形式输出,禁止任何解释文本。"}
            disabled={!enabled}
            value={values.llm_system_prompt_override || ""}
            onChange={(e) => handleChange("llm_system_prompt_override", e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            改动立即生效,无需重启服务。
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

export function LLMSettingsPage() {
  return (
    <SettingsSubPage
      title="AI 模型配置"
      description="主链路与邮件生成使用各自的 LLM 供应商、API Key 和模型。"
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <div className="space-y-6">
          <LLMProviderPanel
            title="主链路 LLM"
            description="为线索挖掘、ReAct 决策、抽取与生成配置供应商与模型。"
            values={values}
            onChange={handleChange}
            defaultModelKey="llm_model"
            reasoningModelKey="reasoning_model"
            apiKeyFieldMap={MAIN_API_KEY_FIELDS}
            apiBaseKey="llm_api_base"
          />
          <LLMProviderPanel
            title="邮件 LLM"
            description="为邮件生成、自动修复与邮件 ReAct 单独配置，避免和主链路共用同一个 RPM。"
            values={values}
            onChange={handleChange}
            defaultModelKey="email_llm_model"
            reasoningModelKey="email_reasoning_model"
            apiKeyFieldMap={EMAIL_API_KEY_FIELDS}
            apiBaseKey="email_llm_api_base"
          />
          <PlatformOverridePanel values={values} handleChange={handleChange} />
        </div>
      )}
    </SettingsSubPage>
  );
}
