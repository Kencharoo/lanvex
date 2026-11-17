import { useQuery, useQueryClient } from "@tanstack/react-query";
import { EthernetPort, Plus } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { EmptyState, Notice } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { ApiError, api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import { useNetBoxLabel, useSystemInfo } from "@/lib/system";
import type { SnmpCredentials, Switch, SwitchDetail, SwitchTestResult } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { DevicePicker, EMPTY_SNMP, SnmpForm, SwitchStatusBadge, snmpComplete, snmpPayload } from "./shared";

export function SwitchesPage() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const navigate = useNavigate();
  const [adding, setAdding] = React.useState(false);
  const info = useSystemInfo();
  const list = useQuery({ queryKey: ["switches"], queryFn: () => api<Switch[]>("/switches") });
  const canStore = info.data?.credential_storage !== false;

  const columns: Column<Switch>[] = [
    {
      key: "name",
      header: "Switch",
      cell: (s) => (
        <span className="grid">
          <span className="font-medium">{s.name}</span>
          <span className="addr text-xs text-muted">
            {s.address}
            {s.port !== 161 ? `:${s.port}` : ""}
          </span>
        </span>
      ),
    },
    { key: "status", header: "Status", cell: (s) => <SwitchStatusBadge status={s.last_status} /> },
    {
      key: "model",
      header: "Model",
      cell: (s) =>
        s.sys_description ? (
          <span className="line-clamp-1 max-w-[32ch] text-muted" title={s.sys_description}>
            {s.sys_description}
          </span>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
    {
      key: "ports",
      header: "Ports up",
      cell: (s) => (s.ports_total != null ? <span className="tabular-nums">{s.ports_up} / {s.ports_total}</span> : <span className="text-faint">—</span>),
    },
    { key: "macs", header: "MAC addresses", cell: (s) => (s.mac_count != null ? <span className="tabular-nums">{s.mac_count}</span> : <span className="text-faint">—</span>) },
    { key: "netbox", header: nbName, cell: (s) => (s.netbox ? <span className="text-muted">Linked</span> : <span className="text-faint">Not linked</span>) },
    {
      key: "read",
      header: "Last read",
      cell: (s) => (
        <span className="text-muted" title={s.last_read_at ?? undefined}>
          {s.last_read_at ? relativeTime(s.last_read_at) : "Never"}
        </span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Switches"
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setAdding(true)} disabled={!canStore}>
              <Plus /> Add switch
            </Button>
          )
        }
      />
      {!canStore && (
        <Notice tone="warn" title="Credential storage is not configured" className="mb-4">
          Set <strong>LANVEX_SECRET_KEY</strong> (<span className="addr">lanvex generate-secret-key</span>) and restart Lanvex.
        </Notice>
      )}
      <DataTable
        columns={columns}
        rows={list.data}
        rowKey={(s) => s.id}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        onRowClick={(s) => navigate(`/switches/${s.id}`)}
        rowLabel={(s) => `Open ${s.name}`}
        empty={
          <EmptyState
            icon={EthernetPort}
            title="No switches yet"
            action={
              isAdmin &&
              canStore && (
                <Button variant="primary" onClick={() => setAdding(true)}>
                  <Plus /> Add switch
                </Button>
              )
            }
          >
            Managed switches read over SNMP.
          </EmptyState>
        }
      />
      <AddSwitchDialog open={adding} onOpenChange={setAdding} onDone={(id) => navigate(`/switches/${id}`)} />
    </>
  );
}

function AddSwitchDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; onDone: (id: string) => void }) {
  const qc = useQueryClient();
  const [conn, setConn] = React.useState({ name: "", address: "", port: "161" });
  const [creds, setCreds] = React.useState<Required<SnmpCredentials>>(EMPTY_SNMP);
  const [device, setDevice] = React.useState<{ id: number; name: string | null } | null>(null);
  const [test, setTest] = React.useState<SwitchTestResult | null>(null);
  const [busy, setBusy] = React.useState<"test" | "add" | null>(null);
  const [error, setError] = React.useState<unknown>(null);

  React.useEffect(() => {
    if (open) {
      setConn({ name: "", address: "", port: "161" });
      setCreds(EMPTY_SNMP);
      setDevice(null);
      setTest(null);
      setError(null);
    }
  }, [open]);
  React.useEffect(() => setTest(null), [conn.address, conn.port, creds]);

  const port = Number(conn.port);
  const portOk = Number.isInteger(port) && port >= 1 && port <= 65535;
  const ready = !!conn.address.trim() && portOk && snmpComplete(creds);

  async function doTest() {
    setBusy("test");
    setError(null);
    try {
      setTest(await api<SwitchTestResult>("/switches/test", { method: "POST", body: { address: conn.address.trim(), port, credentials: snmpPayload(creds) } }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  async function doAdd() {
    setBusy("add");
    setError(null);
    try {
      const sw = await api<Switch>("/switches", {
        method: "POST",
        body: {
          name: conn.name.trim() || test?.system?.name || conn.address.trim(),
          address: conn.address.trim(),
          port,
          credentials: snmpPayload(creds),
          netbox_device_id: device?.id ?? null,
        },
      });
      toast.success(`Added ${sw.name}`);
      const read = await api<SwitchDetail>(`/switches/${sw.id}/read`, { method: "POST" });
      if (read.last_status !== "ok") toast.error(read.last_status_message ?? "Reading the switch failed.");
      void qc.invalidateQueries({ queryKey: ["switches"] });
      onOpenChange(false);
      onDone(sw.id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  const fieldErr = (name: string) => fieldError(error, name);
  const generalError = error && !(error instanceof ApiError && error.fields && Object.keys(error.fields).some((k) => ["name", "address", "port"].includes(k)));

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent title="Add switch" width="max-w-[640px]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (ready) void doAdd();
          }}
        >
          <DialogBody className="content-start">
            <div className="grid gap-4 sm:grid-cols-[1fr_1fr_6rem]">
              <Field label="Name" htmlFor="sw-name" optional error={fieldErr("name")}>
                <Input id="sw-name" value={conn.name} onChange={(e) => setConn({ ...conn, name: e.target.value })} maxLength={100} placeholder={test?.system?.name ?? "Default: its own name"} autoFocus />
              </Field>
              <Field label="Address" htmlFor="sw-address" error={fieldErr("address")}>
                <Input id="sw-address" className="addr" value={conn.address} onChange={(e) => setConn({ ...conn, address: e.target.value })} placeholder="192.0.2.2" spellCheck={false} autoComplete="off" required />
              </Field>
              <Field label="Port" htmlFor="sw-port" error={!portOk ? "1 to 65535" : fieldErr("port")}>
                <Input id="sw-port" inputMode="numeric" className="addr" value={conn.port} onChange={(e) => setConn({ ...conn, port: e.target.value.replace(/\D/g, "") })} />
              </Field>
            </div>
            <SnmpForm value={creds} onChange={setCreds} />
            <DevicePicker value={device} onChange={setDevice} />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" onClick={() => void doTest()} loading={busy === "test"} disabled={!ready || !!busy}>
                Test connection
              </Button>
              {test && (
                <span role="status" className={cn("text-[13px]", test.status === "ok" ? "text-ok" : "text-danger")}>
                  {test.message}
                  {test.system?.description && <span className="block text-xs text-muted">{test.system.description.split("\n")[0]}</span>}
                </span>
              )}
            </div>
            {generalError ? <Notice tone="danger">{errorMessage(error)}</Notice> : null}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={!!busy}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={busy === "add"} disabled={!ready || !!busy}>
              Add and read
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
