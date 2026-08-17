/**
 * Reusable data-state components for list / table / detail views.
 *
 * The old "length === 0" empty-state pattern was misleading — when
 * the underlying query was still loading, the empty state would
 * render as if there were no data, leaving the user wondering
 * whether the page is broken or whether they really have nothing
 * to look at.
 *
 * These three states give every list view the same loading/error/
 * empty vocabulary so the UI is consistent across the app and the
 * user can always tell whether they're looking at "still loading",
 * "fetch failed", or "actually empty".
 */

import { AlertCircle, Inbox, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface LoadingStateProps {
  /** Short label for the data being loaded. Shown next to the spinner. */
  message?: string;
  /** Render a skeleton block instead of a centered spinner. Use for
   *  list / table views where the final layout is known. */
  variant?: "spinner" | "skeleton";
  /** Number of skeleton rows / cards to draw. Defaults to 4. */
  skeletonCount?: number;
  className?: string;
}

export function LoadingState({
  message = "加载中…",
  variant = "spinner",
  skeletonCount = 4,
  className,
}: LoadingStateProps) {
  if (variant === "skeleton") {
    return (
      <div
        className={cn("space-y-2", className)}
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        {Array.from({ length: skeletonCount }).map((_, i) => (
          <div
            key={i}
            className="h-10 rounded-md border bg-muted/30 animate-pulse"
          />
        ))}
        <span className="sr-only">{message}</span>
      </div>
    );
  }
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-md border border-dashed p-8 text-sm text-muted-foreground",
        className,
      )}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <Loader2 className="h-5 w-5 animate-spin" />
      <span>{message}</span>
    </div>
  );
}

export interface ErrorStateProps {
  /** Underlying error object, or a pre-formatted string. */
  error?: unknown;
  /** Optional retry handler — wired up to the query's `refetch`. */
  onRetry?: () => void;
  /** Short title. Defaults to "加载失败". */
  title?: string;
  className?: string;
}

export function ErrorState({
  error,
  onRetry,
  title = "加载失败",
  className,
}: ErrorStateProps) {
  const detail = formatErrorDetail(error);
  return (
    <div
      className={cn(
        "rounded-md border border-destructive/40 bg-destructive/5 p-6 text-sm",
        className,
      )}
      role="alert"
    >
      <div className="flex items-start gap-3">
        <AlertCircle className="h-5 w-5 mt-0.5 text-destructive shrink-0" />
        <div className="flex-1 min-w-0 space-y-2">
          <p className="font-medium text-destructive">{title}</p>
          {detail ? (
            <p className="text-muted-foreground break-words">{detail}</p>
          ) : null}
          {onRetry ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onRetry}
              className="mt-1"
            >
              重试
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export interface EmptyStateProps {
  /** Short title. */
  title: string;
  /** Optional supporting line. */
  message?: string;
  /** Lucide icon name. Defaults to Inbox. */
  icon?: React.ReactNode;
  /** Optional CTA button (e.g. "Create a hunt"). */
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({
  title,
  message,
  icon,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground",
        className,
      )}
    >
      <div className="flex flex-col items-center gap-2">
        <div className="text-muted-foreground/70">
          {icon ?? <Inbox className="h-5 w-5" />}
        </div>
        <p className="font-medium text-foreground">{title}</p>
        {message ? <p className="text-muted-foreground">{message}</p> : null}
        {action ? <div className="mt-2">{action}</div> : null}
      </div>
    </div>
  );
}

function formatErrorDetail(error: unknown): string | null {
  if (error == null) return null;
  if (typeof error === "string") return error;
  if (error instanceof Error) {
    return error.message || error.name || null;
  }
  if (typeof error === "object" && "message" in error) {
    const msg = (error as { message: unknown }).message;
    if (typeof msg === "string" && msg) return msg;
  }
  try {
    return JSON.stringify(error);
  } catch {
    return null;
  }
}
