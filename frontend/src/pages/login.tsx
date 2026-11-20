import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { LogoMark } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import { Notice } from "@/components/ui/misc";
import { api, errorMessage, setCsrfToken } from "@/lib/api";
import type { SessionInfo, SignInMethods } from "@/lib/types";

/** What /auth/sso/callback reports back in ?sso_error=. */
const SSO_ERRORS: Record<string, string> = {
  denied: "This account is in none of the groups allowed to use Lanvex.",
  taken: "Lanvex already has a local user with this name. An administrator can rename or remove it.",
  disabled: "This account is deactivated in Lanvex.",
  expired: "The sign-in took too long or started in another browser. Try again.",
  cancelled: "Sign-in was cancelled.",
  unavailable: "The identity provider did not answer. Try again, or sign in with a password.",
  failed: "Single sign-on did not work. Try again, or ask an administrator to check Lanvex's log.",
};

const schema = z.object({
  username: z.string().trim().min(1, "Enter your username."),
  password: z.string().min(1, "Enter your password."),
});
type Values = z.infer<typeof schema>;

export function LoginPage() {
  const qc = useQueryClient();
  const methods = useQuery({ queryKey: ["auth-methods"], queryFn: () => api<SignInMethods>("/auth/methods"), retry: false });
  const [error, setError] = React.useState<string | null>(() => {
    const code = new URLSearchParams(window.location.search).get("sso_error");
    return code ? (SSO_ERRORS[code] ?? SSO_ERRORS.failed) : null;
  });
  const next = window.location.pathname === "/login" ? "/" : window.location.pathname + window.location.search;
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { username: "", password: "" } });

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const s = await api<SessionInfo>("/auth/login", { method: "POST", body: values });
      setCsrfToken(s.csrf_token);
      qc.setQueryData(["session"], s);
    } catch (e) {
      setError(errorMessage(e));
      form.setValue("password", "");
      form.setFocus("password");
    }
  });

  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <div className="w-full max-w-[360px]">
        <div className="mb-8 flex items-center gap-2.5">
          <LogoMark className="size-8" />
          <div>
            <p className="text-base font-semibold leading-tight">Lanvex</p>
            <p className="text-xs text-muted">Network infrastructure, simplified.</p>
          </div>
        </div>
        <h1 className="mb-1 text-lg font-semibold">Sign in</h1>
        <form onSubmit={onSubmit} className="grid gap-4" noValidate>
          {error && <Notice tone="danger">{error}</Notice>}
          <Field label="Username" htmlFor="username" error={form.formState.errors.username?.message}>
            <Input id="username" autoComplete="username" autoFocus aria-invalid={!!form.formState.errors.username} {...form.register("username")} />
          </Field>
          <Field label="Password" htmlFor="password" error={form.formState.errors.password?.message}>
            <Input id="password" type="password" autoComplete="current-password" aria-invalid={!!form.formState.errors.password} {...form.register("password")} />
          </Field>
          <Button type="submit" variant="primary" size="lg" loading={form.formState.isSubmitting}>
            Sign in
          </Button>
        </form>
        {methods.data?.sso && (
          <>
            <div className="my-5 flex items-center gap-3 text-xs text-faint" aria-hidden>
              <span className="h-px flex-1 bg-line" />
              or
              <span className="h-px flex-1 bg-line" />
            </div>
            <Button variant="secondary" size="lg" className="w-full" asChild>
              <a href={`/api/v1/auth/sso/start?next=${encodeURIComponent(next)}`}>{methods.data.sso}</a>
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
