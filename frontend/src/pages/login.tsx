import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { LogoMark } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import { Notice } from "@/components/ui/misc";
import { api, errorMessage, setCsrfToken } from "@/lib/api";
import type { SessionInfo } from "@/lib/types";

const schema = z.object({
  username: z.string().trim().min(1, "Enter your username."),
  password: z.string().min(1, "Enter your password."),
});
type Values = z.infer<typeof schema>;

export function LoginPage() {
  const qc = useQueryClient();
  const [error, setError] = React.useState<string | null>(null);
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
        <p className="mb-5 text-[13px] text-muted">Use the account your Lanvex administrator created for you.</p>
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
      </div>
    </div>
  );
}
