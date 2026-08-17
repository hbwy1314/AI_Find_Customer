/**
 * Top-header notification bell.
 *
 * Primary delivery is Server-Sent Events via `/api/v1/replies/stream`
 * — when the reply-detection loop matches a new inbound message, the
 * server pushes a `reply` event to every open tab and the badge
 * updates immediately.
 *
 * Polling (`api.fetchRecentNotifications`) is kept as a safety net:
 * - on initial mount to backfill any replies that arrived while the
 *   tab was closed
 * - when the SSE connection is in `CLOSED` state (e.g. the user is
 *   behind a proxy that buffers / strips EventSource). EventSource
 *   auto-reconnects on transient errors, but if it gives up we fall
 *   back to the 30s poll indefinitely.
 *
 * The bell briefly pulses when a new push arrives so the user notices
 * even when the dropdown is collapsed.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Bell, Check, Mail } from "lucide-react";

import { api, type NotificationItem } from "../api/client";

const LAST_SEEN_KEY = "aih_notif_last_seen";
const POLL_INTERVAL_MS = 30_000;
const SSE_PATH = "/api/v1/replies/stream";
const PULSE_MS = 1500;

export function NotificationBell() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [pulse, setPulse] = useState(false);
  const pulseTimer = useRef<number | null>(null);
  const lastSeenRef = useRef<string | null>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const sseFailedRef = useRef<boolean>(false);
  const navigate = useNavigate();

  useEffect(() => {
    lastSeenRef.current = localStorage.getItem(LAST_SEEN_KEY);
    void poll(true);
    const interval = setInterval(() => {
      // Skip the timer poll if SSE is live — saves one request per
      // cycle. SSE itself keeps the list fresh.
      if (sseFailedRef.current) void poll(false);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  // SSE subscription for instant push. EventSource auto-reconnects
  // on transient errors; we only flip into polling-fallback mode if
  // it goes to CLOSED (the browser gives up after some retries).
  useEffect(() => {
    if (typeof EventSource === "undefined") {
      // Old browser / SSR — fall back to polling only.
      sseFailedRef.current = true;
      return;
    }
    const es = new EventSource(SSE_PATH, { withCredentials: true });

    es.addEventListener("reply", (e) => {
      try {
        const item = JSON.parse((e as MessageEvent).data) as NotificationItem;
        if (!item?.id) return;
        setItems((prev) => {
          // Dedupe by id; cap at 30 to match the REST limit.
          const filtered = prev.filter((x) => x.id !== item.id);
          return [item, ...filtered].slice(0, 30);
        });
        setUnread((u) => u + 1);
        triggerPulse();
      } catch (err) {
        console.error("[bell] failed to parse SSE reply event", err);
      }
    });

    es.addEventListener("heartbeat", () => {
      // no-op; the event just keeps the connection alive.
    });

    es.onerror = () => {
      // EventSource auto-reconnects. We don't need to do anything
      // here unless the readyState goes to CLOSED (= browser gave
      // up). Check on the next event loop tick.
      if (es.readyState === EventSource.CLOSED) {
        sseFailedRef.current = true;
      }
    };

    return () => {
      es.close();
    };
  }, []);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  function triggerPulse() {
    setPulse(true);
    if (pulseTimer.current !== null) {
      window.clearTimeout(pulseTimer.current);
    }
    pulseTimer.current = window.setTimeout(() => setPulse(false), PULSE_MS);
  }

  async function poll(forceAll: boolean) {
    try {
      const since = forceAll ? undefined : lastSeenRef.current ?? undefined;
      const res = await api.fetchRecentNotifications(since, 30);
      setItems(res.items);
      // If we polled without a `since` (initial), we don't know what's unread;
      // don't bump the badge. When polling with `since`, every result is unread.
      setUnread(forceAll ? 0 : res.items.length);
    } catch {
      // Silent — bell is best-effort; don't spam the user.
    }
  }

  async function openBell() {
    setOpen((v) => !v);
    if (!open) {
      // Closing in this tick, opening in the next — defer the "mark seen"
      // call so the user actually sees the count drop on the next render.
    } else {
      // Closing: persist last-seen now.
      try {
        const res = await api.markNotificationsSeen();
        lastSeenRef.current = res.last_seen_at;
        localStorage.setItem(LAST_SEEN_KEY, res.last_seen_at);
        setUnread(0);
      } catch {
        // ignore
      }
    }
  }

  const onClick = (n: NotificationItem) => {
    setOpen(false);
    if (n.hunt_id) {
      navigate({ to: "/hunts/$huntId", params: { huntId: n.hunt_id } });
    }
  };

  return (
    <div ref={dropdownRef} className="relative">
      <button
        type="button"
        onClick={openBell}
        title="回信通知"
        className={`relative inline-flex h-9 w-9 items-center justify-center rounded-md border text-muted-foreground hover:text-foreground hover:bg-muted ${
          pulse ? "animate-pulse ring-2 ring-primary/40" : ""
        }`}
      >
        <Bell className="h-4 w-4" />
        {unread > 0 ? (
          <span className="absolute -top-1 -right-1 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-semibold leading-none text-destructive-foreground">
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="absolute right-0 z-50 mt-2 w-96 max-w-[90vw] rounded-lg border bg-card text-card-foreground shadow-lg">
          <div className="flex items-center justify-between border-b px-3 py-2 text-sm">
            <div className="flex items-center gap-2 font-semibold">
              <Mail className="h-4 w-4" /> 回信通知
            </div>
            <button
              type="button"
              onClick={async () => {
                try {
                  const res = await api.markNotificationsSeen();
                  lastSeenRef.current = res.last_seen_at;
                  localStorage.setItem(LAST_SEEN_KEY, res.last_seen_at);
                  setUnread(0);
                } catch { /* noop */ }
              }}
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <Check className="h-3 w-3" /> 全部标为已读
            </button>
          </div>
          {items.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              暂无新回信
            </div>
          ) : (
            <ul className="max-h-96 divide-y overflow-y-auto">
              {items.map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    onClick={() => onClick(n)}
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-muted"
                  >
                    <div className="flex items-baseline gap-2">
                      <span className="font-medium truncate">{n.from_email}</span>
                      <span className="ml-auto text-[10px] text-muted-foreground">
                        {n.received_at?.slice(5, 16) ?? ""}
                      </span>
                    </div>
                    <div className="truncate text-foreground/90">
                      {n.subject || "(无主题)"}
                    </div>
                    {n.snippet ? (
                      <div className="text-xs text-muted-foreground line-clamp-2 mt-0.5">
                        {n.snippet}
                      </div>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
