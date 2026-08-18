import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Mail, Pencil, Plus, Trash2 } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { api, type EmailAccountRow, type EmailAccountPayload } from "../api/client";
import { EmptyState, LoadingState } from "@/components/data-states";

function GraphConfigCard() {
  const { data, isLoading } = useQuery({
    queryKey: ["graph-config"],
    queryFn: () => api.graphConfig(),
  });

  return (
    <div className="rounded-lg border bg-card text-card-foreground shadow-sm">
      <div className="p-6 space-y-1 border-b">
        <h2 className="text-lg font-semibold">Microsoft Graph 配置</h2>
        <p className="text-sm text-muted-foreground">
          通过 Application 权限（Mail.ReadWrite / Mail.Send）共用一个发件邮箱。
          Admin 需在 Azure AD 给该 App 一次 admin consent。
        </p>
      </div>
      <div className="p-6 space-y-3 text-sm">
        {isLoading ? (
          <div className="text-muted-foreground">读取中…</div>
        ) : !data ? (
          <div className="text-muted-foreground">未配置</div>
        ) : (
          <>
            <Row label="Tenant 已配置" value={data.tenant_configured ? "是" : "否"} />
            <Row label="Client App 已配置" value={data.client_configured ? "是" : "否"} />
            <Row label="共享发件邮箱" value={data.mailbox || "—"} />
            <Row label="请求作用域" value={data.scopes} />
            {data.admin_consent_url ? (
              <a
                href={data.admin_consent_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center rounded-md border px-3 py-1.5 text-xs hover:bg-muted"
              >
                在 Azure AD 同意（首次部署点一次）
              </a>
            ) : null}
            <p className="text-xs text-muted-foreground pt-2">
              配置后请到 <a href="/api/settings" target="_blank" rel="noreferrer" className="underline">系统设置 → Graph</a>{" "}
              填入 GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / GRAPH_MAILBOX_UPN，
              并把 EMAIL_PROVIDER_TYPE 改为 <code>graph</code>。
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <div className="w-32 shrink-0 text-muted-foreground">{label}</div>
      <div className="font-mono text-xs break-all">{value}</div>
    </div>
  );
}

function AccountForm({
  initial,
  onClose,
}: {
  initial?: EmailAccountRow;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const isEdit = Boolean(initial);
  // All accounts use Microsoft Graph for send + receive.
  const [fromEmail, setFromEmail] = useState(initial?.from_email ?? "");
  const [fromName, setFromName] = useState(initial?.from_name ?? "");
  const [graphTenantId, setGraphTenantId] = useState(initial?.graph_tenant_id ?? "");
  const [graphClientId, setGraphClientId] = useState(initial?.graph_client_id ?? "");
  const [graphSecret, setGraphSecret] = useState("");
  const [graphUpn, setGraphUpn] = useState(initial?.graph_user_principal_name ?? "");
  const [dailyLimit, setDailyLimit] = useState<string>(
    String(initial?.daily_send_limit ?? 20)
  );
  const [hourlyLimit, setHourlyLimit] = useState<string>(
    String(initial?.hourly_send_limit ?? 10)
  );
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const payload: EmailAccountPayload = {
        provider_type: "graph",
        from_email: fromEmail,
        from_name: fromName,
        graph_tenant_id: graphTenantId,
        graph_client_id: graphClientId,
        graph_user_principal_name: graphUpn,
        daily_send_limit: Number(dailyLimit) || 20,
        hourly_send_limit: Number(hourlyLimit) || 0,
      };
      if (graphSecret) {
        payload.graph_client_secret = graphSecret;
      }
      if (isEdit && initial) {
        return api.updateEmailAccount(initial.id, payload);
      }
      return api.createEmailAccount(payload);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["email-accounts"] });
      onClose();
    },
    onError: (e) => setError(e instanceof Error ? e.message : "保存失败"),
  });

  return (
    <div className="rounded-lg border bg-muted/30 p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{isEdit ? "编辑邮箱" : "添加邮箱"}</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          取消
        </button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
        <label className="space-y-1">
          <span className="font-medium">From 邮箱</span>
          <input
            type="email"
            value={fromEmail}
            onChange={(e) => setFromEmail(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2"
            placeholder="sales@company.com"
          />
        </label>
        <label className="space-y-1">
          <span className="font-medium">From 显示名</span>
          <input
            value={fromName}
            onChange={(e) => setFromName(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2"
            placeholder="Ai Hunter"
          />
        </label>
        <label className="space-y-1">
          <span className="font-medium">每日发送上限</span>
          <input
            type="number"
            min={0}
            max={1000}
            value={dailyLimit}
            onChange={(e) => setDailyLimit(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 font-mono"
            placeholder="20"
          />
          <span className="text-xs text-muted-foreground">到上限后调度器自动把剩余邮件重排到次日 00:05 (UTC)。0 = 不限。</span>
        </label>
        <label className="space-y-1">
          <span className="font-medium">每小时上限（可选）</span>
          <input
            type="number"
            min={0}
            max={500}
            value={hourlyLimit}
            onChange={(e) => setHourlyLimit(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 font-mono"
            placeholder="10"
          />
        </label>
      </div>

      <div className="space-y-3">
        <h4 className="text-sm font-semibold pt-2">Microsoft Graph 凭据（可选）</h4>
        <p className="text-xs text-muted-foreground">
          留空则共用 .env 中的 <code>GRAPH_*</code> 配置（推荐）。
          在这里填值可以为单个账号覆盖默认的 Graph 凭据。
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
          <label className="space-y-1">
            <span className="font-medium">Graph Tenant ID</span>
            <input
              value={graphTenantId}
              onChange={(e) => setGraphTenantId(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 font-mono"
              placeholder="（留空用全局）"
            />
          </label>
          <label className="space-y-1">
            <span className="font-medium">Graph Client ID</span>
            <input
              value={graphClientId}
              onChange={(e) => setGraphClientId(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 font-mono"
              placeholder="（留空用全局）"
            />
          </label>
          <label className="space-y-1">
            <span className="font-medium">Graph Client Secret</span>
            <input
              type="password"
              value={graphSecret}
              onChange={(e) => setGraphSecret(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2"
              placeholder={isEdit ? "留空保持不变" : "（留空用全局）"}
            />
          </label>
          <label className="space-y-1">
            <span className="font-medium">Graph 用户 / UPN</span>
            <input
              value={graphUpn}
              onChange={(e) => setGraphUpn(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 font-mono"
              placeholder="（留空用全局）"
            />
          </label>
        </div>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="inline-flex items-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
        >
          {save.isPending ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}

function DailyQuotaBadge({ sent, limit }: { sent: number; limit: number }) {
  if (!limit || limit <= 0) {
    return (
      <span className="inline-flex items-center gap-1 text-muted-foreground">
        今日 <span className="font-mono text-foreground">{sent}</span> 封 · 上限 未设
      </span>
    );
  }
  const pct = Math.min(100, Math.round((sent / limit) * 100));
  const tone =
    sent >= limit ? "text-destructive" :
    pct >= 80 ? "text-amber-600" :
    "text-muted-foreground";
  return (
    <span className={`inline-flex items-center gap-1 ${tone}`}>
      今日 <span className="font-mono text-foreground">{sent}</span> / {limit} 封
      <span className="text-muted-foreground">({pct}%)</span>
      {sent >= limit ? <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] uppercase">已达上限</span> : null}
    </span>
  );
}


function AccountRow({
  account,
  onEdit,
  qc,
}: {
  account: EmailAccountRow;
  onEdit: () => void;
  qc: ReturnType<typeof useQueryClient>;
}) {
  const [testing, setTesting] = useState<null | "graph">(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const remove = useMutation({
    mutationFn: () => api.deleteEmailAccount(account.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["email-accounts"] }),
  });
  const runTest = async (kind: "graph") => {
    setTesting(kind);
    setTestResult(null);
    try {
      const result = await api.testEmailAccount(account.id, kind);
      setTestResult({ ok: result.ok, msg: result.ok ? "连接成功" : (result.error || "失败") });
    } catch (e) {
      setTestResult({ ok: false, msg: e instanceof Error ? e.message : "测试失败" });
    } finally {
      setTesting(null);
    }
  };
  return (
    <div className="flex flex-col gap-2 rounded-md border p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-muted-foreground" />
          <span className="font-medium">{account.from_email || "(未填)"}</span>
          <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${account.provider_type === "graph" ? "bg-blue-500/10 text-blue-600" : "bg-muted text-muted-foreground"}`}>
            {account.provider_type}
          </span>
          <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${account.status === "active" ? "bg-emerald-500/10 text-emerald-600" : "bg-muted text-muted-foreground"}`}>
            {account.status}
          </span>
        </div>
        <div className="text-xs text-muted-foreground">
          {account.from_name ? `${account.from_name} · ` : ""}
          共享邮箱 {account.graph_user_principal_name || "—"}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <DailyQuotaBadge
            sent={account.sent_today ?? 0}
            limit={account.daily_send_limit}
          />
        </div>
        {testResult ? (
          <div className={`text-xs ${testResult.ok ? "text-emerald-600" : "text-destructive"}`}>
            测试：{testResult.msg}
          </div>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={testing === "graph"}
          onClick={() => runTest("graph")}
          className="rounded-md border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-60"
        >
          {testing === "graph" ? "测试中…" : "测试连接"}
        </button>
        <button
          type="button"
          onClick={onEdit}
          className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-muted"
        >
          <Pencil className="h-3 w-3" /> 编辑
        </button>
        <button
          type="button"
          onClick={() => {
            if (confirm(`确定删除邮箱 ${account.from_email}?`)) remove.mutate();
          }}
          disabled={remove.isPending}
          className="inline-flex items-center gap-1 rounded-md border border-destructive/30 px-2.5 py-1 text-xs text-destructive hover:bg-destructive/5 disabled:opacity-60"
        >
          <Trash2 className="h-3 w-3" /> 删除
        </button>
      </div>
    </div>
  );
}

export function ConnectedMailboxesPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<EmailAccountRow | null>(null);
  const [showForm, setShowForm] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["email-accounts"],
    queryFn: () => api.listEmailAccounts(),
  });

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/settings" className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4 mr-1" /> 返回设置
        </Link>
      </div>
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">已连接邮箱</h1>
          <p className="text-sm text-muted-foreground">管理多套发件邮箱。所有账号都走 Microsoft Graph Application 权限 + 共享邮箱。</p>
        </div>
        <button
          type="button"
          onClick={() => {
            setEditing(null);
            setShowForm(true);
          }}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" /> 添加邮箱
        </button>
      </div>

      {showForm ? (
        <AccountForm
          initial={editing ?? undefined}
          onClose={() => {
            setShowForm(false);
            setEditing(null);
          }}
        />
      ) : null}

      <GraphConfigCard />

      <div className="rounded-lg border bg-card text-card-foreground shadow-sm">
        <div className="p-6 space-y-1 border-b">
          <h2 className="text-lg font-semibold">邮箱列表</h2>
          <p className="text-sm text-muted-foreground">每个账号可单独测试连接与删除。</p>
        </div>
        <div className="p-6 space-y-3">
          {isLoading ? (
            <LoadingState message="正在加载邮箱…" variant="skeleton" skeletonCount={2} />
          ) : !data || data.accounts.length === 0 ? (
            <EmptyState
              title="还没有邮箱"
              message='点上方 "添加邮箱" 创建第一个。'
            />
          ) : (
            data.accounts.map((acc) => (
              <AccountRow
                key={acc.id}
                account={acc}
                onEdit={() => {
                  setEditing(acc);
                  setShowForm(true);
                }}
                qc={qc}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
