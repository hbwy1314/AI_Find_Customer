import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  Save,
  Search,
  TestTube2,
  TriangleAlert,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { api, type GraphUser } from "@/api/client";
import { useSettingsForm, stripUnchangedSecrets } from "@/lib/settingsForm";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SecretInput } from "./settings-panels";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const SAVE_KEYS = [
  "graph_tenant_id", "graph_client_id", "graph_client_secret",
  "graph_mailbox_upn", "graph_default_scopes",
  "email_provider_type",
];

const SECRET_KEYS = ["graph_client_secret"];

type TestState = "idle" | "ok" | "err";

type SyncState =
  | { kind: "loading" }
  | { kind: "ok"; users: GraphUser[] }
  | { kind: "err"; msg: string; hint?: string };

function SyncFromAzureDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkResult, setBulkResult] = useState<
    { created: number; skipped: number } | null
  >(null);
  const [state, setState] = useState<SyncState>({ kind: "loading" });

  // First render: fetch the user list (backend calls Graph /users).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.listGraphUsers();
        if (cancelled) return;
        setState({ kind: "ok", users: res.users });
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : "拉取用户失败";
        setState({ kind: "err", msg, hint: msg.includes("User.Read.All") ? "在 Azure 给此 App 添加 Application 权限 User.Read.All 并 admin consent" : undefined });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    if (state.kind !== "ok") return [];
    if (!filter.trim()) return state.users;
    const q = filter.toLowerCase();
    return state.users.filter(
      (u) =>
        u.email.toLowerCase().includes(q) ||
        u.display_name.toLowerCase().includes(q) ||
        u.user_principal_name.toLowerCase().includes(q) ||
        u.job_title.toLowerCase().includes(q),
    );
  }, [state, filter]);

  const bulkAddMutation = useMutation({
    mutationFn: (emails: string[]) => api.bulkAddGraphAccounts(emails),
    onSuccess: (res) => {
      setBulkResult({ created: res.created_count, skipped: res.skipped_count });
      void qc.invalidateQueries({ queryKey: ["email-accounts"] });
    },
  });

  const toggle = (email: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email);
      else next.add(email);
      return next;
    });
  };
  const selectAllFiltered = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const u of filtered) next.add(u.email);
      return next;
    });
  };
  const clearSelection = () => setSelected(new Set());

  const submit = () => {
    if (selected.size === 0) return;
    bulkAddMutation.mutate(Array.from(selected));
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-3xl rounded-lg border bg-card text-card-foreground shadow-lg flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between p-4 border-b">
          <div className="flex items-center gap-2 font-semibold">
            <Users className="h-4 w-4" />
            同步 Azure AD 账号
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:bg-muted"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {state.kind === "loading" ? (
          <div className="p-8 text-center text-muted-foreground">
            <Loader2 className="h-5 w-5 inline-block animate-spin mr-2" />
            正在从 Microsoft Graph 拉取用户…
          </div>
        ) : state.kind === "err" ? (
          <div className="p-6 space-y-3">
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive inline-flex items-start gap-2">
              <TriangleAlert className="h-4 w-4 mt-0.5 shrink-0" />
              <div>
                <p>{state.msg}</p>
                {state.hint ? <p className="mt-1 text-xs">{state.hint}</p> : null}
              </div>
            </div>
            <Button variant="outline" onClick={() => setState({ kind: "loading" })}>重试</Button>
          </div>
        ) : (
          <>
            <div className="p-4 border-b flex flex-wrap items-center gap-2">
              <div className="relative flex-1 min-w-[200px]">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="按邮箱 / 姓名 / 职位搜索…"
                  className="pl-8 h-9"
                />
              </div>
              <div className="text-xs text-muted-foreground whitespace-nowrap">
                已选 <span className="font-semibold text-foreground">{selected.size}</span> / {state.users.length}
              </div>
              <Button variant="ghost" size="sm" onClick={selectAllFiltered}>全选当前</Button>
              <Button variant="ghost" size="sm" onClick={clearSelection}>清空</Button>
            </div>

            {bulkResult ? (
              <div className="mx-4 mt-4 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
                同步完成 — 新建 <span className="font-semibold">{bulkResult.created}</span> 个账号
                {bulkResult.skipped > 0 ? `，跳过 ${bulkResult.skipped} 个已存在` : ""}。
                <Link to="/settings/mailboxes" onClick={onClose} className="underline ml-2">查看邮箱列表 →</Link>
              </div>
            ) : null}

            <div className="flex-1 overflow-y-auto divide-y">
              {filtered.length === 0 ? (
                <div className="p-8 text-center text-sm text-muted-foreground">无匹配用户</div>
              ) : (
                filtered.map((u) => (
                  <label
                    key={u.id || u.email}
                    className="flex items-start gap-3 p-3 hover:bg-muted/40 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={selected.has(u.email)}
                      onChange={() => toggle(u.email)}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2 flex-wrap">
                        <span className="font-medium truncate">
                          {u.display_name || u.user_principal_name}
                        </span>
                        {u.job_title ? (
                          <span className="text-xs text-muted-foreground">{u.job_title}</span>
                        ) : null}
                      </div>
                      <div className="font-mono text-xs text-muted-foreground truncate">{u.email}</div>
                      {u.department ? (
                        <div className="text-xs text-muted-foreground">{u.department}</div>
                      ) : null}
                    </div>
                  </label>
                ))
              )}
            </div>

            <div className="p-4 border-t flex items-center justify-between gap-2">
              <div className="text-xs text-muted-foreground">
                {bulkAddMutation.isError ? (
                  <span className="text-destructive">{bulkAddMutation.error.message}</span>
                ) : (
                  <>勾选后批量创建为 Graph 账号，发件时按 campaign 选择使用</>
                )}
              </div>
              <Button
                onClick={submit}
                disabled={selected.size === 0 || bulkAddMutation.isPending || bulkResult !== null}
              >
                {bulkAddMutation.isPending ? (
                  <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 添加中…</>
                ) : (
                  <><UserPlus className="mr-2 h-4 w-4" /> 添加 {selected.size || ""} 个账号</>
                )}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function GraphSettingsPage() {
  const form = useSettingsForm();
  const [testState, setTestState] = useState<{ kind: TestState; msg: string }>({ kind: "idle", msg: "" });
  const [showSync, setShowSync] = useState(false);

  const onSave = () => {
    const payload = stripUnchangedSecrets(form.values, SECRET_KEYS);
    const filtered: Record<string, string> = {};
    for (const k of SAVE_KEYS) {
      if (k in payload) filtered[k] = payload[k];
    }
    form.save(filtered);
  };

  const testMutation = useMutation({
    mutationFn: async () => {
      // Save current values first so the backend reads fresh credentials.
      const payload = stripUnchangedSecrets(form.values, SECRET_KEYS);
      const filtered: Record<string, string> = {};
      for (const k of SAVE_KEYS) {
        if (k in payload) filtered[k] = payload[k];
      }
      await api.saveSettings(filtered);
      return api.testGraphSettings();
    },
    onSuccess: (res) => {
      if (res.status === "ok") {
        setTestState({
          kind: "ok",
          msg: `连接成功 — ${res.upn || res.mailbox} (${res.display_name || "no display name"})`,
        });
      } else {
        setTestState({ kind: "err", msg: res.message || "未知错误" });
      }
    },
    onError: (e: Error) => setTestState({ kind: "err", msg: e.message || "测试失败" }),
  });

  const tenantId = form.values.graph_tenant_id?.trim() ?? "";
  const clientId = form.values.graph_client_id?.trim() ?? "";
  const adminConsentUrl = tenantId && clientId
    ? `https://login.microsoftonline.com/${tenantId}/adminconsent?client_id=${clientId}`
    : "";

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Microsoft Graph 配置</h1>
        <p className="text-muted-foreground mt-1">
          用 <a href="/settings/mailboxes" className="underline">已连接邮箱</a> 里的 Graph 账号共用同一份 Azure AD App 配置。
          走 <span className="font-mono">Application 权限</span>，管理员在 Azure 一次性 consent 即可，团队共用一个发件邮箱。
        </p>
      </div>

      {form.isLoading ? (
        <div className="flex items-center justify-center py-20 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin mr-2" /> 正在加载设置…
        </div>
      ) : (
        <>
          {/* Provider type toggle */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">默认发件 Provider</CardTitle>
              <CardDescription>
                切到 <span className="font-mono">graph</span> 后，发件与回信检测都会用共享邮箱走 Graph API，不再依赖 SMTP/IMAP。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              {([
                ["smtp", "SMTP/IMAP（兜底）"],
                ["graph", "Microsoft Graph"],
              ] as const).map(([v, l]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => form.handleChange("email_provider_type", v)}
                  className={`rounded-md border px-4 py-2 text-sm ${
                    (form.values.email_provider_type ?? "smtp") === v
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >
                  {l}
                </button>
              ))}
            </CardContent>
          </Card>

          {/* Azure AD App credentials */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Azure AD 应用凭据</CardTitle>
              <CardDescription>
                在 Azure Portal → App registrations 创建 confidential client，并申请 Application 权限{" "}
                <span className="font-mono">Mail.ReadWrite</span> 与 <span className="font-mono">Mail.Send</span>。
                管理员一次性 admin consent 后，应用即可用 client_credentials 拿 token。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="graph_tenant_id">Tenant ID</Label>
                <Input
                  id="graph_tenant_id"
                  value={form.values.graph_tenant_id ?? ""}
                  onChange={(e) => form.handleChange("graph_tenant_id", e.target.value)}
                  placeholder="11111111-2222-3333-4444-555555555555"
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">在 App registrations → Overview → Directory (tenant) ID</p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="graph_client_id">Client (Application) ID</Label>
                <Input
                  id="graph_client_id"
                  value={form.values.graph_client_id ?? ""}
                  onChange={(e) => form.handleChange("graph_client_id", e.target.value)}
                  placeholder="22222222-3333-4444-5555-666666666666"
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">App registrations → Application (client) ID</p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="graph_client_secret">Client Secret</Label>
                <SecretInput
                  id="graph_client_secret"
                  value={form.values.graph_client_secret ?? ""}
                  onChange={(v) => form.handleChange("graph_client_secret", v)}
                  placeholder="在 Certificates & secrets → Client secrets 新建"
                />
                <p className="text-xs text-muted-foreground">保存时只更新非 **** 的值；保留旧值直接留空。</p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="graph_default_scopes">请求作用域（一般不用改）</Label>
                <Input
                  id="graph_default_scopes"
                  value={form.values.graph_default_scopes ?? "https://graph.microsoft.com/.default"}
                  onChange={(e) => form.handleChange("graph_default_scopes", e.target.value)}
                  placeholder="https://graph.microsoft.com/.default"
                  className="font-mono text-sm"
                />
              </div>
            </CardContent>
          </Card>

          {/* Shared mailbox */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">共享发件邮箱</CardTitle>
              <CardDescription>
                所有 Graph 账号都用这个邮箱发件和查收件箱。可以是 sales@company.com，也可以是某个共享 mailbox。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-1.5">
              <Label htmlFor="graph_mailbox_upn">共享发件邮箱 (UPN / SMTP)</Label>
              <Input
                id="graph_mailbox_upn"
                value={form.values.graph_mailbox_upn ?? ""}
                onChange={(e) => form.handleChange("graph_mailbox_upn", e.target.value)}
                placeholder="sales@company.com"
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">该邮箱必须在申请 Mail.ReadWrite 时可访问，否则测试会 403/404。</p>
            </CardContent>
          </Card>

          {/* Admin consent + test */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">验证</CardTitle>
              <CardDescription>
                第一次部署时需要管理员在 Azure 同意一次 Application 权限；之后只用 client_credentials 拿 token。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {adminConsentUrl ? (
                <a
                  href={adminConsentUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
                >
                  在 Azure AD admin consent <ExternalLink className="h-3.5 w-3.5" />
                </a>
              ) : (
                <p className="text-xs text-muted-foreground">先填 Tenant ID 与 Client ID 以生成 admin consent 链接。</p>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => testMutation.mutate()}
                  disabled={testMutation.isPending}
                >
                  {testMutation.isPending ? (
                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 测试中…</>
                  ) : (
                    <><TestTube2 className="mr-2 h-4 w-4" /> 测试 Graph 连接</>
                  )}
                </Button>
                {testState.kind === "ok" && (
                  <p className="text-sm text-emerald-600 inline-flex items-center gap-1">
                    <CheckCircle2 className="h-4 w-4" /> {testState.msg}
                  </p>
                )}
                {testState.kind === "err" && (
                  <p className="text-sm text-destructive inline-flex items-center gap-1">
                    <TriangleAlert className="h-4 w-4" /> {testState.msg}
                  </p>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                点击测试会先自动保存当前表单，然后调 <span className="font-mono">/api/settings/email/graph-test</span> →
                后端用 client_credentials 拿 token 调 <span className="font-mono">GET /users/&#123;mailbox&#125;</span>。
              </p>
            </CardContent>
          </Card>

          {/* Sync from Azure AD */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Graph 账号管理</CardTitle>
              <CardDescription>
                从 Azure AD 拉取所有 enabled 用户，勾选后批量创建为本系统的 Graph 发件账号（共用上方同一份 App 凭据）。
                每个用户一个发件账号，可在 <Link to="/settings/mailboxes" className="underline">已连接邮箱</Link> 中看到。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs text-muted-foreground">
                需要的 Application 权限：<span className="font-mono">User.Read.All</span>（或 <span className="font-mono">User.ReadBasic.All</span>）。
                第一次点同步如果 403，去 Azure 给 App 加这个权限并重新 admin consent。
              </p>
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  onClick={() => setShowSync(true)}
                >
                  <Users className="mr-2 h-4 w-4" /> 同步 Azure AD 账号
                </Button>
                <Link
                  to="/settings/mailboxes"
                  className="text-sm text-muted-foreground hover:text-foreground underline"
                >
                  查看已添加的邮箱 →
                </Link>
              </div>
            </CardContent>
          </Card>

          <div className="flex justify-end pb-8">
            <Button onClick={onSave} disabled={form.isSaving} size="lg">
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

      {showSync ? <SyncFromAzureDialog onClose={() => setShowSync(false)} /> : null}
    </div>
  );
}
