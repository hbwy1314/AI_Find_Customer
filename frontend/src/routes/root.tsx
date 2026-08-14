import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Link, Outlet, useLocation, useNavigate, useRouter } from "@tanstack/react-router";
import { BarChart3, Crosshair, KeyRound, LayoutDashboard, LogOut, Plus, Settings, User, X } from "lucide-react";
import { AuthProvider, useAuth } from "../lib/auth";
import { NotificationBell } from "../lib/notification-bell";
import { api, AUTH_REQUIRED_EVENT } from "../api/client";

function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (next !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    if (next.length < 8) {
      setError("新密码至少 8 位");
      return;
    }
    setSubmitting(true);
    try {
      await api.changePassword(current, next);
      setSuccess(true);
      setTimeout(onClose, 1200);
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改失败");
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-sm rounded-lg border bg-card text-card-foreground shadow-lg">
        <div className="flex items-center justify-between p-4 border-b">
          <div className="flex items-center gap-2 font-semibold">
            <KeyRound className="h-4 w-4" />
            修改密码
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
        <form className="p-4 space-y-3" onSubmit={submit}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="change-pwd-current">当前密码</label>
            <input
              id="change-pwd-current"
              type="password"
              required
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="change-pwd-next">新密码（≥ 8 位）</label>
            <input
              id="change-pwd-next"
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="change-pwd-confirm">确认新密码</label>
            <input
              id="change-pwd-confirm"
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>
          {error ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          ) : null}
          {success ? (
            <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-600">
              修改成功，下次登录请使用新密码。
            </div>
          ) : null}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
            >
              {submitting ? "保存中…" : "保存新密码"}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [showPwd, setShowPwd] = useState(false);

  if (!user) {
    return (
      <Link
        to="/login"
        className="flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        登录
      </Link>
    );
  }

  const handleLogout = async () => {
    await logout();
    navigate({ to: "/login" });
  };

  return (
    <div className="ml-auto flex items-center gap-3">
      <div className="hidden sm:flex items-center gap-1.5 text-sm text-muted-foreground">
        <User className="h-4 w-4" />
        <span className="max-w-[180px] truncate">{user.email}</span>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
          {user.role}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setShowPwd(true)}
        title="修改密码"
        className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted"
      >
        <KeyRound className="h-3.5 w-3.5" />
        修改密码
      </button>
      <button
        type="button"
        onClick={handleLogout}
        className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted"
      >
        <LogOut className="h-3.5 w-3.5" />
        登出
      </button>
      {showPwd ? <ChangePasswordDialog onClose={() => setShowPwd(false)} /> : null}
    </div>
  );
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    const onAuthRequired = () => {
      const path = router.state.location.pathname;
      if (path !== "/login" && path !== "/signup") {
        navigate({ to: "/login", search: { next: path } as never });
      }
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
  }, [loading, navigate, router]);

  // Hard route gate: any non-auth page requires a logged-in user. This fires
  // on first render after `loading` flips to false, AND on every subsequent
  // location/user change, so a stale tab is also recovered.
  useEffect(() => {
    if (loading) return;
    const path = location.pathname;
    const isAuthPage = path === "/login" || path === "/signup";
    if (!user && !isAuthPage) {
      navigate({ to: "/login", search: { next: path } as never });
      return;
    }
    // Reverse gate: a logged-in user landing on /login or /signup should
    // bounce to home (or the original `?next=`). Backup for the LoginPage's
    // own useEffect — handles any path that forgets to redirect.
    if (user && isAuthPage) {
      const params = new URLSearchParams(window.location.search);
      const next = params.get("next");
      const target = next && next.startsWith("/") ? next : "/";
      navigate({ to: target, replace: true });
    }
  }, [loading, user, location.pathname, navigate]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        正在验证登录状态…
      </div>
    );
  }

  const path = location.pathname;
  const isAuthPage = path === "/login" || path === "/signup";
  // Block rendering the protected layout/children while the redirect is in
  // flight so we never flash a half-loaded dashboard to an anonymous visitor.
  if (!user && !isAuthPage) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        未登录，正在跳转到登录页…
      </div>
    );
  }

  return <>{children}</>;
}

export function RootLayout() {
  return (
    <AuthProvider>
      <AuthGate>
        <div className="min-h-screen bg-background">
          <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
            <div className="container mx-auto flex h-14 items-center px-4">
              <Link to="/" className="mr-8 flex items-center gap-2 text-lg font-bold">
                <Crosshair className="h-5 w-5 text-primary" />
                <span>AI Hunter</span>
              </Link>
              <nav className="flex items-center gap-4 text-sm">
                <Link
                  to="/"
                  className="flex items-center gap-1.5 text-muted-foreground transition-colors hover:text-foreground [&.active]:text-foreground"
                >
                  <LayoutDashboard className="h-4 w-4" />
                  任务看板
                </Link>
                <Link
                  to="/hunts/new"
                  className="flex items-center gap-1.5 text-muted-foreground transition-colors hover:text-foreground [&.active]:text-foreground"
                >
                  <Plus className="h-4 w-4" />
                  新建任务
                </Link>
                <Link
                  to="/quotas"
                  className="flex items-center gap-1.5 text-muted-foreground transition-colors hover:text-foreground [&.active]:text-foreground"
                >
                  <BarChart3 className="h-4 w-4" />
                  发送限额
                </Link>
              </nav>
              <div className="ml-auto flex items-center gap-4">
                <Link
                  to="/settings"
                  className="flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground [&.active]:text-foreground"
                >
                  <Settings className="h-4 w-4" />
                  系统设置
                </Link>
                <NotificationBell />
                <UserMenu />
              </div>
            </div>
          </header>
          <main className="container mx-auto px-4 py-8">
            <Outlet />
          </main>
        </div>
      </AuthGate>
    </AuthProvider>
  );
}
