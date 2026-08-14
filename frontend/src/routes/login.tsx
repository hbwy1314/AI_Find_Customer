import { useEffect, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useAuth } from "../lib/auth";
import { Crosshair, LogIn } from "lucide-react";

export function LoginPage() {
  const { login, signupOpen, user } = useAuth();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { next?: string };
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Once `user` is set (whether via the form submit below, an SSO callback, or
  // any other path), kick the visitor out of the auth page. Doing this in a
  // dedicated effect — rather than only in the click handler — also covers the
  // case where TanStack Router's `navigate` resolves on a later tick than the
  // React render that flips `user`, which would otherwise let the AuthGate
  // briefly re-render the LoginPage on top of the new state.
  useEffect(() => {
    if (user) {
      const next = search?.next && search.next.startsWith("/") ? search.next : "/";
      navigate({ to: next, replace: true });
    }
  }, [user, search?.next, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email.trim(), password);
      // Navigation is handled by the useEffect above once `user` flips.
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-lg border bg-card text-card-foreground shadow-sm">
        <div className="p-6 space-y-1 border-b">
          <div className="flex items-center gap-2 text-lg font-bold">
            <Crosshair className="h-5 w-5 text-primary" />
            <span>Ai Hunter</span>
          </div>
          <p className="text-sm text-muted-foreground">登录到 AI Hunter 智能获客工作台</p>
        </div>
        <form className="p-6 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="email">邮箱</label>
            <input
              id="email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              placeholder="you@company.com"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="password">密码</label>
            <input
              id="password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              placeholder="••••••••"
            />
          </div>
          {error ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          ) : null}
          <button
            type="submit"
            disabled={submitting}
            className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
          >
            <LogIn className="h-4 w-4" />
            {submitting ? "登录中…" : "登录"}
          </button>
          {signupOpen ? (
            <p className="text-center text-sm text-muted-foreground">
              还没有账号？{" "}
              <Link to="/signup" className="text-primary hover:underline">
                创建管理员账号
              </Link>
            </p>
          ) : null}
        </form>
      </div>
    </div>
  );
}
