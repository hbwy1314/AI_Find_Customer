import { SettingsSubPage } from "./settings-sub-page";
import { LLMProviderPanel, MAIN_API_KEY_FIELDS, EMAIL_API_KEY_FIELDS } from "./settings-panels";

const SAVE_KEYS: readonly string[] = [
  ...Object.values(MAIN_API_KEY_FIELDS),
  ...Object.values(EMAIL_API_KEY_FIELDS),
  "llm_model", "reasoning_model", "llm_api_base",
  "email_llm_model", "email_reasoning_model", "email_llm_api_base",
];

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
        </div>
      )}
    </SettingsSubPage>
  );
}
