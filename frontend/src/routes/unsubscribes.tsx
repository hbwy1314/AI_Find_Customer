import { useDeferredValue, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, ChevronLeft, ChevronRight, Loader2, MailPlus, Search, ShieldAlert, Trash2 } from "lucide-react";

import { api, type UnsubscribeRecord } from "@/api/client";
import { EmptyState, ErrorState, LoadingState } from "@/components/data-states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

const PAGE_SIZE = 50;

function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function scopeLabel(scope: string): string {
  if (scope === "all") return "全局退订";
  if (scope.startsWith("campaign:")) return "邮件活动";
  if (scope.startsWith("sequence:")) return "邮件序列";
  return scope || "未知";
}

function sourceLabel(source: string): string {
  if (source === "manual") return "手动添加";
  if (source === "link") return "退订链接";
  return source || "未知";
}

function scopeDetail(scope: string): string {
  const separator = scope.indexOf(":");
  return separator >= 0 ? scope.slice(separator + 1) : "所有邮件";
}

export function UnsubscribesPage() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin" || user?.role === "dev";
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const [scopeType, setScopeType] = useState("");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(0);
  const [email, setEmail] = useState("");
  const [formError, setFormError] = useState("");
  const [listError, setListError] = useState("");
  const [notice, setNotice] = useState("");

  const listQuery = useQuery({
    queryKey: ["unsubscribes", deferredSearch, scopeType, source, page],
    queryFn: () => api.listUnsubscribes({
      query: deferredSearch,
      scopeType,
      source,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    enabled: isAdmin,
  });

  const addMutation = useMutation({
    mutationFn: () => api.createUnsubscribe(email.trim()),
    onSuccess: (result) => {
      setEmail("");
      setFormError("");
      setNotice(`${result.item.email} 已加入全局退订名单`);
      setPage(0);
      queryClient.invalidateQueries({ queryKey: ["unsubscribes"] });
    },
    onError: (error) => {
      setNotice("");
      setFormError(error instanceof Error ? error.message : "添加失败");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (record: UnsubscribeRecord) => api.deleteUnsubscribe(record.id),
    onSuccess: (_, record) => {
      setListError("");
      setNotice(`${record.email} 的这条退订记录已移除`);
      if (page > 0 && listQuery.data?.items.length === 1) {
        setPage((value) => Math.max(0, value - 1));
      }
      queryClient.invalidateQueries({ queryKey: ["unsubscribes"] });
    },
    onError: (error) => {
      setListError(error instanceof Error ? error.message : "恢复订阅失败");
    },
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setFormError("");
    setNotice("");
    if (!email.trim()) {
      setFormError("请输入邮箱地址");
      return;
    }
    addMutation.mutate();
  };

  const remove = (record: UnsubscribeRecord) => {
    const confirmed = window.confirm(
      `确定移除 ${record.email} 的“${scopeLabel(record.scope)}”记录吗？\n\n移除后允许未来邮件发送，但不会自动重启已经停止的历史邮件序列。`,
    );
    if (confirmed) deleteMutation.mutate(record);
  };

  const data = listQuery.data;
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE));
  const hasFilters = Boolean(deferredSearch || scopeType || source);

  if (!isAdmin) {
    return (
      <div className="mx-auto max-w-xl rounded-lg border bg-card p-8 text-center shadow-sm">
        <ShieldAlert className="mx-auto h-8 w-8 text-muted-foreground" />
        <h1 className="mt-3 text-lg font-semibold">需要管理员权限</h1>
        <p className="mt-1 text-sm text-muted-foreground">退订邮箱包含联系人隐私数据，仅管理员可以查看和维护。</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">退订邮箱管理</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          退订名单会在每次发送前强制检查。全局退订会阻止该地址接收所有后续邮件。
        </p>
      </div>

      <section className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="rounded-md bg-destructive/10 p-2 text-destructive">
            <MailPlus className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold">手动添加全局退订</h2>
            <p className="mt-1 text-sm text-muted-foreground">适用于通过回复、电话或其他渠道提出退订的联系人。</p>
            <form className="mt-4 flex flex-col gap-2 sm:flex-row" onSubmit={submit}>
              <label className="sr-only" htmlFor="unsubscribe-email">邮箱地址</label>
              <input
                id="unsubscribe-email"
                type="email"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="contact@example.com"
                className="h-10 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <Button type="submit" disabled={addMutation.isPending}>
                {addMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <MailPlus className="mr-2 h-4 w-4" />}
                加入退订名单
              </Button>
            </form>
            {formError ? <p className="mt-2 text-sm text-destructive" role="alert">{formError}</p> : null}
            {notice ? <p className="mt-2 text-sm text-emerald-600" role="status">{notice}</p> : null}
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="全部记录" value={data?.counts.total} />
          <Stat label="全局退订" value={data?.counts.global} />
          <Stat label="邮件活动" value={data?.counts.campaign} />
          <Stat label="邮件序列" value={data?.counts.sequence} />
        </div>

        <div className="rounded-lg border bg-card shadow-sm">
          <div className="flex flex-col gap-3 border-b p-4 lg:flex-row lg:items-center">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <label className="sr-only" htmlFor="unsubscribe-search">搜索邮箱或作用域</label>
              <input
                id="unsubscribe-search"
                type="search"
                value={search}
                onChange={(event) => { setSearch(event.target.value); setPage(0); }}
                placeholder="搜索邮箱或活动 ID"
                className="h-10 w-full rounded-md border bg-background pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <div className="grid grid-cols-2 gap-2 sm:flex">
              <label className="sr-only" htmlFor="unsubscribe-scope">作用域</label>
              <select
                id="unsubscribe-scope"
                value={scopeType}
                onChange={(event) => { setScopeType(event.target.value); setPage(0); }}
                className="h-10 rounded-md border bg-background px-3 text-sm"
              >
                <option value="">全部作用域</option>
                <option value="all">全局退订</option>
                <option value="campaign">邮件活动</option>
                <option value="sequence">邮件序列</option>
              </select>
              <label className="sr-only" htmlFor="unsubscribe-source">来源</label>
              <select
                id="unsubscribe-source"
                value={source}
                onChange={(event) => { setSource(event.target.value); setPage(0); }}
                className="h-10 rounded-md border bg-background px-3 text-sm"
              >
                <option value="">全部来源</option>
                <option value="link">退订链接</option>
                <option value="manual">手动添加</option>
              </select>
            </div>
          </div>

          {listError ? (
            <div className="border-b bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">{listError}</div>
          ) : null}

          <div className="min-h-64">
            {listQuery.isLoading ? (
              <LoadingState message="正在加载退订记录…" variant="skeleton" skeletonCount={5} className="m-4" />
            ) : listQuery.isError ? (
              <ErrorState error={listQuery.error} onRetry={() => listQuery.refetch()} className="m-4" />
            ) : !data || data.items.length === 0 ? (
              <EmptyState
                icon={<Ban className="h-5 w-5" />}
                title={hasFilters ? "没有匹配的退订记录" : "退订名单为空"}
                message={hasFilters ? "调整搜索词或筛选条件后重试。" : "新退订记录会显示在这里。"}
                className="m-4"
              />
            ) : (
              <div className="divide-y">
                {data.items.map((record) => (
                  <div key={record.id} className="grid gap-3 p-4 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_160px_40px] sm:items-center">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium" title={record.email}>{record.email}</p>
                      <p className="mt-1 text-xs text-muted-foreground">退订于 {formatDate(record.unsubscribed_at)}</p>
                    </div>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={record.scope === "all" ? "destructive" : "secondary"}>{scopeLabel(record.scope)}</Badge>
                        <Badge variant="outline">{sourceLabel(record.source)}</Badge>
                      </div>
                      <p className="mt-1 truncate font-mono text-xs text-muted-foreground" title={record.scope}>{scopeDetail(record.scope)}</p>
                    </div>
                    <p className="text-xs text-muted-foreground sm:text-right">记录于 {formatDate(record.created_at)}</p>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      title="移除退订记录"
                      aria-label={`移除 ${record.email} 的退订记录`}
                      disabled={deleteMutation.isPending}
                      onClick={() => remove(record)}
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-col gap-3 border-t p-4 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
            <span>共 {data?.total ?? 0} 条{hasFilters ? "匹配记录" : "记录"}</span>
            <div className="flex items-center gap-2">
              <Button type="button" variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>
                <ChevronLeft className="mr-1 h-4 w-4" /> 上一页
              </Button>
              <span className="min-w-20 text-center">第 {page + 1} / {totalPages} 页</span>
              <Button type="button" variant="outline" size="sm" disabled={page + 1 >= totalPages} onClick={() => setPage((value) => value + 1)}>
                下一页 <ChevronRight className="ml-1 h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value?: number }) {
  return (
    <div className="rounded-md border bg-card px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums">{value ?? "-"}</p>
    </div>
  );
}
