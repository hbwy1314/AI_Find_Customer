/**
 * Shared panels for the /settings sub-pages.
 *
 * LLMProviderPanel, FieldGroup, EmailDeliveryPanel, AutomationNotifyPanel are
 * the exact panels that previously lived inline in settings.tsx. They're
 * reusable as-is by /settings/llm, /settings/smtp, /settings/search,
 * /settings/notifications, /settings/performance.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Cpu,
  Mail,
  Bell,
  Eye,
  EyeOff,
  Loader2,
  ChevronDown,
  Check,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/api/client";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";

// ── LLM provider definitions ────────────────────────────────────────────────

type Provider = {
  id: string;
  label: string;
  apiKeyField: string;
  apiKeyPlaceholder: string;
  defaultModels: string[];
  reasoningModels: string[];
};

const PROVIDERS: Provider[] = [
  { id: "openai", label: "OpenAI", apiKeyField: "openai_api_key", apiKeyPlaceholder: "sk-…",
    defaultModels: ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"],
    reasoningModels: ["gpt-4o", "o1-mini", "o3-mini", "o1"] },
  { id: "anthropic", label: "Anthropic", apiKeyField: "anthropic_api_key", apiKeyPlaceholder: "sk-ant-…",
    defaultModels: ["anthropic/claude-3-5-haiku-20241022", "anthropic/claude-3-haiku-20240307"],
    reasoningModels: ["anthropic/claude-3-5-sonnet-20241022", "anthropic/claude-3-7-sonnet-20250219", "anthropic/claude-opus-4-5"] },
  { id: "openrouter", label: "OpenRouter", apiKeyField: "openrouter_api_key", apiKeyPlaceholder: "sk-or-…",
    defaultModels: ["openrouter/google/gemini-flash-1.5", "openrouter/mistralai/mistral-7b-instruct", "openrouter/meta-llama/llama-3.1-8b-instruct"],
    reasoningModels: ["openrouter/google/gemini-pro-1.5", "openrouter/deepseek/deepseek-r1", "openrouter/openai/gpt-4o"] },
  { id: "groq", label: "Groq", apiKeyField: "groq_api_key", apiKeyPlaceholder: "gsk_…",
    defaultModels: ["groq/llama-3.1-8b-instant", "groq/llama-3.3-70b-versatile", "groq/gemma2-9b-it"],
    reasoningModels: ["groq/llama-3.3-70b-versatile", "groq/deepseek-r1-distill-llama-70b"] },
  { id: "glm", label: "GLM / Z.AI", apiKeyField: "zai_api_key", apiKeyPlaceholder: "",
    defaultModels: ["openai/glm-4-flash", "openai/glm-4-air"],
    reasoningModels: ["openai/glm-4", "openai/glm-z1-airx"] },
  { id: "kimi", label: "Kimi (Moonshot)", apiKeyField: "moonshot_api_key", apiKeyPlaceholder: "",
    defaultModels: ["openai/moonshot-v1-8k", "openai/moonshot-v1-32k"],
    reasoningModels: ["openai/moonshot-v1-128k", "openai/kimi-k1-5"] },
  { id: "minimax", label: "MiniMax", apiKeyField: "minimax_api_key", apiKeyPlaceholder: "",
    defaultModels: ["openai/MiniMax-Text-01"],
    reasoningModels: ["openai/MiniMax-Text-01"] },
];

const MAIN_API_KEY_FIELDS: Record<string, string> = {
  openai: "openai_api_key",
  anthropic: "anthropic_api_key",
  openrouter: "openrouter_api_key",
  groq: "groq_api_key",
  glm: "zai_api_key",
  kimi: "moonshot_api_key",
  minimax: "minimax_api_key",
};

const EMAIL_API_KEY_FIELDS: Record<string, string> = {
  openai: "email_openai_api_key",
  anthropic: "email_anthropic_api_key",
  openrouter: "email_openrouter_api_key",
  groq: "email_groq_api_key",
  glm: "email_zai_api_key",
  kimi: "email_moonshot_api_key",
  minimax: "email_minimax_api_key",
};

export { MAIN_API_KEY_FIELDS, EMAIL_API_KEY_FIELDS };

function detectProvider(model: string): string {
  if (!model) return "openai";
  if (model.startsWith("anthropic/")) return "anthropic";
  if (model.startsWith("openrouter/")) return "openrouter";
  if (model.startsWith("groq/")) return "groq";
  if (model.startsWith("openai/glm")) return "glm";
  if (model.startsWith("openai/moonshot") || model.startsWith("openai/kimi")) return "kimi";
  if (model.startsWith("openai/MiniMax")) return "minimax";
  return "openai";
}

// ── Sub-components ───────────────────────────────────────────────────────────

export function SecretInput({
  id,
  value,
  onChange,
  placeholder,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <Input
        id={id}
        type={show ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="pr-10 font-mono text-sm"
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
      >
        {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

function ModelCombobox({
  id,
  value,
  onChange,
  options,
  placeholder,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const isCustom = value && !options.includes(value);

  return (
    <div ref={ref} className="relative">
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Input
            id={id}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder ?? "选择或输入模型名称…"}
            className="font-mono text-sm pr-8"
            onFocus={() => setOpen(true)}
          />
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <ChevronDown className="h-4 w-4" />
          </button>
        </div>
      </div>

      {open && (
        <div className="absolute z-50 mt-1 w-full rounded-md border bg-popover shadow-lg">
          <div className="max-h-52 overflow-y-auto py-1">
            {options.map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => { onChange(opt); setOpen(false); }}
                className="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent hover:text-accent-foreground font-mono"
              >
                {value === opt && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                <span className={value === opt ? "ml-0" : "ml-5"}>{opt}</span>
              </button>
            ))}
            <div className="px-3 py-1.5 border-t">
              <p className="text-xs text-muted-foreground">
                或直接在上方输入自定义模型名称
              </p>
            </div>
          </div>
        </div>
      )}
      {isCustom && (
        <p className="text-xs text-muted-foreground mt-1">
          ✎ 自定义模型：<span className="font-mono">{value}</span>
        </p>
      )}
    </div>
  );
}

// ── LLM Provider Panel ────────────────────────────────────────────────────────

export function LLMProviderPanel({
  title,
  description,
  values,
  onChange,
  defaultModelKey,
  reasoningModelKey,
  apiKeyFieldMap,
  apiBaseKey,
}: {
  title: string;
  description: string;
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  defaultModelKey: string;
  reasoningModelKey: string;
  apiKeyFieldMap: Record<string, string>;
  /** Settings key for the custom OpenAI-compatible base URL. */
  apiBaseKey: string;
}) {
  const currentDefaultModel = values[defaultModelKey] ?? "";
  const currentReasoningModel = values[reasoningModelKey] ?? "";
  const [providerId, setProviderId] = useState<string>(() =>
    detectProvider(currentDefaultModel || currentReasoningModel)
  );
  const provider = PROVIDERS.find((p) => p.id === providerId) ?? PROVIDERS[0];

  const handleProviderChange = (newId: string) => {
    setProviderId(newId);
    const p = PROVIDERS.find((pr) => pr.id === newId)!;
    if (!currentDefaultModel || detectProvider(currentDefaultModel) !== newId) {
      onChange(defaultModelKey, p.defaultModels[0] ?? "");
    }
    if (!currentReasoningModel || detectProvider(currentReasoningModel) !== newId) {
      onChange(reasoningModelKey, p.reasoningModels[0] ?? "");
    }
  };

  const apiKeyValue = values[apiKeyFieldMap[provider.id] ?? provider.apiKeyField] ?? "";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Cpu className="h-5 w-5 text-primary" />
          <CardTitle className="text-base">{title}</CardTitle>
        </div>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="space-y-1.5">
          <Label>LLM 供应商</Label>
          <div className="grid grid-cols-4 gap-2">
            {PROVIDERS.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => handleProviderChange(p.id)}
                className={`rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
                  providerId === p.id
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-background text-muted-foreground hover:border-primary/50 hover:text-foreground"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="llm-api-key">{provider.label} API Key</Label>
          <SecretInput
            id="llm-api-key"
            value={apiKeyValue}
            onChange={(v) => onChange(apiKeyFieldMap[provider.id] ?? provider.apiKeyField, v)}
            placeholder={provider.apiKeyPlaceholder || `输入 ${provider.label} API Key`}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="llm-api-base">
            自定义 Base URL <span className="text-muted-foreground font-normal">（可选）</span>
          </Label>
          <Input
            id="llm-api-base"
            value={values[apiBaseKey] ?? ""}
            onChange={(e) => onChange(apiBaseKey, e.target.value)}
            placeholder="https://api.openai.com/v1 或你的代理 / 本地 Ollama / LM Studio 地址"
          />
          <p className="text-xs text-muted-foreground">
            留空 = 走各供应商自带端点。填写后所有供应商（openai / anthropic / openrouter / groq / zai / moonshot / huggingface / togetherai）
            都从这个 OpenAI 兼容地址走，方便接公司代理、Azure、自建 vLLM/Ollama、Claude-OpenAI 网关等。
          </p>
        </div>
        <Separator />
        <div className="space-y-1.5">
          <Label htmlFor="llm-default-model">
            默认模型
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              — 用于提取、关键词生成、邮件生成
            </span>
          </Label>
          <ModelCombobox
            id="llm-default-model"
            value={values[defaultModelKey] ?? ""}
            onChange={(v) => onChange(defaultModelKey, v)}
            options={provider.defaultModels}
            placeholder={provider.defaultModels[0] ?? "gpt-4o-mini"}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="llm-reasoning-model">
            推理模型
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              — 用于 ReAct 决策
            </span>
          </Label>
          <ModelCombobox
            id="llm-reasoning-model"
            value={values[reasoningModelKey] ?? ""}
            onChange={(v) => onChange(reasoningModelKey, v)}
            options={provider.reasoningModels}
            placeholder={provider.reasoningModels[0] ?? "gpt-4o"}
          />
        </div>
      </CardContent>
    </Card>
  );
}

// ── Field group (search / email tools / performance) ─────────────────────────

export type FieldDef = {
  key: string;
  label: string;
  placeholder?: string;
  secret?: boolean;
  hint?: string;
};

export function FieldGroup({
  title,
  icon,
  description,
  fields,
  values,
  onChange,
}: {
  title: string;
  icon: React.ReactNode;
  description?: string;
  fields: FieldDef[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          {icon}
          <CardTitle className="text-base">{title}</CardTitle>
        </div>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-4">
        {fields.map((f) => (
          <div key={f.key} className="space-y-1.5">
            <Label htmlFor={f.key}>{f.label}</Label>
            {f.secret ? (
              <SecretInput
                id={f.key}
                value={values[f.key] ?? ""}
                onChange={(v) => onChange(f.key, v)}
                placeholder={f.placeholder}
              />
            ) : (
              <Input
                id={f.key}
                value={values[f.key] ?? ""}
                onChange={(e) => onChange(f.key, e.target.value)}
                placeholder={f.placeholder}
                className="font-mono text-sm"
              />
            )}
            {f.hint && <p className="text-xs text-muted-foreground">{f.hint}</p>}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

export const SEARCH_FIELDS: FieldDef[] = [
  { key: "tavily_api_key", label: "Tavily API Key", placeholder: "tvly-…（多 Key 用逗号分隔）", secret: true, hint: "通用网页搜索，支持多 Key：key1,key2" },
  { key: "serper_api_key", label: "Serper API Key", placeholder: "", secret: true, hint: "Google Maps 搜索及部分网页补充查询" },
  { key: "jina_api_key", label: "Jina Reader API Key", placeholder: "", secret: true, hint: "网页读取与抓取" },
  { key: "amap_api_key", label: "Amap API Key（高德）", placeholder: "", secret: true, hint: "中国区域地图搜索" },
  { key: "baidu_api_key", label: "Baidu API Key（百度）", placeholder: "", secret: true, hint: "中国区域网页搜索" },
];

export const EMAIL_FIELDS: FieldDef[] = [
  { key: "hunter_api_key", label: "Hunter.io API Key", placeholder: "", secret: true, hint: "用于 Hunter.io Domain Search / Email Finder / Verifier（填了才生效；不填时仅用本地正则）" },
];

export const CONCURRENCY_FIELDS: FieldDef[] = [
  { key: "search_concurrency", label: "搜索并发数", placeholder: "10", hint: "搜索 API 最大并发调用数" },
  { key: "scrape_concurrency", label: "抓取并发数", placeholder: "5", hint: "Jina 抓取最大并发调用数" },
  { key: "email_llm_requests_per_minute", label: "邮件生成模型 RPM", placeholder: "0", hint: "0 表示不限；用于邮件默认模型单独限速" },
  { key: "email_reasoning_requests_per_minute", label: "邮件推理模型 RPM", placeholder: "0", hint: "0 表示不限；用于邮件 ReAct / 校验模型单独限速" },
];

// ── Email Delivery Panel (SMTP/IMAP) ────────────────────────────────────────

export type EmailProviderPreset = {
  id: string;
  label: string;
  smtpHost: string;
  smtpPort: string;
  imapHost: string;
  imapPort: string;
  useTls: string;
  note: string;
};

export const EMAIL_PROVIDER_PRESETS: EmailProviderPreset[] = [
  { id: "manual", label: "手动填写", smtpHost: "", smtpPort: "", imapHost: "", imapPort: "", useTls: "true",
    note: "适合自建邮箱、地区化企业邮箱，或管理员给了专用网关地址的场景。" },
  { id: "qq", label: "QQ 邮箱", smtpHost: "smtp.qq.com", smtpPort: "465", imapHost: "imap.qq.com", imapPort: "993", useTls: "true",
    note: "通常需要在 QQ 邮箱设置里开启 IMAP/SMTP，并生成授权码。" },
  { id: "exmail_qq", label: "腾讯企业邮箱", smtpHost: "smtp.exmail.qq.com", smtpPort: "465", imapHost: "imap.exmail.qq.com", imapPort: "993", useTls: "true",
    note: "适合腾讯企业邮箱常见配置；若管理员提供专用地址，请以管理员配置为准。" },
  { id: "netease_163", label: "网易 163 邮箱", smtpHost: "smtp.163.com", smtpPort: "465", imapHost: "imap.163.com", imapPort: "993", useTls: "true",
    note: "通常需要先在邮箱安全设置里开启 IMAP/SMTP，并使用客户端授权码。" },
  { id: "netease_126", label: "网易 126 邮箱", smtpHost: "smtp.126.com", smtpPort: "465", imapHost: "imap.126.com", imapPort: "993", useTls: "true",
    note: "126 邮箱常见默认配置，授权方式通常与 163 类似。" },
  { id: "netease_qiye", label: "网易企业邮箱", smtpHost: "smtp.qiye.163.com", smtpPort: "465", imapHost: "imap.qiye.163.com", imapPort: "993", useTls: "true",
    note: "网易企业邮箱可能按区域或管理员策略使用不同地址；不一致时请以管理员提供为准。" },
  { id: "aliyun_qiye", label: "阿里云企业邮箱", smtpHost: "smtp.mxhichina.com", smtpPort: "465", imapHost: "imap.mxhichina.com", imapPort: "993", useTls: "true",
    note: "阿里云企业邮箱常见默认配置；部分企业可能由管理员提供独立服务器地址。" },
];

const EMAIL_DELIVERY_FIELDS: FieldDef[] = [
  { key: "email_from_name", label: "发件人名称", placeholder: "Ai Hunter" },
  { key: "email_from_address", label: "发件邮箱", placeholder: "sales@example.com" },
  { key: "email_reply_to", label: "回复邮箱", placeholder: "sales@example.com" },
  { key: "email_smtp_host", label: "SMTP Host", placeholder: "smtp.gmail.com" },
  { key: "email_smtp_port", label: "SMTP Port", placeholder: "587" },
  { key: "email_smtp_username", label: "SMTP 用户名", placeholder: "sales@example.com" },
  { key: "email_smtp_password", label: "SMTP 授权码 / 密码", placeholder: "", secret: true },
  { key: "email_imap_host", label: "IMAP Host", placeholder: "imap.gmail.com" },
  { key: "email_imap_port", label: "IMAP Port", placeholder: "993" },
  { key: "email_imap_username", label: "IMAP 用户名", placeholder: "sales@example.com" },
  { key: "email_imap_password", label: "IMAP 授权码 / 密码", placeholder: "", secret: true },
];

export function EmailDeliveryPanel({
  values,
  onChange,
  onPersist,
}: {
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onPersist: (payload: Record<string, string>) => Promise<void>;
}) {
  const [selectedPresetId, setSelectedPresetId] = useState("manual");
  const smtpConfigured = Boolean(
    values.email_from_address && values.email_smtp_host && values.email_smtp_port &&
    values.email_smtp_username && values.email_smtp_password
  );
  const imapConfigured = Boolean(
    values.email_imap_host && values.email_imap_port &&
    values.email_imap_username && values.email_imap_password
  );
  const smtpVerified = Boolean(values.email_smtp_last_test_at);
  const imapVerified = Boolean(values.email_imap_last_test_at);
  const smtpStatus = !smtpConfigured ? "unconfigured" : smtpVerified ? "verified" : "pending";
  const imapStatus = !imapConfigured ? "unconfigured" : imapVerified ? "verified" : "pending";

  const smtpTestMutation = useMutation({
    mutationFn: async () => {
      await onPersist(values);
      return api.testEmailSettings();
    },
    onSuccess: () => {
      onChange("email_smtp_last_test_at", new Date().toISOString());
    },
  });
  const imapTestMutation = useMutation({
    mutationFn: async () => {
      await onPersist(values);
      return api.testImapSettings();
    },
    onSuccess: () => {
      onChange("email_imap_last_test_at", new Date().toISOString());
    },
  });

  const statusMeta = {
    unconfigured: { label: "未配置", className: "border-slate-200 bg-slate-50 text-slate-900",
      description: "还缺少必要参数，当前不能进入对应自动化链路。" },
    pending: { label: "已配置未验证", className: "border-amber-200 bg-amber-50 text-amber-900",
      description: "参数已填写，但还需要点击测试连接并成功一次。" },
    verified: { label: "已验证", className: "border-emerald-200 bg-emerald-50 text-emerald-900",
      description: "最近一次连接测试成功，可以进入对应自动化链路。" },
  } as const;

  const applyProviderPreset = (preset: EmailProviderPreset) => {
    setSelectedPresetId(preset.id);
    if (preset.id === "manual") return;
    onChange("email_smtp_host", preset.smtpHost);
    onChange("email_smtp_port", preset.smtpPort);
    onChange("email_imap_host", preset.imapHost);
    onChange("email_imap_port", preset.imapPort);
    onChange("email_use_tls", preset.useTls);
    if (values.email_from_address) {
      onChange("email_smtp_username", values.email_from_address);
      onChange("email_imap_username", values.email_from_address);
      if (!values.email_reply_to) {
        onChange("email_reply_to", values.email_from_address);
      }
    }
  };

  useEffect(() => {
    const matchedPreset = EMAIL_PROVIDER_PRESETS.find((preset) => (
      preset.id !== "manual" &&
      values.email_smtp_host === preset.smtpHost &&
      values.email_smtp_port === preset.smtpPort &&
      values.email_imap_host === preset.imapHost &&
      values.email_imap_port === preset.imapPort
    ));
    setSelectedPresetId(matchedPreset?.id ?? "manual");
  }, [values.email_imap_host, values.email_imap_port, values.email_smtp_host, values.email_smtp_port]);

  const selectedPreset = EMAIL_PROVIDER_PRESETS.find((item) => item.id === selectedPresetId) ?? EMAIL_PROVIDER_PRESETS[0];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Mail className="h-5 w-5 text-primary" />
          <CardTitle className="text-base">SMTP 发信配置</CardTitle>
        </div>
        <CardDescription>
          配置企业邮箱 SMTP，用于测试连接和手动发送已批准邮件。点击测试连接时，系统会先自动保存当前表单。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className={`rounded-md border px-3 py-3 ${statusMeta[smtpStatus].className}`}>
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-semibold">SMTP 状态</p>
              <span className="rounded-full border border-current/20 px-2 py-0.5 text-xs font-medium">
                {statusMeta[smtpStatus].label}
              </span>
            </div>
            <p className="mt-2 text-xs leading-5 text-current/80">
              {statusMeta[smtpStatus].description}
            </p>
          </div>
          <div className={`rounded-md border px-3 py-3 ${statusMeta[imapStatus].className}`}>
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-semibold">IMAP 状态</p>
              <span className="rounded-full border border-current/20 px-2 py-0.5 text-xs font-medium">
                {statusMeta[imapStatus].label}
              </span>
            </div>
            <p className="mt-2 text-xs leading-5 text-current/80">
              {statusMeta[imapStatus].description}
            </p>
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>邮箱服务商模板</Label>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {EMAIL_PROVIDER_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                onClick={() => applyProviderPreset(preset)}
                className={`rounded-md border px-3 py-2 text-sm transition-colors ${
                  selectedPresetId === preset.id
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-background text-muted-foreground hover:border-primary/50 hover:text-foreground"
                }`}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">{selectedPreset.note}</p>
        </div>

        {EMAIL_DELIVERY_FIELDS.map((f) => (
          <div key={f.key} className="space-y-1.5">
            <Label htmlFor={f.key}>{f.label}</Label>
            {f.secret ? (
              <SecretInput
                id={f.key}
                value={values[f.key] ?? ""}
                onChange={(v) => onChange(f.key, v)}
                placeholder={f.placeholder}
              />
            ) : (
              <Input
                id={f.key}
                value={values[f.key] ?? ""}
                onChange={(e) => onChange(f.key, e.target.value)}
                placeholder={f.placeholder}
                className="font-mono text-sm"
              />
            )}
          </div>
        ))}

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="email_use_tls">SMTP/IMAP 启用 TLS</Label>
            <div className="flex gap-2">
              {[["true", "启用"], ["false", "关闭"]].map(([v, l]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => onChange("email_use_tls", v)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.email_use_tls ?? "true") === v
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >
                  {l}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="email_provider_type">默认发件 Provider</Label>
            <div className="flex gap-2">
              {[["smtp", "SMTP/IMAP"], ["graph", "Microsoft Graph"]].map(([v, l]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => onChange("email_provider_type", v)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.email_provider_type ?? "smtp") === v
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >
                  {l}
                </button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">SMTP 用上方配置的账号；Graph 走 <a className="underline" href="/settings/graph">/settings/graph</a> 的共享邮箱。</p>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5">
            <Label>自动发送</Label>
            <div className="flex gap-2">
              {[["true", "开启"], ["false", "关闭"]].map(([v, l]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => onChange("email_auto_send_enabled", v)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.email_auto_send_enabled ?? "false") === v
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >{l}</button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">开启后系统会在审批通过后自动发送邮件。</p>
          </div>
          <div className="space-y-1.5">
            <Label>回信检测 (IMAP)</Label>
            <div className="flex gap-2">
              {[["true", "开启"], ["false", "关闭"]].map(([v, l]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => onChange("email_reply_detection_enabled", v)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.email_reply_detection_enabled ?? "false") === v
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >{l}</button>
              ))}
            </div>
            <Input
              value={values.email_reply_check_interval_seconds ?? ""}
              onChange={(e) => onChange("email_reply_check_interval_seconds", e.target.value)}
              placeholder="180"
              className="font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">检测间隔（秒），默认 180。</p>
          </div>
        </div>

        <Separator />

        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={() => smtpTestMutation.mutate()}
            disabled={smtpTestMutation.isPending}
          >
            {smtpTestMutation.isPending ? (<><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 测试中…</>) : "测试 SMTP 连接"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => imapTestMutation.mutate()}
            disabled={imapTestMutation.isPending}
          >
            {imapTestMutation.isPending ? (<><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 测试中…</>) : "测试 IMAP 连接"}
          </Button>
          {smtpTestMutation.isError && (
            <p className="text-sm text-destructive">{smtpTestMutation.error.message}</p>
          )}
          {imapTestMutation.isError && (
            <p className="text-sm text-destructive">{imapTestMutation.error.message}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ── Automation Notify Panel (Feishu) ────────────────────────────────────────

export function AutomationNotifyPanel({
  values,
  onChange,
  onPersist,
}: {
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onPersist: (payload: Record<string, string>) => Promise<void>;
}) {
  const webhookConfigured = Boolean(values.automation_feishu_webhook_url?.trim());
  const feishuTestMutation = useMutation({
    mutationFn: async () => {
      await onPersist(values);
      return api.testFeishuWebhook();
    },
  });

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Bell className="h-5 w-5 text-primary" />
          <CardTitle className="text-base">飞书通知</CardTitle>
        </div>
        <CardDescription>
          配置飞书机器人 webhook，用于接收任务开始、失败、企业发现、发送批次和周期汇总。点击测试会先自动保存当前表单。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className={`rounded-md border px-4 py-4 text-sm ${webhookConfigured ? "border-emerald-200 bg-emerald-50 text-emerald-950" : "border-amber-200 bg-amber-50 text-amber-950"}`}>
          <p className="font-semibold">{webhookConfigured ? "Webhook 已配置" : "Webhook 未配置"}</p>
          <p className="mt-2 text-xs leading-5 opacity-90">
            {webhookConfigured
              ? "后端会使用这个地址发送实时通知和汇总。建议先点一次测试，确认群机器人已经能正常收到消息。"
              : "先在飞书群里添加自定义机器人，复制 webhook 地址填到这里，然后点击测试通知。"}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="automation_feishu_webhook_url">飞书 Webhook URL</Label>
          <SecretInput
            id="automation_feishu_webhook_url"
            value={values.automation_feishu_webhook_url ?? ""}
            onChange={(value) => onChange("automation_feishu_webhook_url", value)}
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."
          />
        </div>

        <div className="space-y-2">
          <Label>回信实时通知</Label>
          <div className="flex gap-2">
            {[["true", "开启"], ["false", "关闭"]].map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => onChange("automation_reply_notifications_enabled", value)}
                className={`rounded-md border px-3 py-2 text-sm ${
                  (values.automation_reply_notifications_enabled ?? "true") === value
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border text-muted-foreground"
                }`}
              >{label}</button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            开启后，每当回复检测匹配到一封回信，会立刻往飞书群推一条消息（多个匹配合并成一条，避免轰炸）。
            关闭后仍可在站内通知铃铛里看到。{webhookConfigured ? "" : "（提示：上方 webhook 还没配，配上才会真正发出去）"}
          </p>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label>周期汇总</Label>
            <div className="flex gap-2">
              {[["true", "开启"], ["false", "关闭"]].map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onChange("automation_summary_enabled", value)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.automation_summary_enabled ?? "true") === value
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >{label}</button>
              ))}
            </div>
            <Input
              value={values.automation_summary_interval_seconds ?? ""}
              onChange={(e) => onChange("automation_summary_interval_seconds", e.target.value)}
              placeholder="7200"
              className="font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">单位秒。测试时可先改小，生产建议 7200 秒或更长。</p>
          </div>

          <div className="space-y-2">
            <Label>异常告警</Label>
            <div className="flex gap-2">
              {[["true", "开启"], ["false", "关闭"]].map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onChange("automation_alerts_enabled", value)}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    (values.automation_alerts_enabled ?? "true") === value
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >{label}</button>
              ))}
            </div>
            <Input
              value={values.automation_alert_interval_seconds ?? ""}
              onChange={(e) => onChange("automation_alert_interval_seconds", e.target.value)}
              placeholder="1800"
              className="font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">单位秒。异常告警更适合短间隔，避免失败长期无人感知。</p>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="automation_alert_backlog_threshold">积压阈值</Label>
            <Input
              id="automation_alert_backlog_threshold"
              value={values.automation_alert_backlog_threshold ?? ""}
              onChange={(e) => onChange("automation_alert_backlog_threshold", e.target.value)}
              placeholder="20"
              className="font-mono text-sm"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="automation_alert_failed_messages_threshold">失败邮件阈值</Label>
            <Input
              id="automation_alert_failed_messages_threshold"
              value={values.automation_alert_failed_messages_threshold ?? ""}
              onChange={(e) => onChange("automation_alert_failed_messages_threshold", e.target.value)}
              placeholder="10"
              className="font-mono text-sm"
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={() => feishuTestMutation.mutate()}
            disabled={feishuTestMutation.isPending}
          >
            {feishuTestMutation.isPending ? (<><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 测试中…</>) : "测试飞书通知"}
          </Button>
          {feishuTestMutation.isSuccess && (
            <p className="text-sm text-emerald-600">已发送测试消息到 {feishuTestMutation.data.webhook_url}</p>
          )}
          {feishuTestMutation.isError && (
            <p className="text-sm text-destructive">{feishuTestMutation.error.message}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
