/**
 * /settings/email-test — central place to send & receive test emails.
 *
 * Pick an account, send a real test message, then poll the inbox to see if it
 * came back. Used for verifying new account wiring without polluting real
 * campaigns.
 */

import { useState } from "react";
import { Inbox, Loader2, Send } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { api, type EmailAccountRow, type TestInboxItem } from "../api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function EmailTestPage() {
  const accountsQ = useQuery({
    queryKey: ["email-accounts"],
    queryFn: api.listEmailAccounts,
  });
  const accounts = accountsQ.data?.accounts ?? [];

  const [accountId, setAccountId] = useState<string>("");
  const [toEmail, setToEmail] = useState<string>("");
  const [subject, setSubject] = useState<string>("");
  const [body, setBody] = useState<string>("");
  const [minutes, setMinutes] = useState<number>(10);

  const [sending, setSending] = useState(false);
  const [checking, setChecking] = useState(false);
  const [sendResult, setSendResult] = useState<
    { ok: boolean; msg: string; messageId?: string; sentAt?: string } | null
  >(null);
  const [inboxItems, setInboxItems] = useState<TestInboxItem[] | null>(null);
  const [inboxAt, setInboxAt] = useState<string | null>(null);

  // Default `to` to the selected account's own address — a quick smoke test
  // proves the round trip without needing a second address.
  const selected: EmailAccountRow | undefined = accounts.find(
    (a) => a.id === accountId,
  );
  const effectiveTo =
    toEmail.trim() || selected?.from_email || "";

  const doSend = async () => {
    if (!accountId || !effectiveTo) return;
    setSending(true);
    setSendResult(null);
    try {
      const res = await api.sendTestEmail(
        accountId,
        effectiveTo,
        subject,
        body,
      );
      const sent = (res as any).sent ?? {};
      setSendResult({
        ok: Boolean(sent.ok),
        msg: sent.ok
          ? `已发送到 ${res.to_email}（provider_message_id=${sent.provider_message_id ?? "?"}）`
          : sent.error || "发送失败",
        messageId: sent.provider_message_id,
        sentAt: sent.sent_at,
      });
    } catch (e) {
      setSendResult({
        ok: false,
        msg: e instanceof Error ? e.message : "发送失败",
      });
    } finally {
      setSending(false);
    }
  };

  const doCheck = async () => {
    if (!accountId) return;
    setChecking(true);
    setInboxItems(null);
    try {
      const res = await api.fetchTestInbox(accountId, minutes, 15);
      setInboxItems((res as any).items ?? []);
      setInboxAt(new Date().toLocaleString());
    } catch (e) {
      setInboxItems([]);
      alert(e instanceof Error ? e.message : "拉取收件箱失败");
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">邮件收发测试</h1>
        <p className="text-muted-foreground mt-1">
          选一个账号，发一封测试邮件，再去拉它的收件箱看回执。用来诊断 SMTP/IMAP 或 Microsoft Graph 是否真的通了。
        </p>
      </div>

      <div className="rounded-lg border bg-card p-6 space-y-5">
        {/* Account picker */}
        <div className="space-y-1.5">
          <Label>发件账号</Label>
          <select
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            disabled={accountsQ.isLoading}
          >
            <option value="">— 选择账号 —</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.from_email} ({a.provider_type})
                {a.daily_send_limit ? ` · 今日 ${a.sent_today ?? 0}/${a.daily_send_limit}` : ""}
              </option>
            ))}
          </select>
          {accountsQ.isLoading ? (
            <p className="text-xs text-muted-foreground">加载中…</p>
          ) : accounts.length === 0 ? (
            <p className="text-xs text-muted-foreground">还没有账号，先去 <a href="/settings/mailboxes" className="underline">已连接邮箱</a> 添加。</p>
          ) : null}
        </div>

        {/* To + subject */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label>收件人</Label>
            <Input
              type="email"
              value={toEmail}
              onChange={(e) => setToEmail(e.target.value)}
              placeholder={selected?.from_email || "you@yourdomain.com"}
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground">留空 = 发送到本账号自己（最快验证通路）。</p>
          </div>
          <div className="space-y-1.5">
            <Label>主题（可选）</Label>
            <Input
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="[AI Hunter test] ..."
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>正文（可选）</Label>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={4}
            placeholder="留空使用默认测试正文"
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label>查看最近（分钟）</Label>
            <Input
              type="number"
              min={1}
              max={1440}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value) || 10)}
            />
          </div>
          <div className="space-y-1.5">
            <Label>&nbsp;</Label>
            <p className="text-xs text-muted-foreground pt-2">
              点击"发一封测试"会用上面配置真实发送；点击"查收件箱"会从该账号的 inbox 拉近 {minutes} 分钟的邮件。
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button
            type="button"
            onClick={doSend}
            disabled={sending || !accountId || !effectiveTo}
          >
            {sending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Send className="mr-2 h-4 w-4" />}
            {sending ? "发送中…" : "发一封测试邮件"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={doCheck}
            disabled={checking || !accountId}
          >
            {checking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Inbox className="mr-2 h-4 w-4" />}
            {checking ? "拉取中…" : "查收件箱"}
          </Button>
        </div>
      </div>

      {/* Send result */}
      {sendResult ? (
        <div
          className={`rounded-lg border px-4 py-3 text-sm ${
            sendResult.ok
              ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-700"
              : "border-destructive/30 bg-destructive/5 text-destructive"
          }`}
        >
          <div className="font-medium">
            {sendResult.ok ? "✓ 发送成功" : "✗ 发送失败"}
          </div>
          <div className="mt-1 text-xs">{sendResult.msg}</div>
          {sendResult.messageId ? (
            <div className="mt-1 text-[10px] text-muted-foreground font-mono">
              message_id={sendResult.messageId}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Inbox result */}
      {inboxItems !== null ? (
        <div className="rounded-lg border bg-card">
          <div className="flex items-center justify-between px-4 py-2 border-b text-sm">
            <div className="font-semibold flex items-center gap-2">
              <Inbox className="h-4 w-4" />
              {selected?.from_email || "账号"} 收件箱
            </div>
            <div className="text-xs text-muted-foreground">
              {inboxItems.length} 封（近 {minutes} 分钟） · 拉取于 {inboxAt}
            </div>
          </div>
          {inboxItems.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-muted-foreground">
              最近 {minutes} 分钟没有新邮件
            </div>
          ) : (
            <ul className="divide-y">
              {inboxItems.map((m) => (
                <li key={m.id} className="px-4 py-3 text-sm">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="font-medium">
                      {m.from_name || m.from_email || "(未知发件人)"}
                    </span>
                    <span className="font-mono text-xs text-muted-foreground">
                      {m.from_email}
                    </span>
                    {m.conversation_id ? (
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground font-mono">
                        conv={m.conversation_id.slice(0, 8)}…
                      </span>
                    ) : null}
                  </div>
                  <div className="font-medium mt-0.5">
                    {m.subject || "(无主题)"}
                  </div>
                  <div className="text-xs text-muted-foreground line-clamp-2 mt-1">
                    {m.snippet}
                  </div>
                  <div className="text-[10px] text-muted-foreground mt-1">
                    {m.received_at}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
