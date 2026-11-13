import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Plus, Server as ServerIcon } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { Badge, EmptyState, Notice } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { ApiError, api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { HostKeyScan, Server, Snapshot } from "@/lib/types";
import { useSystemInfo } from "@/lib/system";
import { cn, relativeTime } from "@/lib/utils";
import {
  ConnectionBadge,
  CredentialsForm,
  EMPTY_CREDENTIALS,
  HostKeyVerification,
  InstallKeyInstructions,
  credentialPayload,
  credentialsComplete,
  type CredentialDraft,
} from "./shared";
import { useNetBoxLabel } from "@/lib/system";

export { useSystemInfo } from "@/lib/system";

export function ServersPage() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const navigate = useNavigate();
  const [adding, setAdding] = React.useState(false);
  const info = useSystemInfo();
  const list = useQuery({ queryKey: ["servers"], queryFn: () => api<Server[]>("/servers") });
  const canStore = info.data?.credential_storage !== false;

  const columns: Column<Server>[] = [
    {
      key: "name",
      header: "Server",
      cell: (s) => (
        <span className="grid">
          <span className="font-medium">{s.name}</span>
          <span className="addr text-xs text-muted">
            {s.username}@{s.address}
            {s.port !== 22 ? `:${s.port}` : ""}
          </span>
        </span>
      ),
    },
    { key: "status", header: "Connection", cell: (s) => <ConnectionBadge status={s.last_status} /> },
    {
      key: "os",
      header: "Operating system",
      cell: (s) =>
        s.os_pretty_name ? (
          <span className="flex items-center gap-2">
            {s.os_pretty_name}
            {s.os_supported === false && <Badge tone="warn">Unsupported</Badge>}
          </span>
        ) : (
          <span className="text-faint">Not read yet</span>
        ),
    },
    { key: "stack", header: "Network stack", cell: (s) => s.network_stack ?? <span className="text-faint">—</span> },
    { key: "netbox", header: `${nbName}`, cell: (s) => (s.netbox ? <span className="text-muted">Linked</span> : <span className="text-faint">Not linked</span>) },
    {
      key: "read",
      header: "Last read",
      cell: (s) => <span className="text-muted" title={s.last_collected_at ?? undefined}>{s.last_collected_at ? relativeTime(s.last_collected_at) : "Never"}</span>,
    },
  ];

  return (
    <>
      <PageHeader
        title="Servers"
        description="Linux servers Lanvex reads over SSH: interfaces, addresses, routes and DNS as they actually are. Lanvex does not change server configuration."
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setAdding(true)} disabled={!canStore}>
              <Plus /> Add server
            </Button>
          )
        }
      />
      {!canStore && (
        <Notice tone="warn" title="Credential storage is not configured" className="mb-4">
          Lanvex encrypts server credentials with <strong>LANVEX_SECRET_KEY</strong>. Generate one with <span className="addr">lanvex generate-secret-key</span>, set it in the server
          environment, and restart Lanvex.
        </Notice>
      )}
      <DataTable
        columns={columns}
        rows={list.data}
        rowKey={(s) => s.id}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        onRowClick={(s) => navigate(`/servers/${s.id}`)}
        rowLabel={(s) => `Open ${s.name}`}
        empty={
          <EmptyState
            icon={ServerIcon}
            title="No servers yet"
            action={
              isAdmin &&
              canStore && (
                <Button variant="primary" onClick={() => setAdding(true)}>
                  <Plus /> Add server
                </Button>
              )
            }
          >
            Add Ubuntu Server, Debian or Fedora Server hosts to see their live network configuration next to what {nbName} documents.
          </EmptyState>
        }
      />
      <AddServerDialog open={adding} onOpenChange={setAdding} onDone={(id) => navigate(`/servers/${id}`)} />
    </>
  );
}

const STEPS = ["Connection", "Host key", "Sign-in", "Finish"] as const;

function AddServerDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; onDone: (id: string) => void }) {
  const qc = useQueryClient();
  const [step, setStep] = React.useState(0);
  const [conn, setConn] = React.useState({ name: "", address: "", port: "22", username: "lanvex" });
  const [scan, setScan] = React.useState<HostKeyScan | null>(null);
  const [confirmed, setConfirmed] = React.useState(false);
  const [creds, setCreds] = React.useState<CredentialDraft>(EMPTY_CREDENTIALS);
  const [server, setServer] = React.useState<Server | null>(null);
  const [busy, setBusy] = React.useState<"scan" | "create" | "test" | "collect" | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [test, setTest] = React.useState<{ status: string; message: string } | null>(null);

  React.useEffect(() => {
    if (open) {
      setStep(0);
      setConn({ name: "", address: "", port: "22", username: "lanvex" });
      setScan(null);
      setConfirmed(false);
      setCreds(EMPTY_CREDENTIALS);
      setServer(null);
      setError(null);
      setTest(null);
    }
  }, [open]);

  const port = Number(conn.port);
  const portOk = Number.isInteger(port) && port >= 1 && port <= 65535;

  async function doScan() {
    setBusy("scan");
    setError(null);
    try {
      setScan(await api<HostKeyScan>("/servers/scan-host-key", { method: "POST", body: { address: conn.address.trim(), port } }));
      setConfirmed(false);
      setStep(1);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  async function doCreate() {
    if (!scan) return;
    setBusy("create");
    setError(null);
    try {
      const r = await api<{ server: Server }>("/servers", {
        method: "POST",
        body: {
          name: conn.name.trim() || conn.address.trim(),
          address: conn.address.trim(),
          port,
          username: conn.username.trim(),
          host_key: { public_key: scan.public_key, fingerprint_sha256: scan.fingerprint_sha256 },
          credentials: credentialPayload(creds),
        },
      });
      setServer(r.server);
      setStep(3);
      void qc.invalidateQueries({ queryKey: ["servers"] });
      toast.success(`Added ${r.server.name}`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  async function doTest() {
    if (!server) return;
    setBusy("test");
    try {
      setTest(await api<{ status: string; message: string }>(`/servers/${server.id}/test`, { method: "POST" }));
    } catch (e) {
      setTest({ status: "error", message: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  }

  async function doCollect() {
    if (!server) return;
    setBusy("collect");
    try {
      const snap = await api<Snapshot>(`/servers/${server.id}/collect`, { method: "POST" });
      void qc.invalidateQueries({ queryKey: ["servers"] });
      if (snap.status !== "ok") toast.error(snap.message ?? "Reading the server failed.");
      onOpenChange(false);
      onDone(server.id);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const fieldErr = (name: string) => fieldError(error, name);
  const generalError = error && !(error instanceof ApiError && error.fields && Object.keys(error.fields).some((k) => ["name", "address", "port", "username"].includes(k) || k.startsWith("credentials")));

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent title="Add server" description="Lanvex reads this server's network state over SSH. It never changes the server." width="max-w-[720px]">
        <div className="grid md:grid-cols-[168px_1fr]">
          <ol className="hidden border-r border-line px-3 py-4 md:block" aria-label="Steps">
            {STEPS.map((label, i) => (
              <li key={label} aria-current={i === step ? "step" : undefined} className={cn("flex items-center gap-2.5 rounded-[var(--radius-control)] px-2 py-1.5 text-[13px]", i === step ? "bg-accent-soft font-medium text-accent" : i < step ? "text-ink" : "text-faint")}>
                <span className={cn("grid size-5 shrink-0 place-items-center rounded-full border text-2xs", i < step ? "border-accent bg-accent text-accent-ink" : i === step ? "border-accent" : "border-line")}>
                  {i < step ? <Check className="size-3" /> : i + 1}
                </span>
                {label}
              </li>
            ))}
          </ol>
          <DialogBody className="min-h-[360px] content-start">
            {step === 0 && (
              <form
                id="add-server-0"
                className="grid gap-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (conn.address.trim() && portOk) void doScan();
                }}
              >
                <Field label="Name" htmlFor="sv-name" optional hint="Defaults to the address." error={fieldErr("name")}>
                  <Input id="sv-name" value={conn.name} onChange={(e) => setConn({ ...conn, name: e.target.value })} maxLength={100} placeholder="e.g. web-01" autoFocus />
                </Field>
                <div className="grid gap-4 sm:grid-cols-[1fr_7rem]">
                  <Field label="Address" htmlFor="sv-address" hint="IP address or DNS name." error={fieldErr("address")}>
                    <Input id="sv-address" className="addr" value={conn.address} onChange={(e) => setConn({ ...conn, address: e.target.value })} placeholder="192.0.2.10" spellCheck={false} autoComplete="off" required />
                  </Field>
                  <Field label="SSH port" htmlFor="sv-port" error={!portOk ? "1 to 65535" : fieldErr("port")}>
                    <Input id="sv-port" inputMode="numeric" className="addr" value={conn.port} onChange={(e) => setConn({ ...conn, port: e.target.value.replace(/\D/g, "") })} />
                  </Field>
                </div>
                <p className="text-xs text-muted">Next, Lanvex fetches the server's SSH host key so you can confirm it is the right machine. It does not sign in yet.</p>
              </form>
            )}
            {step === 1 && scan && <HostKeyVerification scan={scan} confirmed={confirmed} onConfirm={setConfirmed} />}
            {step === 2 && (
              <>
                <Field label="User on the server" htmlFor="sv-user" hint="An ordinary account is enough. Lanvex only reads; it never uses sudo." error={fieldErr("username")}>
                  <Input id="sv-user" className="addr" value={conn.username} onChange={(e) => setConn({ ...conn, username: e.target.value })} autoComplete="off" spellCheck={false} />
                </Field>
                <CredentialsForm
                  value={creds}
                  onChange={setCreds}
                  errors={{ private_key: fieldErr("private_key"), passphrase: fieldErr("passphrase"), password: fieldErr("password") }}
                />
              </>
            )}
            {step === 3 && server && (
              <>
                {server.authorized_keys_line && <InstallKeyInstructions username={server.username} line={server.authorized_keys_line} />}
                <div className="flex flex-wrap items-center gap-2">
                  <Button onClick={() => void doTest()} loading={busy === "test"}>
                    Test connection
                  </Button>
                  {test && (
                    <span role="status" className={cn("text-[13px]", test.status === "ok" ? "text-ok" : "text-danger")}>
                      {test.message}
                    </span>
                  )}
                </div>
              </>
            )}
            {generalError ? <Notice tone="danger">{errorMessage(error)}</Notice> : null}
          </DialogBody>
        </div>
        <DialogFooter>
          {step > 0 && step < 3 && (
            <Button variant="ghost" className="mr-auto" onClick={() => setStep(step - 1)} disabled={!!busy}>
              Back
            </Button>
          )}
          {step < 3 ? (
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={!!busy}>
              Cancel
            </Button>
          ) : (
            <Button variant="secondary" onClick={() => { onOpenChange(false); onDone(server!.id); }} disabled={!!busy}>
              Finish later
            </Button>
          )}
          {step === 0 && (
            <Button type="submit" form="add-server-0" variant="primary" loading={busy === "scan"} disabled={!conn.address.trim() || !portOk}>
              Fetch host key
            </Button>
          )}
          {step === 1 && (
            <Button variant="primary" onClick={() => setStep(2)} disabled={!confirmed}>
              Trust this key
            </Button>
          )}
          {step === 2 && (
            <Button variant="primary" onClick={() => void doCreate()} loading={busy === "create"} disabled={!credentialsComplete(creds) || !conn.username.trim()}>
              Add server
            </Button>
          )}
          {step === 3 && (
            <Button variant="primary" onClick={() => void doCollect()} loading={busy === "collect"} disabled={test?.status !== "ok"}>
              Read network state
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
