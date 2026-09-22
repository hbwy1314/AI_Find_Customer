import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Mail, RefreshCw, Check, Eye, EyeOff, Loader2 } from "lucide-react";

import { api, type InboxMessage } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function messageStatus(message: InboxMessage): string {
  if (message.is_auto_reply) return "自动回复";
  if (message.match_status === "matched") return "已关联任务";
  if (message.match_status === "ignored") return "已忽略";
  return "未关联";
}

function InboxDetail({
  message,
  onClose,
  onRead,
}: {
  message: InboxMessage;
  onClose: () => void;
  onRead: (isRead: boolean, syncToGraph: boolean) => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-lg border bg-background shadow-xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 border-b p-5">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold">{message.subject || "无主题"}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {message.from_name || message.from_email} &lt;{message.from_email}&gt; · {formatDate(message.received_at)}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={onClose}>关闭</Button>
        </div>
        <div className="space-y-4 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={message.site_is_read ? "outline" : "default"}>{message.site_is_read ? "站内已读" : "站内未读"}</Badge>
            <Badge variant={message.graph_is_read ? "outline" : "secondary"}>{message.graph_is_read ? "Graph 已读" : "Graph 未读"}</Badge>
            <Badge variant="outline">{messageStatus(message)}</Badge>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => onRead(!message.site_is_read, false)}>
              {message.site_is_read ? <EyeOff className="mr-2 h-4 w-4" /> : <Eye className="mr-2 h-4 w-4" />}
              {message.site_is_read ? "标为未读" : "标为站内已读"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onRead(true, true)}>
              <Check className="mr-2 h-4 w-4" /> 同步 Graph 已读
            </Button>
          </div>
          <div className="rounded-md border bg-muted/20 p-4">
            <p className="whitespace-pre-wrap text-sm leading-6">{message.body_text || message.snippet || "暂无正文"}</p>
          </div>
          {message.matched_sequence_id ? (
            <p className="text-xs text-muted-foreground">已关联邮件序列：{message.matched_sequence_id}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function InboxPage() {
  const qc = useQueryClient();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [selected, setSelected] = useState<InboxMessage | null>(null);
  const inbox = useQuery({
    queryKey: ["inbox-messages", unreadOnly],
    queryFn: () => api.listInboxMessages({ unreadOnly, limit: 100 }),
    refetchInterval: 30000,
  });
  const syncMutation = useMutation({
    mutationFn: () => api.syncInbox(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["inbox-messages"] }),
  });
  const readMutation = useMutation({
    mutationFn: ({ id, isRead, syncToGraph }: { id: string; isRead: boolean; syncToGraph: boolean }) =>
      api.markInboxMessageRead(id, isRead, syncToGraph),
    onSuccess: (result) => {
      setSelected(result.message);
      qc.invalidateQueries({ queryKey: ["inbox-messages"] });
    },
  });

  const messages = inbox.data?.items ?? [];
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">收件箱</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {inbox.data?.shared_inbox_upn ? `共享 Inbox：${inbox.data.shared_inbox_upn}` : "尚未配置共享 Inbox"}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant={unreadOnly ? "default" : "outline"} onClick={() => setUnreadOnly((value) => !value)}>
            <Mail className="mr-2 h-4 w-4" /> {unreadOnly ? "查看全部" : "只看未读"}
          </Button>
          <Button variant="outline" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
            {syncMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
            同步收件箱
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">邮件 ({inbox.data?.unread_count ?? 0} 封站内未读)</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {inbox.isLoading ? (
            <div className="p-8 text-center text-sm text-muted-foreground">正在加载收件箱…</div>
          ) : inbox.isError ? (
            <div className="p-8 text-center text-sm text-destructive">收件箱加载失败</div>
          ) : messages.length === 0 ? (
            <div className="p-8 text-center text-sm text-muted-foreground">暂无邮件</div>
          ) : (
            <div className="divide-y">
              {messages.map((message) => (
                <button
                  type="button"
                  key={message.id}
                  className={`block w-full p-4 text-left transition-colors hover:bg-muted/40 ${message.site_is_read ? "" : "bg-primary/5"}`}
                  onClick={() => setSelected(message)}
                >
                  <div className="flex items-start gap-3">
                    <Mail className={`mt-1 h-4 w-4 shrink-0 ${message.site_is_read ? "text-muted-foreground" : "text-primary"}`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`truncate text-sm ${message.site_is_read ? "font-medium" : "font-bold"}`}>{message.subject || "无主题"}</span>
                        <Badge variant="outline" className="text-[10px]">{messageStatus(message)}</Badge>
                        <Badge variant="secondary" className="text-[10px]">{message.graph_is_read ? "Graph 已读" : "Graph 未读"}</Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">{message.from_name || message.from_email} &lt;{message.from_email}&gt; · {formatDate(message.received_at)}</p>
                      <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{message.snippet || "暂无摘要"}</p>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
      {selected ? (
        <InboxDetail
          message={selected}
          onClose={() => setSelected(null)}
          onRead={(isRead, syncToGraph) => readMutation.mutate({ id: selected.id, isRead, syncToGraph })}
        />
      ) : null}
    </div>
  );
}
