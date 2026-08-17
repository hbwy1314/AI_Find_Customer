/**
 * /settings — configuration hub.
 *
 * The old 1300-line monolith is now split into focused sub-pages. This hub
 * renders a card grid that links to each sub-page, plus a status row at the
 * top that surfaces the most important runtime signals (provider, signup,
 * LLM ready, etc.).
 */

import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Cpu,
  Loader2,
  Bell,
  RefreshCw,
  Search,
  Inbox,
  KeyRound,
  ChevronRight,
  CircleCheck,
  CircleAlert,
  CircleDashed,
} from "lucide-react";
import { api } from "@/api/client";

type SectionCard = {
  to: string;
  title: string;
  description: string;
  icon: React.ReactNode;
  badge?: string;
  badgeTone?: "ok" | "warn" | "muted";
};

export function SettingsPage() {
  const settingsQ = useQuery({ queryKey: ["app-settings"], queryFn: api.getSettings });
  const accountsQ = useQuery({ queryKey: ["email-accounts"], queryFn: api.listEmailAccounts });
  const graphQ = useQuery({ queryKey: ["graph-config"], queryFn: api.graphConfig });
  const signupQ = useQuery({ queryKey: ["signup-status"], queryFn: api.signupStatus });

  if (settingsQ.isLoading || accountsQ.isLoading || graphQ.isLoading || signupQ.isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin mr-2" /> 正在加载设置…
      </div>
    );
  }

  const settings = settingsQ.data?.settings ?? {};
  const accounts = accountsQ.data?.accounts ?? [];
  const graph = graphQ.data;
  const signup = signupQ.data;

  const llmReady = Boolean(
    settings.LLM_MODEL || settings.REASONING_MODEL
  ) && Boolean(
    settings.OPENAI_API_KEY || settings.ANTHROPIC_API_KEY || settings.OPENROUTER_API_KEY ||
    settings.GROQ_API_KEY || settings.ZAI_API_KEY || settings.MOONSHOT_API_KEY ||
    settings.MINIMAX_API_KEY
  );

  const searchReady = Boolean(
    settings.TAVILY_API_KEY || settings.SERPER_API_KEY || settings.JINA_API_KEY
  );

  const graphReady = Boolean(
    graph?.tenant_configured && graph?.client_configured && graph?.mailbox
  );
  const feishuReady = Boolean(settings.AUTOMATION_FEISHU_WEBHOOK_URL?.trim());

  const provider = (settings.EMAIL_PROVIDER_TYPE || "graph").toLowerCase();

  const cards: SectionCard[] = [
    {
      to: "/settings/mailboxes",
      title: "邮箱账号",
      description: "管理多套发件邮箱，每套可单独测试连接和删除。Microsoft Graph 走共享邮箱 + client_credentials。",
      icon: <Inbox className="h-5 w-5 text-primary" />,
      badge: `${accounts.length} 个账号`,
      badgeTone: accounts.length > 0 ? "ok" : "muted",
    },
    {
      to: "/settings/graph",
      title: "Microsoft Graph",
      description: "用 Application 权限 + client_credentials 共用一个发件邮箱。Admin 一次性 consent 即可。",
      icon: <KeyRound className="h-5 w-5 text-primary" />,
      badge: graphReady ? "已配置" : "未配置",
      badgeTone: graphReady ? "ok" : "muted",
    },
    {
      to: "/settings/llm",
      title: "AI 模型",
      description: "主链路 + 邮件 LLM 供应商、API Key、默认模型与推理模型。",
      icon: <Cpu className="h-5 w-5 text-primary" />,
      badge: llmReady ? "已就绪" : "未配置",
      badgeTone: llmReady ? "ok" : "warn",
    },
    {
      to: "/settings/search",
      title: "搜索 API",
      description: "Tavily / Serper / Jina / Amap / Baidu / Hunter 等外部 API Key。",
      icon: <Search className="h-5 w-5 text-primary" />,
      badge: searchReady ? "已配置" : "未配置",
      badgeTone: searchReady ? "ok" : "muted",
    },
    {
      to: "/settings/notifications",
      title: "飞书通知 & 告警",
      description: "任务开始 / 失败 / 周期汇总 / 异常告警全部从这里发。",
      icon: <Bell className="h-5 w-5 text-primary" />,
      badge: feishuReady ? "已配置" : "未配置",
      badgeTone: feishuReady ? "ok" : "muted",
    },
    {
      to: "/settings/performance",
      title: "性能 & 限速",
      description: "搜索 / 抓取 / 邮件 LLM 的并发与 RPM。",
      icon: <RefreshCw className="h-5 w-5 text-primary" />,
    },
  ];

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">系统设置</h1>
        <p className="text-muted-foreground mt-1">分模块配置。每张卡片对应一个独立子页。</p>
      </div>

      {/* System status snapshot */}
      <div className="rounded-lg border bg-card p-4 text-sm">
        <p className="text-xs uppercase tracking-wide text-muted-foreground mb-3">系统状态</p>
        <div className="grid gap-3 sm:grid-cols-3">
          <StatusRow
            ok={signup?.open}
            label="注册开放"
            detail={signup?.open ? "首位注册可获 admin" : `已注册 ${signup?.user_count ?? 0} 个账号`}
          />
          <StatusRow
            ok={graphReady}
            label={`发件 Provider (${provider})`}
            detail={graphReady ? `共享邮箱 ${graph?.mailbox}` : "未配置 Microsoft Graph"}
          />
          <StatusRow
            ok={llmReady}
            label="LLM"
            detail={llmReady ? "主链路 + 模型已就绪" : "缺少主链路模型或 API Key"}
          />
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {cards.map((card) => (
          <Link
            key={card.to}
            to={card.to}
            className="group block rounded-lg border bg-card p-5 text-card-foreground shadow-sm transition-colors hover:border-primary/40 hover:bg-accent/40"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2">
                {card.icon}
                <h2 className="text-base font-semibold">{card.title}</h2>
              </div>
              <ChevronRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
            </div>
            <p className="mt-2 text-sm text-muted-foreground leading-relaxed">{card.description}</p>
            {card.badge ? (
              <div className="mt-3 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs">
                <StatusDot tone={card.badgeTone ?? "muted"} />
                <span>{card.badge}</span>
              </div>
            ) : null}
          </Link>
        ))}
      </div>
    </div>
  );
}

function StatusRow({ ok, label, detail }: { ok?: boolean; label: string; detail: string }) {
  return (
    <div className="flex items-start gap-2">
      {ok ? <CircleCheck className="h-4 w-4 mt-0.5 text-emerald-600" /> : <CircleAlert className="h-4 w-4 mt-0.5 text-amber-600" />}
      <div>
        <p className="font-medium">{label}</p>
        <p className="text-xs text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}

function StatusDot({ tone }: { tone: "ok" | "warn" | "muted" }) {
  const cls =
    tone === "ok" ? "text-emerald-600" :
    tone === "warn" ? "text-amber-600" :
    "text-slate-400";
  return <CircleDashed className={`h-3 w-3 ${cls}`} />;
}
