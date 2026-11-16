import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Pin, Plus, Send, Trash2, X } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Badge, Notice, Skeleton, type Tone } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import { useNetBoxLabel } from "@/lib/system";
import type { ChangeDetail, DhcpStatus, HostFacts, NetworkLease, NetworksView, NetworkStatus, Server, ServerNetwork } from "@/lib/types";
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
  const update = (v: NetworksView) => qc.setQueryData(key, v);
  const prepare = useMutation({
    mutationFn: () => api<ChangeDetail>(`/servers/${s.id}/networks/change`, { method: "POST" }),
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
  const pending = v ? v.networks.filter((n) => ["missing", "differs", "removed"].includes(n.status) || ["missing", "differs", "removed"].includes(n.dhcp ?? "")).length : 0;
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
            <Button size="sm" variant="primary" disabled={!v.change} loading={prepare.isPending} onClick={() => prepare.mutate()}>
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
      {v && v.missing_fields.length > 0 && (
        <Notice tone="warn" title={`${nbName} lacks fields Lanvex needs for DHCP`}>
          Missing custom fields: {v.missing_fields.join(", ")}. Lanvex Core 0.7 has them; in your own NetBox, add them as text fields.
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
          <p className="text-xs text-muted">
            {v.read_at ? `Compared with the server's read ${relativeTime(v.read_at)}. ` : "Read the server to compare. "}
            {v.in_sync ? `${s.name} has the networks ${nbName} documents.` : `${pending} network${pending === 1 ? "" : "s"} to bring in line: prepare the change.`}{" "}
            Devices on these networks reach this server's DHCP and DNS; reaching other networks and the Internet comes with access rules (Lanvex 0.9).
          </p>
        </>
      )}
      <AddNetworkDialog server={s} facts={facts} open={adding} onClose={() => setAdding(false)} onSaved={update} />
      <PoolDialog server={s} network={pool} onClose={() => setPool(null)} onSaved={update} />
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
