import * as React from "react";
import { Navigate } from "react-router";
import { useSession } from "@/app/session";
import { LogoMark } from "@/components/logo";
import { Button } from "@/components/ui/button";

/** A path on this site to continue to after signing in; anything else (other sites, //host) is ignored. */
export function safeNext(value: string | null): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return null;
  return value;
}

/**
 * /login?next=... once signed in. Lanvex Core sends people here when their Lanvex session has run out;
 * pages under /core/ are not part of this app, so they are opened with a full page load.
 */
export function LoginRedirect() {
  const next = safeNext(new URLSearchParams(window.location.search).get("next"));
  const toCore = next !== null && (next === "/core" || next.startsWith("/core/"));
  React.useEffect(() => {
    if (toCore) window.location.replace(next);
  }, [toCore, next]);
  if (toCore) return null;
  return <Navigate to={next ?? "/"} replace />;
}

/**
 * /signout: where "Log out" in Lanvex Core leads. Coming from this site it signs out right away;
 * a link from elsewhere has to be confirmed, so another site cannot sign people out.
 */
export function SignOutPage() {
  const { signOut } = useSession();
  const [busy, setBusy] = React.useState(false);
  const fromHere = React.useMemo(() => {
    try {
      return document.referrer !== "" && new URL(document.referrer).origin === window.location.origin;
    } catch {
      return false;
    }
  }, []);
  const run = React.useCallback(async () => {
    setBusy(true);
    await signOut();
    window.location.replace("/");
  }, [signOut]);
  React.useEffect(() => {
    if (fromHere) void run();
  }, [fromHere, run]);

  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <div className="grid w-full max-w-[360px] justify-items-start gap-4">
        <LogoMark className="size-8" />
        {fromHere ? (
          <p className="text-[13px] text-muted" aria-live="polite">
            Signing out…
          </p>
        ) : (
          <>
            <h1 className="text-lg font-semibold">Sign out of Lanvex?</h1>
            <p className="text-[13px] text-muted">This also signs you out of Lanvex Core.</p>
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => void run()} loading={busy}>
                Sign out
              </Button>
              <Button variant="secondary" onClick={() => window.location.replace("/")}>
                Stay signed in
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
