/**
 * Top-level /quotas page — focused view of every email account's daily/hourly
 * send caps with the actual sent-today count from the scheduler.
 *
 * Inline-edit the limits without leaving the page; updates flow through
 * PATCH /api/v1/email-accounts/{id}. The rows are draggable so the user
 * can set a manual rotation order — drag a row by the handle on the left,
 * drop it where you want it in the list, and the new order is persisted
 * via POST /api/v1/email-accounts/reorder.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EmptyState, ErrorState, LoadingState } from "@/components/data-states";
import {
  BarChart3,
  Edit2,
  GripVertical,
  Inbox,
  Loader2,
  Mail,
  RotateCcw,
  Save,
  X,
} from "lucide-react";

import { api, type EmailAccountRow } from "../api/client";

function ProgressBar({ used, limit, tone }: { used: number; limit: number; tone: string }) {
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const barTone =
    pct >= 100 ? "bg-destructive" :
    pct >= 80 ? "bg-amber-500" :
    "bg-emerald-500";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className={`font-mono ${tone}`}>
          {used} / {limit > 0 ? limit : "∞"}
        </span>
        <span className="text-muted-foreground">{pct}%</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className={`h-full ${barTone} transition-[width]`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function QuotaRow({
  account,
  onSave,
  isSaving,
  dragHandleProps,
  rowDropProps,
  isDragging,
  isDropTarget,
}: {
  account: EmailAccountRow;
  onSave: (patch: { daily_send_limit?: number; hourly_send_limit?: number }) => Promise<void>;
  isSaving: boolean;
  dragHandleProps?: React.HTMLAttributes<HTMLSpanElement>;
  rowDropProps?: React.HTMLAttributes<HTMLTableRowElement>;
  isDragging?: boolean;
  isDropTarget?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [daily, setDaily] = useState<string>(String(account.daily_send_limit ?? 20));
  const [hourly, setHourly] = useState<string>(String(account.hourly_send_limit ?? 0));

  const used = account.sent_today ?? 0;
  const dailyLimit = account.daily_send_limit ?? 0;
  const dailyTone =
    dailyLimit > 0 && used >= dailyLimit ? "text-destructive" :
    dailyLimit > 0 && used / dailyLimit >= 0.8 ? "text-amber-600" :
    "text-foreground";

  const startEdit = () => {
    setDaily(String(account.daily_send_limit ?? 20));
    setHourly(String(account.hourly_send_limit ?? 0));
    setEditing(true);
  };
  const cancel = () => setEditing(false);
  const save = async () => {
    const dailyN = Number(daily);
    const hourlyN = Number(hourly);
    await onSave({
      daily_send_limit: Number.isFinite(dailyN) ? Math.max(0, dailyN) : 20,
      hourly_send_limit: Number.isFinite(hourlyN) ? Math.max(0, hourlyN) : 0,
    });
    setEditing(false);
  };

  // Visual state for drag-and-drop feedback. We deliberately keep the row
  // visible (not opacity:0) while dragging so the user still has a ghost
  // to follow; the drop target gets a top border accent.
  const rowCls = [
    "border-b last:border-b-0 transition-colors",
    isDragging ? "opacity-40" : "hover:bg-muted/30",
    isDropTarget ? "border-t-2 border-t-primary" : "",
  ].join(" ");

  return (
    <tr
      className={rowCls}
      aria-grabbed={isDragging ? "true" : undefined}
      {...rowDropProps}
    >
      <td className="px-2 py-3 align-middle w-8">
        <span
          {...dragHandleProps}
          className="inline-flex h-7 w-5 cursor-grab select-none items-center justify-center rounded text-muted-foreground hover:bg-muted active:cursor-grabbing"
          title="拖动调整顺序"
          aria-label={`拖动 ${account.from_email || "账号"} 调整顺序`}
        >
          <GripVertical className="h-4 w-4" />
        </span>
      </td>
      <td className="px-4 py-3 align-top">
        <div className="flex items-center gap-2">
          <Inbox className="h-4 w-4 text-muted-foreground shrink-0" />
          <div>
            <div className="font-medium text-sm">{account.from_email || "(未填)"}</div>
            <div className="text-xs text-muted-foreground">{account.from_name || "—"}</div>
          </div>
        </div>
      </td>
      <td className="px-4 py-3 align-top">
        <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${account.provider_type === "graph" ? "bg-blue-500/10 text-blue-600" : "bg-muted text-muted-foreground"}`}>
          {account.provider_type}
        </span>
      </td>
      <td className="px-4 py-3 align-top w-48">
        {editing ? (
          <div className="space-y-1">
            <label className="flex items-center gap-2 text-xs">
              <span className="w-10 text-muted-foreground">每日</span>
              <input
                type="number"
                min={0}
                max={10000}
                value={daily}
                onChange={(e) => setDaily(e.target.value)}
                className="w-20 rounded-md border bg-background px-2 py-1 text-sm font-mono"
                autoFocus
              />
            </label>
            <label className="flex items-center gap-2 text-xs">
              <span className="w-10 text-muted-foreground">每小时</span>
              <input
                type="number"
                min={0}
                max={1000}
                value={hourly}
                onChange={(e) => setHourly(e.target.value)}
                placeholder="0=不限"
                className="w-20 rounded-md border bg-background px-2 py-1 text-sm font-mono"
              />
            </label>
          </div>
        ) : (
          <div className="space-y-1.5">
            <ProgressBar used={used} limit={dailyLimit} tone={dailyTone} />
            {account.hourly_send_limit ? (
              <div className="text-xs text-muted-foreground">每小时上限 {account.hourly_send_limit}</div>
            ) : null}
          </div>
        )}
      </td>
      <td className="px-4 py-3 align-top">
        <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
          account.status === "active" ? "bg-emerald-500/10 text-emerald-600" : "bg-muted text-muted-foreground"
        }`}>
          {account.status}
        </span>
      </td>
      <td className="px-4 py-3 align-top text-right">
        {editing ? (
          <div className="flex items-center justify-end gap-1">
            <button
              type="button"
              disabled={isSaving}
              onClick={save}
              className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
            >
              {isSaving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
              保存
            </button>
            <button
              type="button"
              onClick={cancel}
              className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
            >
              <X className="h-3 w-3" /> 取消
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={startEdit}
            className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted"
          >
            <Edit2 className="h-3 w-3" /> 编辑限额
          </button>
        )}
      </td>
    </tr>
  );
}

type SortKey = "manual" | "rotation" | "sent_desc" | "sent_asc" | "limit_asc" | "limit_desc" | "email";

const SORT_OPTIONS: { value: SortKey; label: string; hint: string }[] = [
  { value: "manual", label: "手动顺序", hint: "按你在本页拖动设置的顺序排列（默认）" },
  { value: "rotation", label: "轮转顺序", hint: "今日用量最少的排前；超限的高亮置底" },
  { value: "sent_desc", label: "今日已发 ↓", hint: "今日已发最多在前" },
  { value: "sent_asc", label: "今日已发 ↑", hint: "今日已发最少在前" },
  { value: "limit_asc", label: "每日上限 ↑", hint: "限额最小的在前" },
  { value: "limit_desc", label: "每日上限 ↓", hint: "限额最大的在前" },
  { value: "email", label: "邮箱 A→Z", hint: "按邮箱排序" },
];

export function QuotasPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("manual");
  // Local working copy of the manual order. The server is the source of
  // truth, but we want drag-and-drop to feel instant (no spinner) so we
  // mirror the order client-side and only call the API on drop. If the
  // server returns a different list later (e.g. 30s refetch, or a
  // sibling tab added an account), we re-sync from the server.
  const [manualOrder, setManualOrder] = useState<string[] | null>(null);
  // Drag state: which id is being dragged, which row it's hovering over.
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  const dragIdRef = useRef<string | null>(null);
  const overIdRef = useRef<string | null>(null);
  dragIdRef.current = dragId;
  overIdRef.current = overId;

  const accountsQ = useQuery({
    queryKey: ["email-accounts"],
    queryFn: api.listEmailAccounts,
    refetchInterval: 30_000, // auto-refresh so today's sent count stays current
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: { daily_send_limit?: number; hourly_send_limit?: number } }) =>
      api.updateEmailAccount(id, patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["email-accounts"] });
    },
  });

  const reorderMutation = useMutation({
    mutationFn: (ids: string[]) => api.reorderEmailAccounts(ids),
    // On success, the server returns the fresh list — we replace our
    // local copy with whatever the server said. The server is now the
    // source of truth (the user can re-drag if needed).
    onSuccess: (resp) => {
      if (resp?.accounts) {
        // Re-key the React Query cache so every consumer of
        // ["email-accounts"] picks up the new order on next read.
        qc.setQueryData(["email-accounts"], { accounts: resp.accounts, count: resp.count });
        setManualOrder(resp.accounts.map((a) => a.id));
      } else {
        void qc.invalidateQueries({ queryKey: ["email-accounts"] });
      }
    },
    onError: (err) => {
      console.error("[quotas] reorder failed", err);
      // Don't reset manualOrder to null — the user just dragged and
      // wants to see their new order locally even if the server round
      // trip hiccupped. The 30s refetch will reconcile eventually.
      // We still surface the error in the banner above so the user
      // knows it didn't persist.
      void qc.invalidateQueries({ queryKey: ["email-accounts"] });
    },
  });

  const onSave = async (
    id: string,
    patch: { daily_send_limit?: number; hourly_send_limit?: number }
  ) => {
    await updateMutation.mutateAsync({ id, patch });
  };

  const accounts = accountsQ.data?.accounts ?? [];

  // Re-sync the manual order whenever the server-side list changes
  // (new account added, deleted, etc). Only do it when no drag is active
  // and the current manual order is stale.
  useEffect(() => {
    if (dragIdRef.current) return; // never yank the floor out from under an active drag
    if (!accounts.length) {
      setManualOrder(null);
      return;
    }
    setManualOrder((prev) => {
      if (!prev) return accounts.map((a) => a.id);
      // Add any new ids at the end; drop ids that no longer exist.
      const known = new Set(prev);
      const merged = [...prev];
      for (const a of accounts) {
        if (!known.has(a.id)) merged.push(a.id);
      }
      return merged.filter((id) => accounts.some((a) => a.id === id));
    });
  }, [accounts]);

  // ── Drag handlers (HTML5 DnD — no extra dependency) ────────────────
  // `dragstart` lives on the handle (so dragging only starts when the
  // user grabs the handle — not when they click on a cell to edit).
  // `dragover` / `drop` / `dragleave` / `dragend` live on the <tr> so the
  // entire row is a valid drop target.
  const onDragStart = (e: React.DragEvent<HTMLSpanElement>, id: string) => {
    setDragId(id);
    // Firefox needs explicit data on the drag to actually fire drop.
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", id);
  };
  const onRowDragOver = (e: React.DragEvent<HTMLTableRowElement>, id: string) => {
    if (!dragIdRef.current) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (overIdRef.current !== id) setOverId(id);
  };
  const onRowDragLeave = (e: React.DragEvent<HTMLTableRowElement>, id: string) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    if (overIdRef.current === id) setOverId(null);
  };
  const onRowDrop = (e: React.DragEvent<HTMLTableRowElement>, dropId: string) => {
    e.preventDefault();
    const sourceId = dragIdRef.current;
    if (!sourceId || sourceId === dropId) {
      setDragId(null);
      setOverId(null);
      return;
    }
    // Capture geometry BEFORE the setState callback. React nulls out
    // `e.currentTarget` once this synchronous handler returns (synthetic
    // event pooling), and we still need the row's bounding rect to
    // decide whether the drop is above or below the midpoint.
    const rowEl = e.currentTarget;
    const rect = rowEl.getBoundingClientRect();
    const clientY = e.clientY;
    const above = clientY < rect.top + rect.height / 2;
    setManualOrder((prev) => {
      const base = prev ?? accounts.map((a) => a.id);
      const next = base.filter((id) => id !== sourceId);
      const targetIdx = next.indexOf(dropId);
      // Drop ABOVE the hovered row when the user is in the top half,
      // BELOW it otherwise.
      const insertAt = above ? Math.max(0, targetIdx) : Math.min(next.length, targetIdx + 1);
      next.splice(insertAt, 0, sourceId);
      // Filter against the *current* server-known ids before sending.
      // The 30s refetch can land between dragstart and drop and leave
      // us with stale ids; sending those to the backend is what
      // triggered the "保存顺序失败" notice the user saw.
      const knownIds = new Set(accounts.map((a) => a.id));
      const payload = next.filter((id) => knownIds.has(id));
      if (payload.length) {
        reorderMutation.mutate(payload);
      }
      return next;
    });
    setDragId(null);
    setOverId(null);
  };
  const onDragEnd = () => {
    setDragId(null);
    setOverId(null);
  };

  const stats = useMemo(() => {
    const total = accounts.length;
    const active = accounts.filter((a) => a.status === "active").length;
    const atLimit = accounts.filter(
      (a) => a.daily_send_limit > 0 && (a.sent_today ?? 0) >= a.daily_send_limit,
    ).length;
    const totalSent = accounts.reduce((sum, a) => sum + (a.sent_today ?? 0), 0);
    return { total, active, atLimit, totalSent };
  }, [accounts]);

  const filtered = useMemo(() => {
    const base = filter.trim()
      ? accounts.filter(
          (a) =>
            (a.from_email || "").toLowerCase().includes(filter.toLowerCase()) ||
            (a.from_name || "").toLowerCase().includes(filter.toLowerCase()) ||
            a.provider_type.toLowerCase().includes(filter.toLowerCase()),
        )
      : [...accounts];

    const pct = (a: EmailAccountRow) => {
      const lim = a.daily_send_limit ?? 0;
      if (lim <= 0) return 0;
      return (a.sent_today ?? 0) / lim;
    };
    const isAtLimit = (a: EmailAccountRow) => {
      const lim = a.daily_send_limit ?? 0;
      return lim > 0 && (a.sent_today ?? 0) >= lim;
    };

    return base.sort((a, b) => {
      switch (sortKey) {
        case "manual": {
          // Honour the user-defined rotation order. Anything not in the
          // manual list (shouldn't happen, but defensive) falls back to
          // the end of the list.
          const order = manualOrder ?? [];
          const ai = order.indexOf(a.id);
          const bi = order.indexOf(b.id);
          const aIdx = ai === -1 ? Number.MAX_SAFE_INTEGER : ai;
          const bIdx = bi === -1 ? Number.MAX_SAFE_INTEGER : bi;
          if (aIdx !== bIdx) return aIdx - bIdx;
          return (a.from_email || "").localeCompare(b.from_email || "");
        }
        case "rotation":
          // Active accounts with most remaining capacity first; at-limit goes last
          if (a.status !== b.status) return a.status === "active" ? -1 : 1;
          const aAt = isAtLimit(a) ? 1 : 0;
          const bAt = isAtLimit(b) ? 1 : 0;
          if (aAt !== bAt) return aAt - bAt;
          return pct(b) - pct(a);
        case "sent_desc":
          return (b.sent_today ?? 0) - (a.sent_today ?? 0);
        case "sent_asc":
          return (a.sent_today ?? 0) - (b.sent_today ?? 0);
        case "limit_asc":
          return (a.daily_send_limit || 0) - (b.daily_send_limit || 0);
        case "limit_desc":
          return (b.daily_send_limit || 0) - (a.daily_send_limit || 0);
        case "email":
          return (a.from_email || "").localeCompare(b.from_email || "");
        default:
          return 0;
      }
    });
  }, [accounts, filter, sortKey, manualOrder]);

  // Find the next-available account (top of the rotation order).
  const nextAvailable = useMemo(() => {
    return accounts.find((a) => {
      const lim = a.daily_send_limit ?? 0;
      return a.status === "active" && (lim <= 0 || (a.sent_today ?? 0) < lim);
    });
  }, [accounts]);

  // Manual-order reset: tells the server to rewrite sort_order using
  // the current created_at order. We just send the list back in the
  // order the server already has (which is created_at asc by default).
  const resetManualOrder = () => {
    const order = [...accounts]
      .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""))
      .map((a) => a.id);
    setManualOrder(order);
    if (order.length) reorderMutation.mutate(order);
  };

  return (
    <div className="max-w-6xl mx-auto px-4 py-6 space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">发送限额</h1>
        <p className="text-muted-foreground mt-1">
          集中管理所有邮箱账号的每日/每小时发送上限。今日已发送量按 UTC 00:00 起重置。
          到达上限的账号会被调度器自动跳过，并把剩余邮件重排到次日 00:05。
        </p>
      </div>

      {/* Summary stats */}
      <div className="grid gap-3 sm:grid-cols-4">
        <StatCard label="账号总数" value={stats.total.toString()} icon={<Inbox className="h-4 w-4" />} />
        <StatCard label="活跃" value={stats.active.toString()} tone="emerald" />
        <StatCard
          label="已达今日上限"
          value={stats.atLimit.toString()}
          tone={stats.atLimit > 0 ? "amber" : "muted"}
        />
        <StatCard
          label="今日已发送（总计）"
          value={stats.totalSent.toString()}
          tone={stats.totalSent > 0 ? "primary" : "muted"}
        />
      </div>

      {nextAvailable ? (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-sm flex items-center gap-2">
          <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700">下一可用</span>
          <span className="font-medium">{nextAvailable.from_email}</span>
          <span className="text-muted-foreground">（今日 {nextAvailable.sent_today ?? 0} / 限额 {nextAvailable.daily_send_limit || "∞"}）</span>
          <span className="ml-auto text-xs text-muted-foreground">轮转模式下超过限额会自动顺延到下一可用账号</span>
        </div>
      ) : (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-700">
          ⚠ 所有活跃账号都已达今日上限；调度器会把剩余邮件重排到次日 00:05。
        </div>
      )}

      <div className="rounded-lg border bg-card text-card-foreground shadow-sm">
        <div className="flex flex-wrap items-center gap-3 border-b p-4">
          <div className="flex items-center gap-2 font-semibold">
            <BarChart3 className="h-4 w-4" /> 邮箱账号限额
          </div>
          <select
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
            title={SORT_OPTIONS.find((o) => o.value === sortKey)?.hint}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                排序：{o.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={resetManualOrder}
            disabled={reorderMutation.isPending}
            title="按创建时间顺序重置手动排序"
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-60"
          >
            <RotateCcw className="h-3 w-3" /> 重置排序
          </button>
          {sortKey === "manual" ? (
            <span className="text-xs text-muted-foreground hidden sm:inline">
              拖动行首的 <GripVertical className="inline h-3 w-3 align-text-bottom" /> 调整轮转顺序
            </span>
          ) : null}
          {reorderMutation.isError ? (
            <span className="text-xs text-destructive">
              保存顺序失败：{reorderMutation.error instanceof Error ? reorderMutation.error.message : "未知错误"}
            </span>
          ) : null}
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="按邮箱 / 名称 / 类型过滤…"
            className="ml-auto rounded-md border bg-background px-3 py-1.5 text-sm w-64"
          />
        </div>

        {accountsQ.isLoading ? (
          <LoadingState message="正在加载账号…" variant="skeleton" skeletonCount={3} />
        ) : accountsQ.isError ? (
          <ErrorState
            error={accountsQ.error}
            onRetry={() => void accountsQ.refetch()}
            title="加载邮箱账号失败"
          />
        ) : accounts.length === 0 ? (
          <EmptyState
            title="还没有邮箱账号"
            message={
              <>去 <a href="/settings/mailboxes" className="underline">已连接邮箱</a> 添加一个。</>
            }
            icon={<Mail className="h-5 w-5" />}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground bg-muted/40">
                <tr>
                  <th className="w-8" aria-label="拖动把手" />
                  <th className="px-4 py-2 text-left font-medium">邮箱</th>
                  <th className="px-4 py-2 text-left font-medium">类型</th>
                  <th className="px-4 py-2 text-left font-medium w-48">今日 / 上限</th>
                  <th className="px-4 py-2 text-left font-medium">状态</th>
                  <th className="px-4 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((acc) => (
                  <QuotaRow
                    key={acc.id}
                    account={acc}
                    isSaving={
                      updateMutation.isPending &&
                      updateMutation.variables?.id === acc.id
                    }
                    onSave={(patch) => onSave(acc.id, patch)}
                    isDragging={dragId === acc.id}
                    isDropTarget={overId === acc.id && dragId !== acc.id}
                    dragHandleProps={{
                      draggable: true,
                      onDragStart: (e) => onDragStart(e, acc.id),
                      onDragEnd,
                    }}
                    rowDropProps={{
                      onDragOver: (e) => onRowDragOver(e, acc.id),
                      onDragLeave: (e) => onRowDragLeave(e, acc.id),
                      onDrop: (e) => onRowDrop(e, acc.id),
                    }}
                  />
                ))}
                {filtered.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-sm text-muted-foreground">
                      无匹配账号
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        限额变更立即生效，但已经在调度器排队的邮件不会重新计数。修改后请到{" "}
        <a href="/settings/mailboxes" className="underline">已连接邮箱</a> 验证账号连接仍然正常。
      </p>
    </div>
  );
}

function StatCard({
  label,
  value,
  icon,
  tone = "muted",
}: {
  label: string;
  value: string;
  icon?: React.ReactNode;
  tone?: "muted" | "emerald" | "amber" | "primary";
}) {
  const toneCls =
    tone === "emerald" ? "text-emerald-600" :
    tone === "amber" ? "text-amber-600" :
    tone === "primary" ? "text-primary" :
    "text-muted-foreground";
  return (
    <div className="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${toneCls}`}>{value}</div>
    </div>
  );
}
