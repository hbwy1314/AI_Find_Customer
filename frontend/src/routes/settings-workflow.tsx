/**
 * /settings/workflow — outbound email workflow knobs (Phases 1-4).
 *
 * Brings together the controls that were scattered between the
 * feature phase deliveries:
 *   - public_base_url: where the unsubscribe link in every email
 *     should point (defaults to https://api.nineluan.com)
 *   - Hunter.io rate + monthly budget
 *   - multi-recipient waterfall window (3d default) and cap (3)
 *   - template adherence: min-token-match threshold + raw-template
 *     fallback toggle + manual required-token override
 */

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsSubPage } from "./settings-sub-page";

const SAVE_KEYS: readonly string[] = [
  "public_base_url",
  "hunter_monthly_quota",
  "hunter_requests_per_second",
  "email_recipient_waterfall_days",
  "email_recipient_max_per_lead",
  "email_template_min_token_match_ratio",
  "email_template_fallback_enabled",
  "email_template_required_tokens_override",
];

export function WorkflowSettingsPage() {
  return (
    <SettingsSubPage
      title="邮件工作流"
      description="退订 / Hunter / 多邮箱 waterfall / 模板遵循。每一项都会影响调度器如何处理发出的邮件。"
      back={{ to: "/settings", label: "返回设置" }}
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <div className="space-y-8">
          <Section title="退订链接 (Phase 1)" hint="每封邮件底部都会自动加 List-Unsubscribe 链接，指向这里配置的域名。">
            <Field
              id="public_base_url"
              label="Public base URL"
              hint="完整 URL，例如 https://api.nineluan.com。退订页 + List-Unsubscribe 头会基于这个拼接。"
              value={values.public_base_url ?? ""}
              onChange={(v) => handleChange("public_base_url", v)}
              placeholder="https://api.nineluan.com"
            />
          </Section>

          <Section title="Hunter.io 配额 (Phase 2)" hint="留空 / 0 表示关闭 Hunter 步骤，回落到本地正则邮箱提取。">
            <Field
              id="hunter_monthly_quota"
              label="每月配额"
              hint="默认 500。新月份自动重置计数；超过后本次 hunt 跳过 Hunter。"
              value={values.hunter_monthly_quota ?? ""}
              onChange={(v) => handleChange("hunter_monthly_quota", v)}
              type="number"
              min="0"
            />
            <Field
              id="hunter_requests_per_second"
              label="每秒请求上限"
              hint="默认 10。滑窗限速，避免触发 Hunter 的 WAF。"
              value={values.hunter_requests_per_second ?? ""}
              onChange={(v) => handleChange("hunter_requests_per_second", v)}
              type="number"
              min="1"
              max="50"
            />
          </Section>

          <Section
            title="多邮箱 waterfall (Phase 3)"
            hint="同一 lead 有多个候选邮箱时按顺序尝试。N 天没回复就翻下一个。"
          >
            <Field
              id="email_recipient_waterfall_days"
              label="Waterfall 窗口（天）"
              hint="默认 3。设为 0 关闭 waterfall，回到单邮箱 legacy 行为。"
              value={values.email_recipient_waterfall_days ?? ""}
              onChange={(v) => handleChange("email_recipient_waterfall_days", v)}
              type="number"
              min="0"
              max="30"
            />
            <Field
              id="email_recipient_max_per_lead"
              label="单 lead 上限"
              hint="默认 3。一个 lead 即使有 10 个邮箱，也最多试 N 个。"
              value={values.email_recipient_max_per_lead ?? ""}
              onChange={(v) => handleChange("email_recipient_max_per_lead", v)}
              type="number"
              min="0"
              max="20"
            />
          </Section>

          <Section
            title="模板遵循 (Phase 4)"
            hint="AI 生成的邮件必须保留你历史示例中的关键短语，否则自动 fallback 到原模板。"
          >
            <Field
              id="email_template_min_token_match_ratio"
              label="Token 匹配率阈值"
              hint="默认 0.5 (50%)。生成的邮件至少要保留这个比例的 required tokens。"
              value={values.email_template_min_token_match_ratio ?? ""}
              onChange={(v) => handleChange("email_template_min_token_match_ratio", v)}
              type="number"
              min="0"
              max="1"
              step="0.05"
            />
            <ToggleField
              id="email_template_fallback_enabled"
              label="Raw template fallback"
              hint="打开时：LLM 改写 1 轮仍不达标的邮件会被替换为你的原始示例（占位符替换）。关闭则保留 LLM 输出。"
              checked={(values.email_template_fallback_enabled ?? "true").toLowerCase() !== "false"}
              onChange={(checked) => handleChange("email_template_fallback_enabled", checked ? "true" : "false")}
            />
            <Field
              id="email_template_required_tokens_override"
              label="Required tokens 覆盖（逗号分隔）"
              hint="手动指定必须保留的短语，覆盖自动从示例中提取的结果。例：partnership program, grow revenue, best regards"
              value={values.email_template_required_tokens_override ?? ""}
              onChange={(v) => handleChange("email_template_required_tokens_override", v)}
              placeholder="partnership program, grow revenue, best regards"
            />
          </Section>
        </div>
      )}
    </SettingsSubPage>
  );
}

function Section({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-sm text-muted-foreground">{hint}</p>
      </div>
      <div className="space-y-4 rounded-lg border bg-card p-4">{children}</div>
    </section>
  );
}

function Field({
  id,
  label,
  hint,
  value,
  onChange,
  type = "text",
  placeholder,
  min,
  max,
  step,
}: {
  id: string;
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  type?: "text" | "number";
  placeholder?: string;
  min?: string;
  max?: string;
  step?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        min={min}
        max={max}
        step={step}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}

function ToggleField({
  id,
  label,
  hint,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="flex items-center gap-2 cursor-pointer">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 rounded border-input"
        />
        <span className="text-sm font-medium leading-none">{label}</span>
      </label>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}
