import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleSlash, Plus, RefreshCw, XCircle } from "lucide-react";
import * as React from "react";
import { useLocation } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Badge, Notice } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { type ThemePref, useTheme } from "@/app/theme";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { NetBoxStatus, Role, User } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { useNetBoxLabel } from "@/lib/system";
import { MonitoringSection } from "@/pages/settings-monitoring";

export function SettingsPage() {
  const { isAdmin } = useSession();
  const { hash } = useLocation();
  React.useEffect(() => {
    if (!hash) return;
    const t = setTimeout(() => document.getElementById(hash.slice(1))?.scrollIntoView({ block: "start" }), 50);
    return () => clearTimeout(t);
  }, [hash]);
  return (
    <>
      <PageHeader title="Settings" />
      <div className="grid max-w-4xl gap-10">
        <ConnectionSection />
        <AppearanceSection />
        <AccountSection />
        {isAdmin && <UsersSection />}
        {isAdmin && <SignInSection />}
        {isAdmin && (
          <div id="monitoring" className="scroll-mt-6">
            <MonitoringSection />
          </div>
        )}
      </div>
    </>
  );
}

function Check({ ok, label, pending }: { ok: boolean | null; label: string; pending?: boolean }) {
  const Icon = pending ? RefreshCw : ok === null ? CircleSlash : ok ? CheckCircle2 : XCircle;
  return (
    <li className="flex items-center gap-2 text-[13px]">
      <Icon className={cn("size-4", pending && "animate-spin text-faint", !pending && (ok === null ? "text-faint" : ok ? "text-ok" : "text-danger"))} aria-hidden />
      <span>{label}</span>
    </li>
  );
}

function ConnectionSection() {
  const nbName = useNetBoxLabel();
  const q = useQuery({ queryKey: ["netbox-status"], queryFn: () => api<NetBoxStatus>("/system/netbox") });
  const s = q.data;
  return (
    <Section
      title={`${nbName} connection`}
      actions={
        <Button size="sm" onClick={() => void q.refetch()} loading={q.isFetching}>
          Test connection
        </Button>
      }
    >
      <div className="grid gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
        {s && (
          <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-1.5 text-[13px]">
            <dt className="text-muted">URL</dt>
            <dd className="addr">{s.url ?? "Not set"}</dd>
            <dt className="text-muted">Version</dt>
            <dd>{s.version ?? "—"}</dd>
            <dt className="text-muted">Response time</dt>
            <dd>{s.latency_ms != null ? `${s.latency_ms} ms` : "—"}</dd>
          </dl>
        )}
        <ul className="grid gap-1.5" aria-live="polite">
          <Check pending={q.isFetching} ok={s ? s.configured : null} label="URL and token configured" />
          <Check pending={q.isFetching} ok={s?.configured ? s.reachable : null} label={`${nbName} reachable`} />
          <Check pending={q.isFetching} ok={s?.reachable ? s.authenticated : null} label="API token accepted" />
          <Check pending={q.isFetching} ok={s?.reachable ? !!s.supported : null} label={`Version ${s?.minimum_version ?? "4.2"} or newer`} />
        </ul>
        {s?.message && <Notice tone={s.reachable ? "warn" : "danger"}>{s.message}</Notice>}
        {s && !s.configured && (
          <Notice title="How to connect">
            Set <strong>LANVEX_NETBOX_URL</strong> and <strong>LANVEX_NETBOX_TOKEN</strong> in the server environment (see <strong>.env.example</strong>) and restart Lanvex. Use a token with
            only the permissions Lanvex needs: read on IPAM, DCIM and tenancy; write on prefixes, IP addresses, VLANs, interfaces and tags.
          </Notice>
        )}
      </div>
    </Section>
  );
}

function AppearanceSection() {
  const { theme, setTheme } = useTheme();
  const options: { v: ThemePref; label: string }[] = [
    { v: "light", label: "Light" },
    { v: "dark", label: "Dark" },
    { v: "system", label: "Match system" },
  ];
  return (
    <Section title="Appearance">
      <div role="radiogroup" aria-label="Theme" className="inline-grid w-fit grid-cols-3 gap-1 rounded-[var(--radius-control)] bg-surface-2 p-1">
        {options.map((o) => (
          <button
            key={o.v}
            role="radio"
            aria-checked={theme === o.v}
            onClick={() => setTheme(o.v)}
            className={cn("rounded px-3 py-1 text-[13px]", theme === o.v ? "bg-surface font-medium shadow-sm" : "text-muted hover:text-ink")}
          >
            {o.label}
          </button>
        ))}
      </div>
    </Section>
  );
}

function AccountSection() {
  const { user } = useSession();
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [repeat, setRepeat] = React.useState("");
  const change = useMutation({
    mutationFn: () => api("/auth/password", { method: "POST", body: { current_password: current, new_password: next } }),
    onSuccess: () => {
      toast.success("Password changed", { description: "Your other sessions were signed out." });
      setCurrent("");
      setNext("");
      setRepeat("");
    },
  });
  const mismatch = repeat.length > 0 && repeat !== next;
  const role = user.role === "admin" ? "administrator" : "read-only";
  if (user.auth_source !== "local") {
    return (
      <Section
        title="Your account"
        description={`Signed in as ${user.username}, ${role}, with ${user.auth_source === "directory" ? "the directory" : "single sign-on"}. Your password and groups are managed there.`}
      >
        {null}
      </Section>
    );
  }
  return (
    <Section title="Your account" description={`Signed in as ${user.username}, ${role}.`}>
      <form
        className="grid max-w-md gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!mismatch && next.length >= 12) change.mutate();
        }}
      >
        <Field label="Current password" htmlFor="pw-current" error={fieldError(change.error, "current_password")}>
          <Input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        </Field>
        <Field label="New password" htmlFor="pw-new" hint="12 characters or more" error={fieldError(change.error, "password", "new_password")}>
          <Input id="pw-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={12} />
        </Field>
        <Field label="Repeat new password" htmlFor="pw-repeat" error={mismatch ? "Passwords do not match." : undefined}>
          <Input id="pw-repeat" type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required aria-invalid={mismatch} />
        </Field>
        {change.error && !fieldError(change.error, "current_password", "password", "new_password") && <Notice tone="danger">{errorMessage(change.error)}</Notice>}
        <Button type="submit" className="justify-self-start" loading={change.isPending} disabled={mismatch || next.length < 12 || !current}>
          Change password
        </Button>
      </form>
    </Section>
  );
}

interface SignInConfiguration {
  directory: { type: "ad" | "ldap"; urls: string[]; encrypted: boolean; user_base: string; admin_group: string | null; readonly_group: string | null } | null;
  sso: { name: string; issuer: string; client_id: string; callback: string; admin_groups: string[]; readonly_groups: string[] } | null;
}

function SignInSection() {
  const conf = useQuery({ queryKey: ["auth-configuration"], queryFn: () => api<SignInConfiguration>("/auth/configuration") });
  const test = useMutation({ mutationFn: () => api<{ ok: boolean; message: string }>("/auth/directory/test", { method: "POST" }) });
  const d = conf.data?.directory;
  const sso = conf.data?.sso;
  const row = (label: string, value: React.ReactNode) => (
    <div className="grid grid-cols-[10rem_1fr] gap-3 py-1.5 text-[13px]">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{value}</dd>
    </div>
  );
  return (
    <Section title="Sign-in" description="Set in .env; see docs/sign-in.md.">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
          <p className="mb-2 flex items-center gap-2 text-[13px] font-medium">
            Directory {d ? <Badge tone="ok">{d.type === "ad" ? "Active Directory" : "LDAP"}</Badge> : <Badge>Off</Badge>}
          </p>
          {d && (
            <>
              <dl>
                {row("Servers", <span className="addr">{d.urls.join(", ")}</span>)}
                {row("Users", <span className="addr">{d.user_base}</span>)}
                {row("Administrators", <span className="addr">{d.admin_group ?? "—"}</span>)}
                {row("Read-only", <span className="addr">{d.readonly_group ?? "—"}</span>)}
              </dl>
              {!d.encrypted && (
                <Notice tone="warn" className="mt-2">
                  Passwords cross the network unencrypted: use ldaps:// or STARTTLS.
                </Notice>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <Button size="sm" variant="secondary" onClick={() => test.mutate()} loading={test.isPending}>
                  Test directory
                </Button>
                {test.data && (
                  <span className={cn("flex items-center gap-1.5 text-[13px]", test.data.ok ? "text-ok" : "text-danger")}>
                    {test.data.ok ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
                    {test.data.message}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
        <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
          <p className="mb-2 flex items-center gap-2 text-[13px] font-medium">
            Single sign-on {sso ? <Badge tone="ok">{sso.name}</Badge> : <Badge>Off</Badge>}
          </p>
          {sso && (
            <dl>
              {row("Provider", <span className="addr">{sso.issuer}</span>)}
              {row("Client ID", <span className="addr">{sso.client_id}</span>)}
              {row("Redirect URI", <span className="addr">{sso.callback}</span>)}
              {row("Administrators", sso.admin_groups.join(", ") || "—")}
              {row("Read-only", sso.readonly_groups.join(", ") || "—")}
            </dl>
          )}
        </div>
      </div>
    </Section>
  );
}

function UsersSection() {
  const qc = useQueryClient();
  const { user: me } = useSession();
  const [adding, setAdding] = React.useState(false);
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const update = useMutation({
    mutationFn: ({ id, ...body }: { id: string; role?: Role; is_active?: boolean }) => api<User>(`/users/${id}`, { method: "PATCH", body }),
    onSuccess: (u) => {
      toast.success(`Updated ${u.username}`);
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: (u: User) => api<void>(`/users/${u.id}`, { method: "DELETE" }),
    onSuccess: (_, u) => {
      toast.success(`Deleted ${u.username}`, { description: "The audit log keeps its name." });
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const columns: Column<User>[] = [
    {
      key: "name",
      header: "User",
      cell: (u) => (
        <span>
          <span className="font-medium">{u.display_name || u.username}</span>
          {u.display_name && <span className="text-muted"> {u.username}</span>}
          {u.id === me.id && <span className="text-faint"> (you)</span>}
        </span>
      ),
    },
    {
      key: "source",
      header: "Sign-in",
      cell: (u) => (u.auth_source === "directory" ? <Badge>Directory</Badge> : u.auth_source === "sso" ? <Badge>SSO</Badge> : <span className="text-muted">Password</span>),
    },
    {
      key: "role",
      header: "Role",
      cell: (u) => (
        <Select
          aria-label={`Role for ${u.username}`}
          className="h-7 w-36"
          value={u.role}
          title={u.auth_source !== "local" ? "Set by the account's groups" : undefined}
          disabled={update.isPending || u.auth_source !== "local"}
          onChange={(e) => update.mutate({ id: u.id, role: e.target.value as Role })}
        >
          <option value="admin">Administrator</option>
          <option value="readonly">Read-only</option>
        </Select>
      ),
    },
    { key: "status", header: "Status", cell: (u) => (u.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Deactivated</Badge>) },
    { key: "login", header: "Last sign-in", cell: (u) => <span className="text-muted">{relativeTime(u.last_login_at)}</span> },
    {
      key: "act",
      header: <span className="sr-only">Actions</span>,
      cell: (u) =>
        u.id !== me.id && (
          <span className="flex justify-end gap-2">
            <Button size="sm" variant={u.is_active ? "danger-outline" : "secondary"} onClick={() => update.mutate({ id: u.id, is_active: !u.is_active })} disabled={update.isPending}>
              {u.is_active ? "Deactivate" : "Reactivate"}
            </Button>
            {/* Deactivate first, then delete: two steps, so no one is deleted by a slip. */}
            {!u.is_active && (
              <Button size="sm" variant="danger-outline" aria-label={`Delete ${u.username}`} onClick={() => remove.mutate(u)} disabled={remove.isPending}>
                Delete
              </Button>
            )}
          </span>
        ),
    },
  ];
  return (
    <Section
      title="Users"
      actions={
        <Button size="sm" onClick={() => setAdding(true)}>
          <Plus /> Add user
        </Button>
      }
    >
      <DataTable columns={columns} rows={users.data} rowKey={(u) => u.id} loading={users.isPending} error={users.error} onRetry={() => void users.refetch()} dense />
      <AddUserDialog open={adding} onOpenChange={setAdding} />
    </Section>
  );
}

function AddUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [f, setF] = React.useState({ username: "", display_name: "", password: "", role: "readonly" as Role });
  React.useEffect(() => {
    if (open) setF({ username: "", display_name: "", password: "", role: "readonly" });
  }, [open]);
  const create = useMutation({
    mutationFn: () => api<User>("/users", { method: "POST", body: { ...f, display_name: f.display_name || null } }),
    onSuccess: (u) => {
      toast.success(`Added ${u.username}`, { description: "Share the password with them through a secure channel." });
      void qc.invalidateQueries({ queryKey: ["users"] });
      onOpenChange(false);
    },
  });
  const err = create.error;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Add user">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <DialogBody>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Username" htmlFor="nu-username" error={fieldError(err, "username")}>
                <Input id="nu-username" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required autoComplete="off" />
              </Field>
              <Field label="Display name" htmlFor="nu-display" optional>
                <Input id="nu-display" value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} />
              </Field>
            </div>
            <Field label="Initial password" htmlFor="nu-password" hint="12 characters or more" error={fieldError(err, "password")}>
              <Input id="nu-password" type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required />
            </Field>
            <Field label="Role" htmlFor="nu-role">
              <Select id="nu-role" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as Role })}>
                <option value="readonly">Read-only</option>
                <option value="admin">Administrator</option>
              </Select>
            </Field>
            {err && !fieldError(err, "username", "password") && <Notice tone="danger">{errorMessage(err)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Add user
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
