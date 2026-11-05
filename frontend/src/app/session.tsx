import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { ApiError, api, setCsrfToken, setUnauthenticatedHandler } from "@/lib/api";
import type { SessionInfo, User } from "@/lib/types";

interface SessionContextValue {
  user: User;
  isAdmin: boolean;
  signOut: () => Promise<void>;
}

const SessionContext = React.createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const ctx = React.useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionGate>");
  return ctx;
}

export function useSessionQuery() {
  return useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        const s = await api<SessionInfo>("/auth/session");
        setCsrfToken(s.csrf_token);
        return s;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setCsrfToken(null);
          return null;
        }
        throw e;
      }
    },
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function SessionGate({ signIn, loading, children }: { signIn: React.ReactNode; loading: React.ReactNode; children: React.ReactNode }) {
  const qc = useQueryClient();
  const { data, isPending } = useSessionQuery();

  React.useEffect(() => {
    setUnauthenticatedHandler(() => qc.setQueryData(["session"], null));
    return () => setUnauthenticatedHandler(null);
  }, [qc]);

  const value = React.useMemo<SessionContextValue | null>(() => {
    if (!data) return null;
    return {
      user: data.user,
      isAdmin: data.user.role === "admin",
      signOut: async () => {
        try {
          await api("/auth/logout", { method: "POST" });
        } finally {
          setCsrfToken(null);
          qc.clear();
          qc.setQueryData(["session"], null);
        }
      },
    };
  }, [data, qc]);

  if (isPending) return <>{loading}</>;
  if (!value) return <>{signIn}</>;
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
