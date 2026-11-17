import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ExternalLink, MoreHorizontal, Plus, Unlink, Waypoints } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";
import { DataTable, Pagination, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Badge, EmptyState, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Notice, StatusBadge } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError, useLookup, useNetBoxWrite, useUrlState } from "@/lib/hooks";
import type { AvailableIP, Device, Interface, IPAddress, IPStatus, Page, Prefix, WriteResult } from "@/lib/types";
import { NotConfigured, isNotConfigured } from "./networks";
import { useNetBoxLabel } from "@/lib/system";

const LIMIT = 50;
const STATUSES: { v: IPStatus; label: string }[] = [
  { v: "active", label: "Active" },
  { v: "reserved", label: "Reserved" },
  { v: "dhcp", label: "DHCP" },
  { v: "slaac", label: "SLAAC" },
  { v: "deprecated", label: "Deprecated" },
];

type IPWrite = WriteResult & { ip_address: IPAddress };

export function IPAddressesPage() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const { values, set, offset } = useUrlState(["q", "parent", "status", "assigned", "vrf_id", "global_only", "family"] as const);
  const [creating, setCreating] = React.useState(false);
  const [assigning, setAssigning] = React.useState<IPAddress | null>(null);
  const [deleting, setDeleting] = React.useState<IPAddress | null>(null);

  const query = {
    q: values.q,
    parent: values.parent,
    status: values.status,
    assigned: values.assigned,
    vrf_id: values.vrf_id,
    global_only: values.global_only ? true : undefined,
    family: values.family,
    limit: LIMIT,
    offset,
  };
  const list = useQuery({
    queryKey: ["ip-addresses", "list", query],
    queryFn: ({ signal }) => api<Page<IPAddress>>("/ip-addresses", { query, signal }),
    placeholderData: keepPreviousData,
  });

  const update = useNetBoxWrite({
    fn: ({ id, status }: { id: number; status: IPStatus }) => api<IPWrite>(`/ip-addresses/${id}`, { method: "PATCH", body: { status } }),
    success: (r) => `${r.ip_address.address} is now ${r.ip_address.status.label.toLowerCase()}`,
    invalidate: [["ip-addresses"], ["prefixes"]],
  });
  const release = useNetBoxWrite({
    fn: (ip: IPAddress) => api<IPWrite>(`/ip-addresses/${ip.id}/release`, { method: "POST" }),
    success: (r) => `Released ${r.ip_address.address} from its interface`,
    invalidate: [["ip-addresses"], ["devices"]],
  });

  const filtered = !!(values.q || values.parent || values.status || values.assigned || values.family);

  const columns: Column<IPAddress>[] = [
    {
      key: "address",
      header: "Address",
      cell: (ip) => (
        <span className="flex items-center gap-2">
          <span className="addr font-medium">{ip.address}</span>
          {ip.is_gateway && <Badge tone="accent">Gateway</Badge>}
        </span>
      ),
    },
    { key: "status", header: "Status", cell: (ip) => <StatusBadge value={ip.status.value} label={ip.status.label} /> },
    {
      key: "assigned",
      header: "Assigned to",
      cell: (ip) =>
        ip.assigned ? (
          <span>
            {ip.assigned.device ? (
              <Link to={`/devices/${ip.assigned.device.id}`} className="hover:text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
                {ip.assigned.device.name}
              </Link>
            ) : (
              "Virtual machine"
            )}
            <span className="text-muted"> / {ip.assigned.interface_name}</span>
          </span>
        ) : (
          <span className="text-faint">Unassigned</span>
        ),
    },
    { key: "dns", header: "DNS name", cell: (ip) => ip.dns_name || <span className="text-faint">—</span>, className: "max-w-48 truncate" },
    { key: "desc", header: "Description", cell: (ip) => ip.description || <span className="text-faint">—</span>, className: "max-w-56 truncate text-muted" },
    { key: "vrf", header: "VRF", cell: (ip) => ip.vrf?.name ?? <span className="text-muted">Global</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      headerClassName: "w-10",
      cell: (ip) =>
        isAdmin ? (
          <Menu>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${ip.address}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              {STATUSES.filter((s) => s.v !== ip.status.value).map((s) => (
                <MenuItem key={s.v} onSelect={() => update.mutate({ id: ip.id, status: s.v })}>
                  Mark as {s.label.toLowerCase()}
                </MenuItem>
              ))}
              <MenuSeparator />
              <MenuItem onSelect={() => setAssigning(ip)}>{ip.assigned ? "Move to another interface" : "Assign to interface"}</MenuItem>
              {ip.assigned && (
                <MenuItem onSelect={() => release.mutate(ip)}>
                  <Unlink /> Release from interface
                </MenuItem>
              )}
              {ip.meta.netbox_url && (
                <MenuItem asChild>
                  <a href={ip.meta.netbox_url} target="_blank" rel="noreferrer">
                    <ExternalLink /> Open in {nbName}
                  </a>
                </MenuItem>
              )}
              <MenuSeparator />
              <MenuItem danger onSelect={() => setDeleting(ip)}>
                Delete record
              </MenuItem>
            </MenuContent>
          </Menu>
        ) : null,
    },
  ];

  return (
    <>
      <PageHeader
        title="IP Addresses"
        description={
          values.parent ? (
            <>
              Addresses inside <span className="addr text-ink">{values.parent}</span>.{" "}
              <button className="text-accent hover:underline" onClick={() => set({ parent: null, vrf_id: null, global_only: null })}>
                Show all
              </button>
            </>
          ) : (
            undefined
          )
        }
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> Add address
            </Button>
          )
        }
      />
      {isNotConfigured(list.error) ? (
        <NotConfigured />
      ) : (
        <>
          <Toolbar>
            <SearchInput label="Search addresses" placeholder="Search address, DNS name, description" value={values.q} onChange={(q) => set({ q })} />
            <Select aria-label="Status" className="w-auto" value={values.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="">Any status</option>
              {STATUSES.map((s) => (
                <option key={s.v} value={s.v}>
                  {s.label}
                </option>
              ))}
            </Select>
            <Select aria-label="Assignment" className="w-auto" value={values.assigned} onChange={(e) => set({ assigned: e.target.value })}>
              <option value="">Assigned or not</option>
              <option value="true">Assigned to an interface</option>
              <option value="false">Unassigned</option>
            </Select>
            <Select aria-label="Address family" className="w-auto" value={values.family} onChange={(e) => set({ family: e.target.value })}>
              <option value="">IPv4 and IPv6</option>
              <option value="4">IPv4</option>
              <option value="6">IPv6</option>
            </Select>
            {filtered && (
              <Button variant="ghost" size="sm" onClick={() => set({ q: null, parent: null, status: null, assigned: null, family: null, vrf_id: null, global_only: null })}>
                Clear filters
              </Button>
            )}
          </Toolbar>
          {(update.error || release.error) && (
            <Notice tone="danger" className="mb-3">
              {errorMessage(update.error ?? release.error)}
            </Notice>
          )}
          <DataTable
            dense
            columns={columns}
            rows={list.data?.results}
            rowKey={(ip) => ip.id}
            loading={list.isPending}
            error={list.error}
            onRetry={() => void list.refetch()}
            empty={
              <EmptyState icon={Waypoints} title={filtered ? "No addresses match these filters" : "No IP addresses recorded yet"}>
                {filtered ? "Try a broader search." : "Add an address, or reserve the next free one from a network's detail panel."}
              </EmptyState>
            }
            footer={list.data && <Pagination count={list.data.count} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o })} noun="addresses" />}
          />
        </>
      )}
      <CreateIPDialog open={creating} onOpenChange={setCreating} />
      <AssignDialog ip={assigning} onClose={() => setAssigning(null)} />
      <DeleteIPDialog ip={deleting} onClose={() => setDeleting(null)} />
    </>
  );
}

function CreateIPDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const nbName = useNetBoxLabel();
  const vrfs = useLookup("vrfs", undefined, open);
  const [mode, setMode] = React.useState<"next" | "manual">("next");
  const [prefixId, setPrefixId] = React.useState("");
  const [address, setAddress] = React.useState("");
  const [vrfId, setVrfId] = React.useState("");
  const [status, setStatus] = React.useState<IPStatus>("active");
  const [dns, setDns] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [device, setDevice] = React.useState<number | null>(null);
  const [iface, setIface] = React.useState<number | null>(null);

  React.useEffect(() => {
    if (open) {
      setMode("next");
      setPrefixId("");
      setAddress("");
      setVrfId("");
      setStatus("active");
      setDns("");
      setDescription("");
      setDevice(null);
      setIface(null);
    }
  }, [open]);

  const prefixes = useQuery({
    queryKey: ["prefixes", "pickable"],
    queryFn: () => api<Page<Prefix>>("/prefixes", { query: { status: ["active", "reserved"], limit: 500, include_utilization: false } }),
    enabled: open,
  });
  const preview = useQuery({
    queryKey: ["prefixes", "next-ips", Number(prefixId)],
    queryFn: () => api<AvailableIP[]>(`/prefixes/${prefixId}/available-ips`, { query: { limit: 1 } }),
    enabled: open && mode === "next" && !!prefixId,
  });

  const create = useNetBoxWrite({
    fn: () =>
      mode === "next"
        ? api<IPWrite>(`/prefixes/${prefixId}/allocate-ip`, {
            method: "POST",
            body: { status, dns_name: dns || null, description: description || null, interface_id: iface },
          })
        : api<IPWrite>("/ip-addresses", {
            method: "POST",
            body: {
              address: address.trim(),
              vrf_id: vrfId ? Number(vrfId) : null,
              status,
              dns_name: dns || null,
              description: description || null,
              interface_id: iface,
            },
          }),
    success: (r) => `Added ${r.ip_address.address}`,
    invalidate: [["ip-addresses"], ["prefixes"], ["devices"]],
    onSuccess: () => onOpenChange(false),
  });

  const err = create.error;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Add IP address" width="max-w-xl">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate(undefined);
          }}
        >
          <DialogBody>
            <div role="radiogroup" aria-label="How to choose the address" className="grid grid-cols-2 gap-1 rounded-[var(--radius-control)] bg-surface-2 p-1">
              {(["next", "manual"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={mode === m}
                  onClick={() => setMode(m)}
                  className={`rounded px-2 py-1 text-[13px] ${mode === m ? "bg-surface font-medium shadow-sm" : "text-muted hover:text-ink"}`}
                >
                  {m === "next" ? "Next free in a network" : "Specific address"}
                </button>
              ))}
            </div>
            {mode === "next" ? (
              <Field
                label="Network"
                htmlFor="ip-prefix"
                hint={preview.data?.[0] ? <>Next free: <span className="addr text-ink">{preview.data[0].address}</span>. {nbName} assigns it atomically.</> : preview.data ? "No free addresses in this network." : undefined}
              >
                <Select id="ip-prefix" value={prefixId} onChange={(e) => setPrefixId(e.target.value)} required>
                  <option value="">Choose a network</option>
                  {prefixes.data?.results.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.prefix}
                      {p.name ? ` ${p.name}` : ""}
                      {p.vrf ? ` [${p.vrf.name}]` : ""}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : (
              <div className="grid gap-4 sm:grid-cols-[1fr_12rem]">
                <Field label="Address" htmlFor="ip-addr" error={fieldError(err, "address")} hint="e.g. 10.20.0.15/24">
                  <Input id="ip-addr" className="addr" value={address} onChange={(e) => setAddress(e.target.value)} spellCheck={false} required aria-invalid={!!fieldError(err, "address")} />
                </Field>
                <Field label="VRF" htmlFor="ip-vrf">
                  <Select id="ip-vrf" value={vrfId} onChange={(e) => setVrfId(e.target.value)}>
                    <option value="">Global table</option>
                    {vrfs.data?.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Status" htmlFor="ip-status">
                <Select id="ip-status" value={status} onChange={(e) => setStatus(e.target.value as IPStatus)}>
                  {STATUSES.map((s) => (
                    <option key={s.v} value={s.v}>
                      {s.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="DNS name" htmlFor="ip-dns" optional error={fieldError(err, "dns_name")}>
                <Input id="ip-dns" value={dns} onChange={(e) => setDns(e.target.value)} placeholder="host.example.net" />
              </Field>
            </div>
            <Field label="Description" htmlFor="ip-desc" optional>
              <Input id="ip-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={200} />
            </Field>
            <InterfacePicker device={device} iface={iface} onDevice={setDevice} onIface={setIface} enabled={open} />
            {err && !fieldError(err, "address", "dns_name") && <Notice tone="danger">{errorMessage(err)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending} disabled={mode === "next" ? !prefixId || preview.data?.length === 0 : !address.trim()}>
              Add address
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function InterfacePicker({
  device,
  iface,
  onDevice,
  onIface,
  enabled,
  optional = true,
}: {
  device: number | null;
  iface: number | null;
  onDevice: (d: number | null) => void;
  onIface: (i: number | null) => void;
  enabled: boolean;
  optional?: boolean;
}) {
  const [search, setSearch] = React.useState("");
  const devices = useQuery({
    queryKey: ["devices", "picker", search],
    queryFn: () => api<Page<Device>>("/devices", { query: { q: search, limit: 50 } }),
    enabled,
  });
  const interfaces = useQuery({
    queryKey: ["devices", "interfaces", device],
    queryFn: () => api<Page<Interface>>("/interfaces", { query: { device_id: device!, limit: 500 } }),
    enabled: enabled && device !== null,
  });
  return (
    <fieldset className="grid gap-3 rounded-[var(--radius-control)] border border-line p-3">
      <legend className="px-1 text-[13px] font-medium">
        Interface {optional && <span className="font-normal text-faint">(optional)</span>}
      </legend>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Device" htmlFor="pick-device">
          <Input id="pick-device-search" aria-label="Filter devices" placeholder="Filter devices" value={search} onChange={(e) => setSearch(e.target.value)} className="mb-1.5" />
          <Select
            id="pick-device"
            value={device ?? ""}
            onChange={(e) => {
              onDevice(e.target.value ? Number(e.target.value) : null);
              onIface(null);
            }}
          >
            <option value="">{devices.data?.count === 0 ? "No devices found" : "No device"}</option>
            {devices.data?.results.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.site ? ` (${d.site.name})` : ""}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Interface" htmlFor="pick-iface">
          <Select id="pick-iface" value={iface ?? ""} onChange={(e) => onIface(e.target.value ? Number(e.target.value) : null)} disabled={device === null || interfaces.isPending} className="sm:mt-[38px]">
            <option value="">{device === null ? "Choose a device first" : interfaces.data?.count === 0 ? "Device has no interfaces" : "Choose an interface"}</option>
            {interfaces.data?.results.map((i) => (
              <option key={i.id} value={i.id}>
                {i.name}
                {i.mgmt_only ? " (management)" : ""}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </fieldset>
  );
}

function AssignDialog({ ip, onClose }: { ip: IPAddress | null; onClose: () => void }) {
  const [device, setDevice] = React.useState<number | null>(null);
  const [iface, setIface] = React.useState<number | null>(null);
  React.useEffect(() => {
    setDevice(ip?.assigned?.device?.id ?? null);
    setIface(null);
  }, [ip]);
  const assign = useNetBoxWrite({
    fn: () => api<IPWrite>(`/ip-addresses/${ip!.id}/assign`, { method: "POST", body: { interface_id: iface } }),
    success: (r) => `Assigned ${r.ip_address.address} to ${r.ip_address.assigned?.interface_name}`,
    invalidate: [["ip-addresses"], ["devices"]],
    onSuccess: onClose,
  });
  return (
    <Dialog open={!!ip} onOpenChange={(o) => !o && onClose()}>
      {ip && (
        <DialogContent title={`Assign ${ip.address}`} >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (iface) assign.mutate(undefined);
            }}
          >
            <DialogBody>
              <InterfacePicker device={device} iface={iface} onDevice={setDevice} onIface={setIface} enabled optional={false} />
              {assign.error && <Notice tone="danger">{errorMessage(assign.error)}</Notice>}
            </DialogBody>
            <DialogFooter>
              <Button type="button" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!iface} loading={assign.isPending}>
                Assign address
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}

function DeleteIPDialog({ ip, onClose }: { ip: IPAddress | null; onClose: () => void }) {
  const del = useNetBoxWrite({
    fn: () => api<WriteResult>(`/ip-addresses/${ip!.id}`, { method: "DELETE" }),
    success: () => `Deleted ${ip?.address}`,
    invalidate: [["ip-addresses"], ["prefixes"], ["devices"]],
    onSuccess: onClose,
  });
  return (
    <Dialog open={!!ip} onOpenChange={(o) => !o && onClose()}>
      {ip && (
        <DialogContent title={`Delete ${ip.address}?`} role="alertdialog">
          <DialogBody>
            {ip.assigned && (
              <Notice tone="warn" title="This address is assigned">
                It is recorded on {ip.assigned.device?.name ?? "a virtual machine"} / {ip.assigned.interface_name}. The device itself is not changed.
              </Notice>
            )}
            {ip.is_gateway && <Notice tone="warn">This address is marked as a network gateway.</Notice>}
            {del.error && <Notice tone="danger">{errorMessage(del.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="danger" onClick={() => del.mutate(undefined)} loading={del.isPending}>
              Delete record
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}
