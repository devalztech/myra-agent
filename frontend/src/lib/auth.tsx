/**
 * Client-side auth state: JWT + user persisted in localStorage.
 * SSR-safe — storage is only read after hydration.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { fetchMe } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { AuthResponse, User } from "@/types";

const TOKEN_KEY = "myra.token";
const USER_KEY = "myra.user";

type AuthState = {
  token: string | null;
  user: User | null;
  ready: boolean;
  signIn: (auth: AuthResponse) => void;
  signOut: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const storedToken = window.localStorage.getItem(TOKEN_KEY);
    const storedUser = window.localStorage.getItem(USER_KEY);
    if (!storedToken) {
      setReady(true);
      return;
    }
    setToken(storedToken);
    if (storedUser) {
      try {
        setUser(JSON.parse(storedUser) as User);
      } catch {
        /* ignore corrupt cache */
      }
    }
    // Validate the token against the backend; drop it if it is no longer valid.
    fetchMe(storedToken)
      .then((fresh) => {
        setUser(fresh);
        window.localStorage.setItem(USER_KEY, JSON.stringify(fresh));
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) {
          window.localStorage.removeItem(TOKEN_KEY);
          window.localStorage.removeItem(USER_KEY);
          setToken(null);
          setUser(null);
        }
      })
      .finally(() => setReady(true));
  }, []);

  const signIn = useCallback((auth: AuthResponse) => {
    window.localStorage.setItem(TOKEN_KEY, auth.token);
    window.localStorage.setItem(USER_KEY, JSON.stringify(auth.user));
    setToken(auth.token);
    setUser(auth.user);
    setReady(true);
  }, []);

  const signOut = useCallback(() => {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ token, user, ready, signIn, signOut }),
    [token, user, ready, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>.");
  return ctx;
}
