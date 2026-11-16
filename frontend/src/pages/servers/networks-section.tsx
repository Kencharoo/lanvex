import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Pin, Plus, Send, Trash2, X } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Badge, Notice, Skeleton, type Tone } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import { useNetBoxLabel } from "@/lib/system";
import type { AccessSettings, ChangeDetail, ImportReport, DhcpStatus, HostFacts, NetworkLease, NetworksView, NetworkStatus, Server, ServerNetwork } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";

const STATUS: Record<NetworkStatus, { tone: Tone; label: string }> = {
  in_sync: { tone: "ok", label: "On the server" },
  missing: { tone: "warn", label: "Not on the server yet" },
  differs: { tone: "warn", label: "Differs on the server" },
  removed: { tone: "danger", label: "To be removed" },
  protected: { tone: "neutral", label: "Protected" },
  foreign: { tone: "neutral", label: "Another tool's" },
  incomplete: { tone: "neutral", label: "No address documented" },
  unknown: { tone: "neutral", label: "Not read yet" },
};

const DHCP: Record<DhcpStatus, { tone: Tone; label: string }> = {
  in_sync: { tone: "ok", label: "DHCP as documented" },
  missing: { tone: "warn", label: "DHCP not served yet" },
  differs: { tone: "warn", label: "DHCP differs" },
  removed: { tone: "danger", label: "DHCP to be removed" },
  none: { tone: "neutral", label: "No DHCP" },
};

/** The server's VLAN networks and their DHCP, as documented, next to what the server has. Documenting is
 * immediate; the server changes only through the prepared, approved change. */
export function NetworksSection({ server: s, facts, isAdmin }: { server: Server; facts: HostFacts | null; isAdmin: boolean }) {
  const nbName = useNetBoxLabel();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const key = ["servers", s.id, "networks"];
  const q = useQuery({ queryKey: key, queryFn: () => api<NetworksView>(`/servers/${s.id}/networks`) });
  const [adding, setAdding] = React.useState(false);
  const [pool, setPool] = React.useState<ServerNetwork | null>(null);
  const [fixed, setFixed] = React.useState<{ network: ServerNetwork; ip?: string; mac?: string; name?: string } | null>(null);
  const [open, setOpen] = React.useState<string | null>(null);
  const [access, setAccess] = React.useState<ServerNetwork | null>(null);
  const update = (v: NetworksView) => qc.setQueryData(key, v);
  const [report, setReport] = React.useState<ImportReport | null>(null);
  const importNv = useMutation({
    mutationFn: () => api<ImportReport>(`/servers/${s.id}/networks/import`, { method: "POST", body: { source: "netzverwaltung" } }),
    onSuccess: (r) => {
      update(r.view);
      setReport(r);
      if (!r.found) toast.info(`Netzverwaltung is not installed on ${s.name}.`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const prepare = useMutation({
    mutationFn: (takeover: boolean = false) =>
      api<ChangeDetail>(`/servers/${s.id}/networks/change${takeover ? "?takeover=netzverwaltung" : ""}`, { method: "POST" }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["servers", s.id] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      navigate(`/changes/${c.id}`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: (name: string) => api<NetworksView>(`/servers/${s.id}/networks/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: (v, name) => {
      update(v);
      toast.success(`${name} removed from ${nbName}. Prepare the change to remove it from ${s.name}.`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const unfix = useMutation({
    mutationFn: ({ network, ip }: { network: string; ip: string }) =>
      api<NetworksView>(`/servers/${s.id}/networks/${encodeURIComponent(network)}/fixed/${ip}`, { method: "DELETE" }),
    onSuccess: update,
    onError: (e) => toast.error(errorMessage(e)),
  });

  const v = q.data;
  const pending = v
    ? v.networks.filter((n) => ["missing", "differs", "removed"].includes(n.status) || ["missing", "differs", "removed"].includes(n.dhcp ?? "") || n.access_status === "differs").length
    : 0;
  return (
    <Section
      title="Networks"
      description={`VLAN interfaces on ${s.name} and the DHCP it serves on them, as ${nbName} documents them. Lanvex changes the server only through an approved change.`}
      actions={
        isAdmin &&
        v?.linked && (
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={() => setAdding(true)} disabled={v.supported === false}>
              <Plus /> Add network
            </Button>
            <Button size="sm" variant="ghost" loading={importNv.isPending} onClick={() => importNv.mutate()} title={`Document Netzverwaltung's networks on ${s.name} in ${nbName}`}>
              Import from Netzverwaltung
            </Button>
            <Button size="sm" variant="primary" disabled={!v.change} loading={prepare.isPending} onClick={() => prepare.mutate(false)}>
              <Send /> Prepare change
            </Button>
          </div>
        )
      }
    >
      {q.isLoading && <Skeleton className="h-24 w-full" />}
      {q.error && <Notice tone="danger">{errorMessage(q.error)}</Notice>}
      {v && !v.linked && (
        <Notice title={`Link ${s.name} to its device in ${nbName}`}>
          Networks are documented as the device's VLAN interfaces. Link the server under "Documented in {nbName} vs. on the server" first.
        </Notice>
      )}
      {v?.linked && v.supported === false && (
        <Notice tone="warn" title="Not on this server yet">
          Lanvex creates VLAN interfaces and serves DHCP on servers whose network NetworkManager, netplan or ifupdown manages (Fedora, RHEL, Ubuntu Server, Debian). {s.name} uses {v.stack_label}.
        </Notice>
      )}
      {report?.found && (
        <Notice tone="info" title={`Imported from Netzverwaltung into ${nbName}`}>
          <ul className="grid gap-1">
            {report.networks.map((n) => (
              <li key={n.name}>
                <span className="font-medium">{n.name}</span>: {n.result}
                {n.notes.length > 0 && <span className="text-muted"> ({n.notes.join(" ")})</span>}
              </li>
            ))}
          </ul>
          <p className="pt-2">Nothing on {s.name} changed. Check the networks below, then take them over in one change: Netzverwaltung's files are replaced by Lanvex's, and a rollback brings them back.</p>
        </Notice>
      )}
      {isAdmin && v?.networks.some((n) => n.status === "foreign") && (
        <Notice tone="warn" title="Networks another tool manages">
          <p>Lanvex leaves them alone. If they are Netzverwaltung's, take them over: one change replaces all of its networks with Lanvex's, stops Netzverwaltung, and is undone like any other. Protected names must be removed from the server's protected interfaces first.</p>
          <div className="pt-2">
            <Button size="sm" variant="secondary" loading={prepare.isPending} onClick={() => prepare.mutate(true)}>
              <Send /> Prepare takeover from Netzverwaltung
            </Button>
          </div>
        </Notice>
      )}
      {v && v.missing_fields.length > 0 && (
        <Notice tone="warn" title={`${nbName} lacks fields Lanvex needs for DHCP`}>
          Missing custom fields: {v.missing_fields.join(", ")}. Lanvex Core 0.9 has them; in your own NetBox, add them as described in docs/networks.md.
        </Notice>
      )}
      {v?.linked && (
        <>
          {v.networks.length === 0 ? (
            <div className="rounded-[var(--radius-panel)] border border-dashed border-line bg-surface px-4 py-6 text-center text-[13px] text-muted">
              No VLAN networks documented on {s.name}.{isAdmin && v.supported !== false && " Add one to create it on the server with an approved change."}
            </div>
          ) : (
            <div className="overflow-x-auto rounded-[var(--radius-panel)] border border-line bg-surface">
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="w-8 px-2 py-2" />
                    <th className="px-3 py-2 font-medium">Network</th>
                    <th className="px-3 py-2 font-medium">Server's address</th>
                    <th className="px-3 py-2 font-medium">DHCP pool</th>
                    <th className="px-3 py-2 font-medium">Devices</th>
                    <th className="px-3 py-2 font-medium">State</th>
                    {isAdmin && <th className="px-3 py-2" />}
                  </tr>
                </thead>
                <tbody>
                  {v.networks.map((n) => (
                    <NetworkRow
                      key={n.name}
                      n={n}
                      open={open === n.name}
                      onToggle={() => setOpen(open === n.name ? null : n.name)}
                      isAdmin={isAdmin}
                      onPool={() => setPool(n)}
                      onFix={(l) => setFixed({ network: n, ip: l?.ip, mac: l?.mac, name: l?.hostname ?? undefined })}
                      onUnfix={(ip) => unfix.mutate({ network: n.name, ip })}
                      onRemove={() => {
                        if (window.confirm(`Remove ${n.name} from ${nbName}? The server keeps it until you prepare and approve the change.`)) remove.mutate(n.name);
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {v.networks.some((n) => n.access !== null || n.access_problem) && (
            <AccessMatrix networks={v.networks} isAdmin={isAdmin} onEdit={setAccess} />
          )}
          <p className="text-xs text-muted">
            {v.read_at ? `Compared with the server's read ${relativeTime(v.read_at)}. ` : "Read the server to compare. "}
            {v.in_sync ? `${s.name} has the networks ${nbName} documents.` : `${pending} network${pending === 1 ? "" : "s"} to bring in line: prepare the change.`}
          </p>
        </>
      )}
      <AddNetworkDialog server={s} facts={facts} open={adding} onClose={() => setAdding(false)} onSaved={update} />
      <PoolDialog server={s} network={pool} onClose={() => setPool(null)} onSaved={update} />
      <AccessDialog server={s} network={access} networks={v?.networks ?? []} onClose={() => setAccess(null)} onSaved={update} />
      <FixedDialog server={s} seed={fixed} onClose={() => setFixed(null)} onSaved={update} />
    </Section>
  );
}

function NetworkRow({
  n,
  open,
  onToggle,
  isAdmin,
  onPool,
  onFix,
  onUnfix,
  onRemove,
}: {
  n: ServerNetwork;
  open: boolean;
  onToggle: () => void;
  isAdmin: boolean;
  onPool: () => void;
  onFix: (lease?: NetworkLease) => void;
  onUnfix: (ip: string) => void;
  onRemove: () => void;
}) {
  const st = STATUS[n.status];
  const managed = n.netbox_interface_id !== null && ["in_sync", "missing", "differs"].includes(n.status);
  return (
    <>
      <tr className="border-b border-line align-top last:border-0">
        <td className="px-2 py-2">
          <button type="button" aria-label={open ? `Hide ${n.name}` : `Show ${n.name}`} aria-expanded={open} onClick={onToggle} className="rounded p-0.5 text-muted hover:bg-surface-2">
            {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        </td>
        <td className="px-3 py-2">
          <span className="addr font-medium">{n.name}</span>
          <span className="block text-xs text-muted">
            VLAN {n.vlan_id ?? "?"}
            {n.vlan_name ? ` (${n.vlan_name})` : ""} on {n.parent ?? "?"}
          </span>
        </td>
        <td className="addr px-3 py-2">{n.addresses.join(", ") || <span className="text-faint">—</span>}</td>
        <td className="px-3 py-2">
          {n.pool ? (
            <>
              <span className="addr">
                {n.pool.start} to {n.pool.end}
              </span>
              <span className="block text-xs text-muted">lease {n.pool.lease}</span>
            </>
          ) : (
            <span className="text-faint">None</span>
          )}
        </td>
        <td className="px-3 py-2 text-xs text-muted">
          {n.fixed.length} fixed
          <br />
          {n.leases.length} with a lease
        </td>
        <td className="px-3 py-2">
          <div className="flex flex-wrap gap-1">
            <Badge tone={st.tone}>{st.label}</Badge>
            {n.dhcp && n.dhcp !== "none" && <Badge tone={DHCP[n.dhcp].tone}>{DHCP[n.dhcp].label}</Badge>}
          </div>
          {n.note && <p className="mt-1 max-w-xs text-xs text-muted">{n.note}</p>}
          {n.status === "differs" && n.on_server && (
            <p className="mt-1 text-xs text-muted">
              The server has VLAN {n.on_server.id} on {n.on_server.parent}, <span className="addr">{n.on_server.addresses.join(", ") || "no address"}</span>.
            </p>
          )}
        </td>
        {isAdmin && (
          <td className="px-3 py-2 text-right">
            {managed && (
              <div className="flex justify-end gap-1">
                <Button size="sm" variant="ghost" onClick={onPool}>
                  DHCP pool…
                </Button>
                <Button size="sm" variant="ghost" aria-label={`Remove ${n.name}`} onClick={onRemove}>
                  <Trash2 />
                </Button>
              </div>
            )}
          </td>
        )}
      </tr>
      {open && (
        <tr className="border-b border-line bg-surface-2/40 last:border-0">
          <td />
          <td colSpan={isAdmin ? 6 : 5} className="px-3 py-3">
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="grid content-start gap-1.5">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-medium">Fixed addresses</p>
                  {isAdmin && managed && n.pool && (
                    <Button size="sm" variant="ghost" onClick={() => onFix()}>
                      <Plus /> Add
                    </Button>
                  )}
                </div>
                {n.fixed.length === 0 ? (
                  <p className="text-xs text-muted">None. A device with a fixed address always gets the same one.</p>
                ) : (
                  <ul className="grid gap-1">
                    {n.fixed.map((f) => (
                      <li key={f.ip} className="flex items-center justify-between gap-2 text-xs">
                        <span>
                          <span className="addr">{f.ip}</span> · <span className="addr">{f.mac}</span>
                          {f.name ? ` · ${f.name}` : ""}
                        </span>
                        {isAdmin && managed && (
                          <button type="button" aria-label={`Remove the fixed address ${f.ip}`} onClick={() => onUnfix(f.ip)} className="rounded p-0.5 text-muted hover:text-danger">
                            <X className="size-3.5" />
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="grid content-start gap-1.5">
                <p className="text-xs font-medium">Leases on the server</p>
                {n.leases.length === 0 ? (
                  <p className="text-xs text-muted">No device has an address from this network right now (as of the last read).</p>
                ) : (
                  <ul className="grid gap-1">
                    {n.leases.map((l) => (
                      <li key={l.ip} className="flex items-center justify-between gap-2 text-xs">
                        <span>
                          <span className="addr">{l.ip}</span> · <span className="addr">{l.mac}</span>
                          {l.hostname ? ` · ${l.hostname}` : ""}
                          {l.expires_at ? <span className="text-muted"> · until {absoluteTime(l.expires_at)}</span> : ""}
                        </span>
                        {l.fixed ? (
                          <Badge tone="ok">Fixed</Badge>
                        ) : (
                          isAdmin &&
                          managed && (
                            <Button size="sm" variant="ghost" onClick={() => onFix(l)}>
                              <Pin /> Make fixed
                            </Button>
                          )
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function AddNetworkDialog({
  server: s,
  facts,
  open,
  onClose,
  onSaved,
}: {
  server: Server;
  facts: HostFacts | null;
  open: boolean;
  onClose: () => void;
  onSaved: (v: NetworksView) => void;
}) {
  const nbName = useNetBoxLabel();
  // Anything a VLAN can sit on: not loopback, not a VLAN or tunnel itself, not part of a bridge or bond.
  const parents = (facts?.interfaces ?? []).filter((i) => !["loopback", "vlan", "wireguard", "tun", "tap", "ipip", "gre", "sit"].includes(i.kind) && !i.master);
  const initial = () => ({
    vlan_id: "",
    name: "",
    vlan_name: "",
    parent: parents[0]?.name ?? "",
    address: "",
    dhcp: true,
    pool_start: "",
    pool_end: "",
    lease: "12h",
  });
  const [f, setF] = React.useState(initial);
  const [touched, setTouched] = React.useState<Record<string, boolean>>({});
  React.useEffect(() => {
    if (open) {
      setF(initial());
      setTouched({});
      save.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  // Suggestions follow the VLAN ID until the field is edited: vlan50, 192.168.50.1/24, pool .100 to .199.
  const setVid = (vid: string) => {
    const n = Number(vid);
    const ok = Number.isInteger(n) && n >= 1 && n <= 254;
    setF((d) => ({
      ...d,
      vlan_id: vid,
      name: touched.name ? d.name : vid ? `vlan${vid}` : "",
      address: touched.address ? d.address : ok ? `192.168.${n}.1/24` : d.address,
      pool_start: touched.pool ? d.pool_start : ok ? `192.168.${n}.100` : d.pool_start,
      pool_end: touched.pool ? d.pool_end : ok ? `192.168.${n}.199` : d.pool_end,
    }));
  };
  const save = useMutation({
    mutationFn: () =>
      api<NetworksView>(`/servers/${s.id}/networks`, {
        method: "POST",
        body: {
          name: f.name.trim(),
          parent: f.parent,
          vlan_id: Number(f.vlan_id),
          vlan_name: f.vlan_name.trim() || null,
          address: f.address.trim(),
          pool_start: f.dhcp ? f.pool_start.trim() : null,
          pool_end: f.dhcp ? f.pool_end.trim() : null,
          lease: f.lease.trim() || "12h",
        },
      }),
    onSuccess: (v) => {
      onSaved(v);
      toast.success(`${f.name} documented in ${nbName}. Prepare the change to create it on ${s.name}.`);
      onClose();
    },
  });
  const err = (field: string) => fieldError(save.error, field);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Add a network to ${s.name}`} description={`Lanvex documents it in ${nbName} first. The server gets it with the next change you prepare and approve.`}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <DialogBody>
            <div className="grid grid-cols-2 gap-3">
              <Field label="VLAN ID" htmlFor="nw-vid" error={err("vlan_id")}>
                <Input id="nw-vid" inputMode="numeric" value={f.vlan_id} onChange={(e) => setVid(e.target.value.replace(/\D/g, ""))} placeholder="50" required autoFocus />
              </Field>
              <Field label="VLAN name" htmlFor="nw-vname" optional>
                <Input id="nw-vname" value={f.vlan_name} onChange={(e) => setF({ ...f, vlan_name: e.target.value })} placeholder="Klasse 5" />
              </Field>
              <Field label="Interface name" htmlFor="nw-name" error={err("name")} hint="On the server, at most 15 characters.">
                <Input
                  id="nw-name"
                  className="addr"
                  value={f.name}
                  onChange={(e) => {
                    setTouched((t) => ({ ...t, name: true }));
                    setF({ ...f, name: e.target.value });
                  }}
                  maxLength={15}
                  required
                />
              </Field>
              <Field label="On" htmlFor="nw-parent" error={err("parent")} hint="The trunk to the switch.">
                <Select id="nw-parent" value={f.parent} onChange={(e) => setF({ ...f, parent: e.target.value })} required>
                  {parents.map((i) => (
                    <option key={i.name} value={i.name}>
                      {i.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Field label="The server's address in the network" htmlFor="nw-addr" error={err("address")} hint="With prefix length. Devices use it as their gateway and DNS server.">
              <Input
                id="nw-addr"
                className="addr"
                value={f.address}
                onChange={(e) => {
                  setTouched((t) => ({ ...t, address: true }));
                  setF({ ...f, address: e.target.value });
                }}
                placeholder="192.168.50.1/24"
                required
              />
            </Field>
            <Checkbox id="nw-dhcp" label="Hand out addresses by DHCP" checked={f.dhcp} onChange={(e) => setF({ ...f, dhcp: e.target.checked })} />
            {f.dhcp && (
              <div className="grid grid-cols-[1fr_1fr_6rem] gap-3">
                <Field label="Pool from" htmlFor="nw-start" error={err("pool_start")}>
                  <Input
                    id="nw-start"
                    className="addr"
                    value={f.pool_start}
                    onChange={(e) => {
                      setTouched((t) => ({ ...t, pool: true }));
                      setF({ ...f, pool_start: e.target.value });
                    }}
                    required
                  />
                </Field>
                <Field label="to" htmlFor="nw-end" error={err("pool_end")}>
                  <Input
                    id="nw-end"
                    className="addr"
                    value={f.pool_end}
                    onChange={(e) => {
                      setTouched((t) => ({ ...t, pool: true }));
                      setF({ ...f, pool_end: e.target.value });
                    }}
                    required
                  />
                </Field>
                <Field label="Lease" htmlFor="nw-lease" error={err("lease")}>
                  <Input id="nw-lease" className="addr" value={f.lease} onChange={(e) => setF({ ...f, lease: e.target.value })} />
                </Field>
              </div>
            )}
            {save.error && !["vlan_id", "name", "parent", "address", "pool_start", "pool_end", "lease"].some((k) => err(k)) && (
              <Notice tone="danger">{errorMessage(save.error)}</Notice>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending} disabled={parents.length === 0}>
              Add to {nbName}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PoolDialog({ server: s, network: n, onClose, onSaved }: { server: Server; network: ServerNetwork | null; onClose: () => void; onSaved: (v: NetworksView) => void }) {
  const [f, setF] = React.useState({ start: "", end: "", lease: "12h" });
  React.useEffect(() => {
    if (n) {
      const base = n.network ? n.network.split("/")[0].split(".").slice(0, 3).join(".") : "";
      setF({ start: n.pool?.start ?? (base ? `${base}.100` : ""), end: n.pool?.end ?? (base ? `${base}.199` : ""), lease: n.pool?.lease ?? "12h" });
      save.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [n]);
  const save = useMutation({
    mutationFn: (remove: boolean) =>
      api<NetworksView>(`/servers/${s.id}/networks/${encodeURIComponent(n?.name ?? "")}/pool`, {
        method: "PUT",
        body: remove ? { start: null, end: null, lease: "12h" } : { start: f.start.trim(), end: f.end.trim(), lease: f.lease.trim() || "12h" },
      }),
    onSuccess: (v) => {
      onSaved(v);
      onClose();
    },
  });
  return (
    <Dialog open={n !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`DHCP pool on ${n?.name ?? ""}`} description={`Addresses devices on ${n?.network ?? "this network"} get. The server serves it after the next approved change.`}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(false);
          }}
        >
          <DialogBody>
            <div className="grid grid-cols-[1fr_1fr_6rem] gap-3">
              <Field label="From" htmlFor="pl-start" error={fieldError(save.error, "start")}>
                <Input id="pl-start" className="addr" value={f.start} onChange={(e) => setF({ ...f, start: e.target.value })} required />
              </Field>
              <Field label="to" htmlFor="pl-end">
                <Input id="pl-end" className="addr" value={f.end} onChange={(e) => setF({ ...f, end: e.target.value })} required />
              </Field>
              <Field label="Lease" htmlFor="pl-lease">
                <Input id="pl-lease" className="addr" value={f.lease} onChange={(e) => setF({ ...f, lease: e.target.value })} />
              </Field>
            </div>
            {save.error && !fieldError(save.error, "start") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            {n?.pool && (
              <Button type="button" variant="ghost" className="mr-auto" onClick={() => save.mutate(true)}>
                <Trash2 /> No DHCP
              </Button>
            )}
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function FixedDialog({
  server: s,
  seed,
  onClose,
  onSaved,
}: {
  server: Server;
  seed: { network: ServerNetwork; ip?: string; mac?: string; name?: string } | null;
  onClose: () => void;
  onSaved: (v: NetworksView) => void;
}) {
  const [f, setF] = React.useState({ ip: "", mac: "", name: "" });
  React.useEffect(() => {
    if (seed) {
      setF({ ip: seed.ip ?? "", mac: seed.mac ?? "", name: seed.name ?? "" });
      save.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed]);
  const save = useMutation({
    mutationFn: () =>
      api<NetworksView>(`/servers/${s.id}/networks/${encodeURIComponent(seed?.network.name ?? "")}/fixed`, {
        method: "PUT",
        body: { ip: f.ip.trim(), mac: f.mac.trim(), name: f.name.trim() || null },
      }),
    onSuccess: (v) => {
      onSaved(v);
      onClose();
    },
  });
  return (
    <Dialog open={seed !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Fixed address" description={`The device with this MAC address always gets this address on ${seed?.network.name ?? ""}.`}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <DialogBody>
            <Field label="Address" htmlFor="fx-ip" error={fieldError(save.error, "ip")}>
              <Input id="fx-ip" className="addr" value={f.ip} onChange={(e) => setF({ ...f, ip: e.target.value })} placeholder={seed?.network.network?.replace(/0\/\d+$/, "20") ?? ""} required autoFocus />
            </Field>
            <Field label="MAC address" htmlFor="fx-mac" error={fieldError(save.error, "mac")}>
              <Input id="fx-mac" className="addr" value={f.mac} onChange={(e) => setF({ ...f, mac: e.target.value })} placeholder="aa:bb:cc:dd:ee:ff" required />
            </Field>
            <Field label="Name" htmlFor="fx-name" optional error={fieldError(save.error, "name")} hint="The device's host name, like drucker-1.">
              <Input id="fx-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
            </Field>
            {save.error && !fieldError(save.error, "ip", "mac", "name") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const SERVICES: { port: string; label: string }[] = [
  { port: "22/tcp", label: "SSH" },
  { port: "80/tcp", label: "Web (HTTP)" },
  { port: "443/tcp", label: "Web (HTTPS)" },
  { port: "123/udp", label: "Time (NTP)" },
];
const serviceLabel = (p: string) => SERVICES.find((x) => x.port === p)?.label ?? p;

/** One row per network: what its devices may reach, like Netzverwaltung's matrix. */
function AccessMatrix({ networks, isAdmin, onEdit }: { networks: ServerNetwork[]; isAdmin: boolean; onEdit: (n: ServerNetwork) => void }) {
  const rows = networks.filter((n) => n.netbox_interface_id !== null && n.network);
  const yes = <span className="font-medium text-[var(--color-ok)]">Yes</span>;
  const no = <span className="text-faint">No</span>;
  return (
    <div className="grid gap-2">
      <h3 className="text-[13px] font-semibold">What devices on each network may reach</h3>
      <div className="overflow-x-auto rounded-[var(--radius-panel)] border border-line bg-surface">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-3 py-2 font-medium">From</th>
              <th className="px-3 py-2 font-medium">Internet</th>
              {rows.map((t) => (
                <th key={t.name} className="px-3 py-2 font-medium">
                  {t.name}
                </th>
              ))}
              <th className="px-3 py-2 font-medium">Elsewhere</th>
              <th className="px-3 py-2 font-medium">On the server</th>
              <th className="px-3 py-2 font-medium">DNS filter</th>
              <th className="px-3 py-2 font-medium">State</th>
              {isAdmin && <th className="px-3 py-2" />}
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => {
              const a = n.access;
              const others = new Set(rows.map((t) => t.network));
              return (
                <tr key={n.name} className="border-b border-line last:border-0 align-top">
                  <td className="px-3 py-2 font-medium">{n.name}</td>
                  <td className="px-3 py-2">{a ? (a.internet ? yes : no) : "?"}</td>
                  {rows.map((t) => {
                    if (t.name === n.name) return <td key={t.name} className="px-3 py-2 text-faint">–</td>;
                    const d = a?.destinations.find((x) => x.network === t.network);
                    return (
                      <td key={t.name} className="px-3 py-2">
                        {!a ? "?" : d ? d.ports.length ? <span className="addr">{d.ports.join(", ")}</span> : yes : no}
                      </td>
                    );
                  })}
                  <td className="px-3 py-2">
                    {a && a.destinations.filter((d) => !others.has(d.network)).length > 0 ? (
                      <ul className="grid gap-0.5">
                        {a.destinations
                          .filter((d) => !others.has(d.network))
                          .map((d) => (
                            <li key={d.network} className="addr">
                              {d.network.replace(/\/32$/, "")}
                              {d.ports.length > 0 && <span className="text-muted"> {d.ports.join(", ")}</span>}
                            </li>
                          ))}
                      </ul>
                    ) : (
                      <span className="text-faint">{a ? "Nothing" : "?"}</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {a ? ["DHCP", "DNS", ...a.services.map(serviceLabel)].join(", ") : "?"}
                  </td>
                  <td className="px-3 py-2">
                    {!a ? "?" : a.dns_filter ? `${a.dns_filter.block.length} blocked${a.dns_filter.allow.length ? `, ${a.dns_filter.allow.length} exceptions` : ""}` : no}
                  </td>
                  <td className="px-3 py-2">
                    {n.access_status === "invalid" ? (
                      <Badge tone="danger" title={n.access_problem ?? undefined}>
                        Cannot be used
                      </Badge>
                    ) : n.access_status === "differs" ? (
                      <Badge tone="warn">Differs on the server</Badge>
                    ) : n.access_status === "in_sync" ? (
                      <Badge tone="ok">On the server</Badge>
                    ) : (
                      <span className="text-faint">With the network</span>
                    )}
                  </td>
                  {isAdmin && (
                    <td className="px-3 py-2 text-right">
                      <Button size="sm" variant="ghost" onClick={() => onEdit(n)}>
                        Edit
                      </Button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.filter((n) => n.access_problem).map((n) => (
        <Notice key={n.name} tone="danger">
          {n.access_problem} The server keeps its current rules for {n.name}; edit them here to replace what is documented.
        </Notice>
      ))}
    </div>
  );
}

type DestinationRow = { network: string; ports: string };

function AccessDialog({
  server: s,
  network: n,
  networks,
  onClose,
  onSaved,
}: {
  server: Server;
  network: ServerNetwork | null;
  networks: ServerNetwork[];
  onClose: () => void;
  onSaved: (v: NetworksView) => void;
}) {
  const nbName = useNetBoxLabel();
  const others = networks.filter((t) => n && t.name !== n.name && t.network && t.netbox_interface_id !== null);
  const [internet, setInternet] = React.useState(false);
  const [lanvex, setLanvex] = React.useState<string[]>([]);
  const [rows, setRows] = React.useState<DestinationRow[]>([]);
  const [services, setServices] = React.useState<string[]>([]);
  const [extra, setExtra] = React.useState("");
  const [filtering, setFiltering] = React.useState(false);
  const [blocked, setBlocked] = React.useState("");
  const [allowed, setAllowed] = React.useState("");
  React.useEffect(() => {
    if (!n) return;
    const a: AccessSettings = n.access ?? n.access_on_server ?? { internet: false, destinations: [], services: [] };
    const otherNets = new Set(others.map((t) => t.network));
    setInternet(a.internet);
    setLanvex(a.destinations.filter((d) => otherNets.has(d.network) && d.ports.length === 0).map((d) => d.network));
    setRows(
      a.destinations
        .filter((d) => !(otherNets.has(d.network) && d.ports.length === 0))
        .map((d) => ({ network: d.network.replace(/\/32$/, ""), ports: d.ports.join(", ") })),
    );
    setServices(a.services.filter((p) => SERVICES.some((x) => x.port === p)));
    setExtra(a.services.filter((p) => !SERVICES.some((x) => x.port === p)).join(", "));
    setFiltering(!!a.dns_filter);
    setBlocked((a.dns_filter?.block ?? []).join("\n"));
    setAllowed((a.dns_filter?.allow ?? []).join("\n"));
    save.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [n]);
  const split = (text: string) => text.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const save = useMutation({
    mutationFn: () =>
      api<NetworksView>(`/servers/${s.id}/networks/${encodeURIComponent(n?.name ?? "")}/access`, {
        method: "PUT",
        body: {
          internet,
          destinations: [
            ...lanvex.map((network) => ({ network, ports: [] })),
            ...rows.filter((r) => r.network.trim()).map((r) => ({ network: r.network.trim(), ports: split(r.ports) })),
          ],
          services: [...services, ...split(extra)],
          dns_filter: filtering ? { block: split(blocked), allow: split(allowed) } : null,
        },
      }),
    onSuccess: (v) => {
      onSaved(v);
      onClose();
    },
  });
  const toggle = (list: string[], value: string, on: boolean) => (on ? [...list, value] : list.filter((x) => x !== value));
  return (
    <Dialog open={n !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`What devices on ${n?.name ?? ""} may reach`}
        description={`Documented in ${nbName} at once; ${s.name} gets it with the next change you prepare and approve. Answers to allowed connections always come back.`}
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <DialogBody>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-xs font-medium text-muted">Through the server</legend>
              <Checkbox id="ac-internet" label="The Internet (private networks stay closed unless allowed below)" checked={internet} onChange={(e) => setInternet(e.target.checked)} />
              {others.map((t) => (
                <Checkbox
                  key={t.name}
                  id={`ac-${t.name}`}
                  label={`${t.name} (${t.network})`}
                  checked={lanvex.includes(t.network ?? "")}
                  onChange={(e) => setLanvex(toggle(lanvex, t.network ?? "", e.target.checked))}
                />
              ))}
            </fieldset>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-xs font-medium text-muted">Other networks and addresses (the main LAN, a switch's web interface)</legend>
              {rows.map((r, i) => (
                <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                  <Input aria-label="Network or address" className="addr" placeholder="192.168.178.3 or 192.168.178.0/24" value={r.network} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, network: e.target.value } : x)))} />
                  <Input aria-label="Ports" className="addr" placeholder="All ports, or 443/tcp, 80/tcp" value={r.ports} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, ports: e.target.value } : x)))} />
                  <Button type="button" variant="ghost" aria-label="Remove" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                    <X />
                  </Button>
                </div>
              ))}
              <div>
                <Button type="button" size="sm" variant="secondary" onClick={() => setRows([...rows, { network: "", ports: "" }])}>
                  <Plus /> Add destination
                </Button>
              </div>
            </fieldset>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-xs font-medium text-muted">On {s.name} itself (DHCP and DNS always)</legend>
              <div className="grid grid-cols-2 gap-2">
                {SERVICES.map((x) => (
                  <Checkbox key={x.port} id={`ac-${x.port}`} label={`${x.label} (${x.port})`} checked={services.includes(x.port)} onChange={(e) => setServices(toggle(services, x.port, e.target.checked))} />
                ))}
              </div>
              <Field label="Other ports" htmlFor="ac-extra" error={fieldError(save.error, "services")}>
                <Input id="ac-extra" className="addr" placeholder="8100/tcp, 5000-5010/udp" value={extra} onChange={(e) => setExtra(e.target.value)} />
              </Field>
            </fieldset>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-xs font-medium text-muted">DNS filter</legend>
              <Checkbox
                id="ac-filter"
                label={n?.pool ? "Block names for devices on this network" : "Block names for devices on this network (needs a DHCP pool here)"}
                checked={filtering}
                disabled={!n?.pool && !filtering}
                onChange={(e) => setFiltering(e.target.checked)}
              />
              {filtering && (
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Blocked, with every name below" htmlFor="ac-block" hint="One domain per line">
                    <Textarea id="ac-block" className="addr min-h-28" placeholder={"tiktok.com\ninstagram.com"} value={blocked} onChange={(e) => setBlocked(e.target.value)} />
                  </Field>
                  <Field label="Exceptions" htmlFor="ac-allow" hint="Answered despite a block above">
                    <Textarea id="ac-allow" className="addr min-h-28" placeholder="edu.tiktok.com" value={allowed} onChange={(e) => setAllowed(e.target.value)} />
                  </Field>
                </div>
              )}
              {filtering && <p className="text-xs text-muted">Devices notice a block once their own DNS cache expires; switching Wi-Fi off and on again helps.</p>}
            </fieldset>
            {save.error && !fieldError(save.error, "services") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
