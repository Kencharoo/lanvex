import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cable, ExternalLink, MoreHorizontal, Pencil, RefreshCw, Trash2 } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Badge, EmptyState, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Notice, Skeleton } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import { useNetBoxLabel } from "@/lib/system";
import type { PortFinding, SnmpCredentials, Switch, SwitchDetail, SwitchFacts, SwitchPort } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { PlanDialog } from "../discovery";
import { DevicePicker, EMPTY_SNMP, EndpointLine, PortStatusBadge, SnmpForm, SwitchStatusBadge, snmpComplete, snmpLabel, snmpPayload, speed, uptime } from "./shared";

export function SwitchDetailPage() {
  const { id = "" } = useParams();
  const { isAdmin } = useSession();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [dialog, setDialog] = React.useState<null | "edit" | "delete">(null);
  const q = useQuery({ queryKey: ["switches", id], queryFn: () => api<SwitchDetail>(`/switches/${id}`) });
  const read = useMutation({
    mutationFn: () => api<SwitchDetail>(`/switches/${id}/read`, { method: "POST" }),
    onSuccess: (d) => {
      qc.setQueryData(["switches", id], d);
      void qc.invalidateQueries({ queryKey: ["switches"], exact: true });
      void qc.invalidateQueries({ queryKey: ["discovery"] });
      if (d.last_status === "ok") toast.success(`Read ${d.name}`);
      else toast.error(d.last_status_message ?? "Reading the switch failed.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const s = q.data;

  if (q.error) {
    return (
      <>
        <PageHeader crumbs={[{ label: "Switches", to: "/switches" }, { label: "Not found" }]} title="Switch" />
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      </>
    );
  }

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Switches", to: "/switches" }, { label: s?.name ?? "…" }]}
        title={s ? s.name : <Skeleton className="h-6 w-48" />}
        description={
          s && (
            <span className="flex flex-wrap items-center gap-2">
              <span className="addr">
                {s.address}
                {s.port !== 161 ? `:${s.port}` : ""}
              </span>
              <SwitchStatusBadge status={s.last_status} />
              {s.description && <span>{s.description}</span>}
            </span>
          )
        }
        actions={
          s &&
          isAdmin && (
            <>
              <Button variant="primary" onClick={() => read.mutate()} loading={read.isPending}>
                <RefreshCw /> Read now
              </Button>
              <Menu>
                <MenuTrigger asChild>
                  <Button size="icon" variant="secondary" aria-label="Switch actions">
                    <MoreHorizontal />
                  </Button>
                </MenuTrigger>
                <MenuContent>
                  <MenuItem onSelect={() => setDialog("edit")}>
                    <Pencil /> Edit
                  </MenuItem>
                  <MenuSeparator />
                  <MenuItem danger onSelect={() => setDialog("delete")}>
                    <Trash2 /> Remove switch
                  </MenuItem>
                </MenuContent>
              </Menu>
            </>
          )
        }
      />

      {s && s.last_status && s.last_status !== "ok" && (
        <Notice tone="danger" title="Last read failed" className="mb-6">
          {s.last_status_message} {s.last_checked_at && <span className="text-faint">({relativeTime(s.last_checked_at)})</span>}
        </Notice>
      )}

      {q.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : !s?.facts ? (
        <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
          <EmptyState
            icon={RefreshCw}
            title="Not read yet"
            action={isAdmin && <Button variant="primary" onClick={() => read.mutate()} loading={read.isPending}>Read now</Button>}
          >
            Read-only SNMP requests.
          </EmptyState>
        </div>
      ) : (
        <div className="grid gap-8">
          <p className="-mt-2 text-xs text-muted">
            {s.last_status !== "ok" ? "Last successful read" : "Read"} {relativeTime(s.facts.collected_at)} ({absoluteTime(s.facts.collected_at)})
          </p>
          <Summary sw={s} facts={s.facts} />
          {s.facts.notes.length > 0 && (
            <Notice tone="info">
              <ul className="list-disc pl-4">
                {s.facts.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </Notice>
          )}
          <PortsSection sw={s} facts={s.facts} />
          <VlansSection facts={s.facts} />
        </div>
      )}

      {s && <EditDialog sw={s} open={dialog === "edit"} onClose={() => setDialog(null)} />}
      {s && (
        <DeleteDialog
          sw={s}
          open={dialog === "delete"}
          onClose={() => setDialog(null)}
          onDeleted={() => {
            void qc.invalidateQueries({ queryKey: ["switches"] });
            navigate("/switches");
          }}
        />
      )}
    </>
  );
}

function Summary({ sw, facts }: { sw: SwitchDetail; facts: SwitchFacts }) {
  const nbName = useNetBoxLabel();
  const sys = facts.system;
  const items: { label: string; value: React.ReactNode; sub?: React.ReactNode }[] = [
    {
      label: "Model",
      value: <span className="line-clamp-2 text-[13px]" title={sys.description ?? undefined}>{sys.description?.split("\n")[0] ?? "—"}</span>,
      sub: sys.name && <span className="addr">{sys.name}</span>,
    },
    { label: "Up for", value: uptime(sys.uptime_seconds) ?? "—", sub: sys.location },
    {
      label: nbName,
      value: sw.netbox ? (
        sw.netbox.url ? (
          <a href={sw.netbox.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline">
            {sw.netbox.name ?? `Device ${sw.netbox.id}`} <ExternalLink className="size-3" />
          </a>
        ) : (
          (sw.netbox.name ?? `Device ${sw.netbox.id}`)
        )
      ) : (
        <span className="text-faint">Not linked</span>
      ),
      sub: !sw.netbox && "Link it to compare ports with cables",
    },
    { label: "Access", value: <span className="text-[13px]">{snmpLabel(sw.snmp)}</span>, sub: sw.read_interval ? `Read every ${sw.read_interval} min` : "Not read on a schedule" },
  ];
  return (
    <div className="grid grid-cols-1 overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface sm:grid-cols-2 xl:grid-cols-4">
      {items.map((it) => (
        <div key={it.label} className="border-b border-line px-4 py-3 last:border-b-0 sm:border-r xl:border-b-0 xl:last:border-r-0">
          <p className="text-xs text-muted">{it.label}</p>
          <div className="mt-0.5 text-[15px] font-medium">{it.value}</div>
          {it.sub && <p className="text-xs text-muted">{it.sub}</p>}
        </div>
      ))}
    </div>
  );
}

function vlanText(p: SwitchPort): React.ReactNode {
  if (!p.untagged.length && !p.tagged.length) return p.pvid ? `${p.pvid}` : null;
  const tagged = p.tagged.length > 6 ? `${p.tagged.length} tagged` : p.tagged.join(", ");
  return (
    <span title={p.tagged.length ? `Tagged: ${p.tagged.join(", ")}` : undefined}>
      {p.untagged.join(", ") || "—"}
      {p.tagged.length > 0 && <span className="text-muted"> + {tagged}</span>}
    </span>
  );
}

function PortsSection({ sw, facts }: { sw: SwitchDetail; facts: SwitchFacts }) {
  const { isAdmin } = useSession();
  const nbName = useNetBoxLabel();
  const [all, setAll] = React.useState(false);
  const [recordKey, setRecordKey] = React.useState<string | null>(null);
  const findings = new Map<number, PortFinding>(sw.findings.map((f) => [f.if_index, f]));
  const byIndex = new Map(facts.ports.map((p) => [p.index, p]));
  const rows = facts.ports.filter((p) => all || p.kind === "ethernet" || p.kind === "lag" || findings.has(p.index));
  const columns: Column<SwitchPort>[] = [
    {
      key: "port",
      header: "Port",
      cell: (p) => (
        <span className="grid">
          <span className="font-medium">{p.name}</span>
          <span className="text-xs text-muted">
            {[p.alias, p.kind === "lag" ? "Link aggregation" : null, p.lag ? `in ${byIndex.get(p.lag)?.name ?? p.lag}` : null, findings.get(p.index)?.uplink ? "uplink" : null]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (p) => (
        <span className="grid gap-0.5">
          <span>{p.admin_up === false ? <Badge>Disabled</Badge> : p.oper_up ? <Badge tone="ok">Up</Badge> : <Badge tone="neutral">Down</Badge>}</span>
          {p.oper_up && speed(p.speed_mbps) && <span className="text-xs text-muted">{speed(p.speed_mbps)}</span>}
        </span>
      ),
    },
    { key: "vlan", header: "VLANs", cell: (p) => vlanText(p) ?? <span className="text-faint">—</span> },
    {
      key: "seen",
      header: "Connected",
      cell: (p) => {
        const f = findings.get(p.index);
        if (!f || !f.endpoints.length) return <span className="text-faint">—</span>;
        return (
          <span className="grid gap-1">
            {f.endpoints.slice(0, 4).map((e, i) => (
              <EndpointLine key={`${e.via}-${e.mac ?? e.name}-${i}`} e={e} />
            ))}
            {f.endpoints.length > 4 && <span className="text-xs text-muted">and {f.endpoints.length - 4} more</span>}
          </span>
        );
      },
    },
    {
      key: "cable",
      header: `In ${nbName}`,
      cell: (p) => {
        const f = findings.get(p.index);
        if (!f) return null;
        return (
          <span className="grid max-w-sm gap-1">
            <span>
              <PortStatusBadge status={f.status} />
            </span>
            {f.documented && f.status !== "documented" && <span className="text-xs text-muted">Recorded: {f.documented}</span>}
            {f.problem && <span className="text-xs text-muted">{f.problem}</span>}
            {isAdmin && f.status === "undocumented" && !f.problem && (
              <span>
                <Button size="sm" variant="secondary" onClick={() => setRecordKey(f.key)}>
                  <Cable /> Record cable…
                </Button>
              </span>
            )}
          </span>
        );
      },
    },
  ];
  return (
    <Section
      title="Ports"
      actions={<Checkbox id="sw-all-ifaces" label="All interfaces" checked={all} onChange={(e) => setAll(e.target.checked)} />}
    >
      {sw.findings_error && <Notice tone="warn">{sw.findings_error}</Notice>}
      <DataTable dense columns={columns} rows={rows} rowKey={(p) => p.index} empty={<p className="p-4 text-[13px] text-muted">No ports.</p>} />
      <PlanDialog
        open={recordKey !== null}
        onClose={() => setRecordKey(null)}
        endpoint="/discovery/ports/record"
        items={recordKey ? [recordKey] : []}
        title={`Record a cable in ${nbName}`}
        description="Added as a connected cable."
        refresh={["switches", sw.id]}
      />
    </Section>
  );
}

function VlansSection({ facts }: { facts: SwitchFacts }) {
  if (!facts.vlans.length) return null;
  const count = (vid: number, key: "untagged" | "tagged") => facts.ports.filter((p) => p[key].includes(vid)).length;
  return (
    <Section title="VLANs">
      <DataTable
        dense
        columns={[
          { key: "id", header: "VLAN", cell: (v) => <span className="tabular-nums font-medium">{v.id}</span> },
          { key: "name", header: "Name", cell: (v) => v.name ?? <span className="text-faint">—</span> },
          { key: "untagged", header: "Untagged ports", cell: (v) => <span className="tabular-nums">{count(v.id, "untagged")}</span> },
          { key: "tagged", header: "Tagged ports", cell: (v) => <span className="tabular-nums">{count(v.id, "tagged")}</span> },
        ]}
        rows={facts.vlans}
        rowKey={(v) => v.id}
      />
    </Section>
  );
}

const INTERVALS: [string, string][] = [
  ["", "Default"],
  ["5", "Every 5 minutes"],
  ["15", "Every 15 minutes"],
  ["60", "Every hour"],
  ["1440", "Once a day"],
  ["0", "Off"],
];

function EditDialog({ sw, open, onClose }: { sw: Switch; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = React.useState({ name: "", description: "", address: "", port: "161", interval: "" });
  const [device, setDevice] = React.useState<{ id: number; name: string | null } | null>(null);
  const [replace, setReplace] = React.useState(false);
  const [creds, setCreds] = React.useState<Required<SnmpCredentials>>(EMPTY_SNMP);
  React.useEffect(() => {
    if (!open) return;
    setForm({
      name: sw.name,
      description: sw.description,
      address: sw.address,
      port: String(sw.port),
      interval: sw.read_interval_minutes == null ? "" : String(sw.read_interval_minutes),
    });
    setDevice(sw.netbox ? { id: sw.netbox.id, name: sw.netbox.name } : null);
    setReplace(false);
    setCreds({ ...EMPTY_SNMP, version: sw.snmp.version, username: sw.snmp.username ?? "", auth_protocol: sw.snmp.auth_protocol ?? "sha", priv_protocol: sw.snmp.priv_protocol ?? "aes" });
  }, [open, sw]);
  const save = useMutation({
    mutationFn: () =>
      api<Switch>(`/switches/${sw.id}`, {
        method: "PATCH",
        body: {
          name: form.name.trim(),
          description: form.description.trim(),
          address: form.address.trim(),
          port: Number(form.port),
          netbox_device_id: device?.id ?? null,
          read_interval_minutes: form.interval === "" ? null : Number(form.interval),
          ...(replace ? { credentials: snmpPayload(creds) } : {}),
        },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["switches"] });
      void qc.invalidateQueries({ queryKey: ["discovery"] });
      toast.success("Saved");
      onClose();
    },
  });
  const err = (f: string) => fieldError(save.error, f);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Edit ${sw.name}`} width="max-w-[640px]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <DialogBody>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Name" htmlFor="esw-name" error={err("name")}>
                <Input id="esw-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={100} />
              </Field>
              <Field label="Description" htmlFor="esw-desc" optional error={err("description")}>
                <Input id="esw-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} maxLength={200} />
              </Field>
            </div>
            <div className="grid gap-4 sm:grid-cols-[1fr_6rem_12rem]">
              <Field label="Address" htmlFor="esw-address" error={err("address")}>
                <Input id="esw-address" className="addr" value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} spellCheck={false} />
              </Field>
              <Field label="Port" htmlFor="esw-port" error={err("port")}>
                <Input id="esw-port" className="addr" inputMode="numeric" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value.replace(/\D/g, "") })} />
              </Field>
              <Field label="Scheduled reads" htmlFor="esw-interval">
                <Select id="esw-interval" value={form.interval} onChange={(e) => setForm({ ...form, interval: e.target.value })}>
                  {INTERVALS.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <DevicePicker value={device} onChange={setDevice} />
            <Checkbox id="esw-replace" label="Change SNMP credentials" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
            {replace && <SnmpForm value={creds} onChange={setCreds} />}
            {save.error && !err("name") && !err("address") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending} disabled={!form.name.trim() || !form.address.trim() || (replace && !snmpComplete(creds))}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteDialog({ sw, open, onClose, onDeleted }: { sw: Switch; open: boolean; onClose: () => void; onDeleted: () => void }) {
  const [confirm, setConfirm] = React.useState("");
  React.useEffect(() => setConfirm(""), [open]);
  const del = useMutation({
    mutationFn: () => api(`/switches/${sw.id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast.success(`Removed ${sw.name}`);
      onClose();
      onDeleted();
    },
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Remove ${sw.name}?`} description="Its credentials and what was read from it are deleted. The switch itself is not touched." role="alertdialog">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (confirm === sw.name) del.mutate();
          }}
        >
          <DialogBody>
            <Field label={`Type ${sw.name} to confirm`} htmlFor="del-sw">
              <Input id="del-sw" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" autoFocus />
            </Field>
            {del.error && <Notice tone="danger">{errorMessage(del.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" disabled={confirm !== sw.name} loading={del.isPending}>
              Remove switch
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
