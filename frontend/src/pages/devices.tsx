import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Cpu, ExternalLink, Pencil } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router";
import { DataTable, Pagination, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { Facts, PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Select } from "@/components/ui/form";
import { Badge, EmptyState, Notice, Skeleton, StatusBadge } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { useLookup, useNetBoxWrite, useUrlState } from "@/lib/hooks";
import type { Device, DeviceDetail, Interface, Page, WriteResult } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { NotConfigured, isNotConfigured } from "./networks";

const LIMIT = 50;

export function DevicesPage() {
  const navigate = useNavigate();
  const { values, set, offset } = useUrlState(["q", "site", "role", "status"] as const);
  const sites = useLookup("sites");
  const roles = useLookup("device-roles");
  const query = { q: values.q, site_id: values.site, role_id: values.role, status: values.status, limit: LIMIT, offset };
  const list = useQuery({
    queryKey: ["devices", "list", query],
    queryFn: ({ signal }) => api<Page<Device>>("/devices", { query, signal }),
    placeholderData: keepPreviousData,
  });
  const filtered = !!(values.q || values.site || values.role || values.status);

  const columns: Column<Device>[] = [
    { key: "name", header: "Name", cell: (d) => <span className="font-medium">{d.name}</span> },
    { key: "status", header: "Status", cell: (d) => <StatusBadge value={d.status.value} label={d.status.label} /> },
    { key: "role", header: "Role", cell: (d) => d.role?.name ?? <span className="text-faint">—</span> },
    {
      key: "type",
      header: "Type",
      cell: (d) =>
        d.device_type ? (
          <span>
            <span className="text-muted">{d.device_type.manufacturer?.name} </span>
            {d.device_type.model}
          </span>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
    { key: "site", header: "Site", cell: (d) => d.site?.name ?? <span className="text-faint">—</span> },
    { key: "ip", header: "Management IP", cell: (d) => (d.primary_ip ? <span className="addr">{d.primary_ip}</span> : <span className="text-faint">—</span>) },
    { key: "ifaces", header: "Interfaces", cell: (d) => <span className="tabular-nums">{d.interface_count}</span> },
  ];

  return (
    <>
      <PageHeader title="Devices" description="Device inventory documented in NetBox. Live operational data is not shown here yet." />
      {isNotConfigured(list.error) ? (
        <NotConfigured />
      ) : (
        <>
          <Toolbar>
            <SearchInput label="Search devices" placeholder="Search name, serial, asset tag" value={values.q} onChange={(q) => set({ q })} />
            <Select aria-label="Site" className="w-auto" value={values.site} onChange={(e) => set({ site: e.target.value })}>
              <option value="">All sites</option>
              {sites.data?.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </Select>
            <Select aria-label="Role" className="w-auto" value={values.role} onChange={(e) => set({ role: e.target.value })}>
              <option value="">All roles</option>
              {roles.data?.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </Select>
            <Select aria-label="Status" className="w-auto" value={values.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="">Any status</option>
              {["active", "planned", "staged", "offline", "failed", "inventory", "decommissioning"].map((s) => (
                <option key={s} value={s} className="capitalize">
                  {s[0].toUpperCase() + s.slice(1)}
                </option>
              ))}
            </Select>
            {filtered && (
              <Button variant="ghost" size="sm" onClick={() => set({ q: null, site: null, role: null, status: null })}>
                Clear filters
              </Button>
            )}
          </Toolbar>
          <DataTable
            columns={columns}
            rows={list.data?.results}
            rowKey={(d) => d.id}
            loading={list.isPending}
            error={list.error}
            onRetry={() => void list.refetch()}
            onRowClick={(d) => navigate(`/devices/${d.id}`)}
            rowLabel={(d) => `Open ${d.name}`}
            empty={
              <EmptyState icon={Cpu} title={filtered ? "No devices match these filters" : "No devices in NetBox yet"}>
                {filtered ? "Try a broader search." : "Devices are added in NetBox (or imported later through discovery). They appear here automatically."}
              </EmptyState>
            }
            footer={list.data && <Pagination count={list.data.count} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o })} noun="devices" />}
          />
        </>
      )}
    </>
  );
}

export function DeviceDetailPage() {
  const { id } = useParams();
  const { isAdmin } = useSession();
  const [vlanEdit, setVlanEdit] = React.useState<Interface | null>(null);
  const q = useQuery({ queryKey: ["devices", "detail", Number(id)], queryFn: () => api<DeviceDetail>(`/devices/${id}`) });
  const d = q.data;

  const ifaceColumns: Column<Interface>[] = [
    {
      key: "name",
      header: "Interface",
      cell: (i) => (
        <span className="flex items-center gap-2">
          <span className="font-medium">{i.name}</span>
          {i.mgmt_only && <Badge>Management</Badge>}
          {!i.enabled && <Badge tone="warn">Disabled</Badge>}
        </span>
      ),
    },
    { key: "type", header: "Type", cell: (i) => i.type?.label ?? <span className="text-faint">—</span>, className: "text-muted" },
    {
      key: "mode",
      header: "802.1Q",
      cell: (i) =>
        i.mode ? (
          <span>
            {i.mode.label}
            {i.untagged_vlan && <span className="text-muted"> untagged {i.untagged_vlan.vid}</span>}
            {i.tagged_vlans.length > 0 && <span className="text-muted"> tagged {i.tagged_vlans.map((v) => v.vid).join(", ")}</span>}
          </span>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
    {
      key: "ips",
      header: "Addresses",
      cell: (i) => {
        const ips = d?.ip_addresses.filter((ip) => ip.assigned?.interface_id === i.id) ?? [];
        return ips.length ? (
          <span className="grid gap-0.5">
            {ips.map((ip) => (
              <span key={ip.id} className="addr">
                {ip.address}
              </span>
            ))}
          </span>
        ) : (
          <span className="text-faint">—</span>
        );
      },
    },
    { key: "mac", header: "MAC", cell: (i) => (i.mac_address ? <span className="addr text-muted">{i.mac_address}</span> : <span className="text-faint">—</span>) },
    { key: "cable", header: "Cabled", cell: (i) => (i.connected ? "Yes" : <span className="text-faint">No</span>) },
    {
      key: "act",
      header: <span className="sr-only">Actions</span>,
      headerClassName: "w-10",
      cell: (i) =>
        isAdmin ? (
          <Button size="icon-sm" variant="ghost" aria-label={`Edit VLANs on ${i.name}`} onClick={() => setVlanEdit(i)}>
            <Pencil />
          </Button>
        ) : null,
    },
  ];

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Devices", to: "/devices" }, { label: d?.name ?? "…" }]}
        title={d ? d.name : <Skeleton className="h-6 w-48" />}
        description={d && [d.device_type ? `${d.device_type.manufacturer?.name ?? ""} ${d.device_type.model}`.trim() : null, d.site?.name].filter(Boolean).join(" at ")}
        actions={
          d?.meta.netbox_url && (
            <Button asChild variant="secondary">
              <a href={d.meta.netbox_url} target="_blank" rel="noreferrer">
                <ExternalLink /> Open in NetBox
              </a>
            </Button>
          )
        }
      />
      {q.error && (
        <Notice tone="danger" title="Could not load this device">
          {errorMessage(q.error)}
        </Notice>
      )}
      {q.isPending && <Skeleton className="h-40 w-full" />}
      {d && (
        <div className="grid gap-8">
          <div className="grid gap-8 lg:grid-cols-[1fr_320px]">
            <Section title="Inventory" description="As documented in NetBox.">
              <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
                <Facts
                  items={[
                    ["Status", <StatusBadge key="s" value={d.status.value} label={d.status.label} />],
                    ["Role", d.role?.name],
                    ["Manufacturer", d.device_type?.manufacturer?.name],
                    ["Model", d.device_type?.model],
                    ["Platform", d.platform?.name],
                    ["Site", d.site?.name],
                    ["Location", d.location?.name],
                    ["Rack", d.rack?.name],
                    ["Tenant", d.tenant?.name],
                    ["Serial", d.serial ? <span className="addr">{d.serial}</span> : null],
                    ["Asset tag", d.asset_tag],
                    ["Management IP", d.primary_ip ? <span className="addr">{d.primary_ip}</span> : null],
                    ["Out-of-band IP", d.oob_ip ? <span className="addr">{d.oob_ip}</span> : null],
                    ["Last updated", <span key="u" title={absoluteTime(d.meta.last_updated)}>{relativeTime(d.meta.last_updated)}</span>],
                  ]}
                />
              </div>
            </Section>
            <Section title="VLANs" description="Carried by this device's interfaces.">
              <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
                {d.vlans.length ? (
                  <ul className="grid gap-1.5 text-[13px]">
                    {d.vlans.map((v) => (
                      <li key={v.id} className="flex justify-between gap-2">
                        <span>{v.name}</span>
                        <span className="addr text-muted">{v.vid}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[13px] text-muted">No VLANs are assigned to its interfaces.</p>
                )}
              </div>
            </Section>
          </div>
          <Section title="Interfaces" description={`${d.interfaces.length} documented, ${d.ip_addresses.length} IP address${d.ip_addresses.length === 1 ? "" : "es"} assigned.`}>
            <DataTable
              dense
              columns={ifaceColumns}
              rows={d.interfaces}
              rowKey={(i) => i.id}
              empty={<EmptyState icon={Cpu} title="No interfaces documented">Interfaces come from the device type in NetBox, or can be added there.</EmptyState>}
            />
          </Section>
          {d.comments && (
            <Section title="Comments">
              <p className="whitespace-pre-wrap rounded-[var(--radius-panel)] border border-line bg-surface p-4 text-[13px]">{d.comments}</p>
            </Section>
          )}
        </div>
      )}
      <InterfaceVlanDialog iface={vlanEdit} onClose={() => setVlanEdit(null)} />
    </>
  );
}

function InterfaceVlanDialog({ iface, onClose }: { iface: Interface | null; onClose: () => void }) {
  const vlans = useLookup("vlans", undefined, !!iface);
  const [mode, setMode] = React.useState("");
  const [untagged, setUntagged] = React.useState("");
  const [tagged, setTagged] = React.useState<string[]>([]);
  React.useEffect(() => {
    setMode(iface?.mode?.value ?? "");
    setUntagged(iface?.untagged_vlan ? String(iface.untagged_vlan.id) : "");
    setTagged(iface?.tagged_vlans.map((v) => String(v.id)) ?? []);
  }, [iface]);
  const save = useNetBoxWrite({
    fn: () =>
      api<WriteResult>(`/interfaces/${iface!.id}/vlans`, {
        method: "PUT",
        body: {
          mode: mode || null,
          untagged_vlan_id: untagged ? Number(untagged) : null,
          tagged_vlan_ids: mode === "tagged" ? tagged.map(Number) : [],
        },
      }),
    success: () => `Saved VLANs on ${iface?.name}`,
    invalidate: [["devices"]],
    onSuccess: onClose,
  });
  return (
    <Dialog open={!!iface} onOpenChange={(o) => !o && onClose()}>
      {iface && (
        <DialogContent title={`VLANs on ${iface.name}`} description="Documents the interface's 802.1Q mode in NetBox. The switch port is not changed.">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate(undefined);
            }}
          >
            <DialogBody>
              <Field label="Mode" htmlFor="if-mode">
                <Select id="if-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="">None</option>
                  <option value="access">Access (one untagged VLAN)</option>
                  <option value="tagged">Tagged (trunk with listed VLANs)</option>
                  <option value="tagged-all">Tagged, all VLANs</option>
                </Select>
              </Field>
              {mode && (
                <Field label={mode === "access" ? "VLAN" : "Untagged (native) VLAN"} htmlFor="if-untagged" optional={mode !== "access"}>
                  <Select id="if-untagged" value={untagged} onChange={(e) => setUntagged(e.target.value)}>
                    <option value="">None</option>
                    {vlans.data?.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
              {mode === "tagged" && (
                <Field label="Tagged VLANs" htmlFor="if-tagged" hint="Hold Ctrl or Cmd to select several.">
                  <select
                    id="if-tagged"
                    multiple
                    value={tagged}
                    onChange={(e) => setTagged(Array.from(e.target.selectedOptions, (o) => o.value))}
                    className="min-h-32 w-full rounded-[var(--radius-control)] border border-line bg-surface p-1 text-[13px]"
                  >
                    {vlans.data?.map((v) => (
                      <option key={v.id} value={v.id} className="rounded px-1.5 py-0.5">
                        {v.label}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              {save.error && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
            </DialogBody>
            <DialogFooter>
              <Button type="button" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" loading={save.isPending}>
                Save VLANs
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}
