/**
 * Lightweight React context for the current authenticated user.
 *
 * We deliberately keep this tiny (no React Query, no router-level gate) so that
 * the rest of the app can opt in to useAuth() without affecting existing flows.
 * The route guard is implemented in root.tsx via AuthGate.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { AUTH_REQUIRED_EVENT, api, type AuthUser } from "../api/client";

interface AuthState {
  user: AuthUser | null;
  signupOpen: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
  login: (email: string, password: string) => Promise<AuthUser>;
  signup: (email: string, password: string) => Promise<AuthUser>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [signupOpen, setSignupOpen] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);

  const refresh = useCallback(async () => {
    try {
      const status = await api.signupStatus();
      setSignupOpen(Boolean(status?.open));
    } catch {
      setSignupOpen(false);
    }
    try {
      const me = await api.me();
      setUser(me.user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.login(email, password);
    setUser(result.user);
    setSignupOpen(false);
    return result.user;
  }, []);

  const signup = useCallback(async (email: string, password: string) => {
    const result = await api.signup(email, password);
    setUser(result.user);
    setSignupOpen(false);
    return result.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Global 401 dispatcher — when a 401 comes back from any request, force a
  // re-read of `me` so the UI reflects the now-anonymous state. Root layout
  // also listens for AUTH_REQUIRED_EVENT to navigate to /login.
  useEffect(() => {
    const handler = () => {
      setUser(null);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, handler);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, handler);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, signupOpen, loading, refresh, login, signup, logout }),
    [user, signupOpen, loading, refresh, login, signup, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return ctx;
}
