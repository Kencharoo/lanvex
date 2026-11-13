import { useQuery } from "@tanstack/react-query";
import { History, Network, RefreshCw } from "lucide-react";
import { Link } from "react-router";
import { Bar, BarChart, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";
import { UtilStrip } from "@/components/address-space";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { EmptyState, Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import type { NetBoxStatus, Overview } from "@/lib/types";
import { formatCount, relativeTime } from "@/lib/utils";
import { NotConfigured, isNotConfigured } from "./networks";
import { useNetBoxLabel } from "@/lib/system";

export function OverviewPage() {
  const nbName = useNetBoxLabel();
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => api<Overview>("/dashboard/overview"), staleTime: 30_000 });
  const nb = useQuery({ queryKey: ["netbox-status"], queryFn: () => api<NetBoxStatus>("/system/netbox"), staleTime: 30_000 });
  const o = q.data;

  return (
    <>
      <PageHeader
        title="Overview"
        description={nb.data?.reachable ? `Documented state from ${nbName} ${nb.data.version} at ${nb.data.url}.` : `Documented state from ${nbName}.`}
        actions={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void api<Overview>("/dashboard/overview", { query: { refresh: true } }).then(() => q.refetch())}
            disabled={q.isFetching}
          >
            <RefreshCw className={q.isFetching ? "animate-spin" : ""} /> Refresh
          </Button>
        }
      />
      {isNotConfigured(q.error) ? (
        <NotConfigured />
      ) : q.error ? (
        <Notice tone="danger" title={`${nbName} data is unavailable`}>
          {errorMessage(q.error)}
        </Notice>
      ) : (
        <div className="grid gap-8">
          <Inventory o={o} />
          {o && o.counts.prefixes === 0 && o.counts.devices === 0 && o.counts.vlans === 0 ? (
            <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
              <EmptyState
                icon={Network}
                title={`${nbName} has no network data yet`}
                action={
                  <Button asChild variant="primary">
                    <Link to="/networks">Go to networks</Link>
                  </Button>
                }
              >
                Start by documenting a network. Devices and VLANs appear here as soon as they exist in {nbName}.
              </EmptyState>
            </div>
          ) : (
            <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.9fr)]">
              <Section
                title="Most utilized networks"
                description={o ? `Among ${o.utilization_sampled} active and reserved prefixes sampled.` : undefined}
                actions={
                  <Link to="/networks" className="text-xs text-accent hover:underline">
                    All networks
                  </Link>
                }
              >
                <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
                  {!o ? (
                    <div className="grid gap-3 p-4">
                      {Array.from({ length: 5 }).map((_, i) => (
                        <Skeleton key={i} className="h-5" />
                      ))}
                    </div>
                  ) : o.top_utilized.length === 0 ? (
                    <p className="p-4 text-[13px] text-muted">No active prefixes to measure yet.</p>
                  ) : (
                    <ul>
                      {o.top_utilized.map((p) => (
                        <li key={p.id} className="border-b border-line last:border-0">
                          <Link to={`/networks?prefix=${p.id}`} className="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-2/70">
                            <span className="min-w-0 flex-1">
                              <span className="addr block font-medium">{p.prefix}</span>
                              <span className="block truncate text-xs text-muted">
                                {p.name || "No name"}
                                {p.vrf ? `, VRF ${p.vrf}` : ""}
                              </span>
                            </span>
                            <UtilStrip u={{ percent: p.percent, used: p.used, total: p.total, basis: "ip_addresses" }} />
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </Section>
              <div className="grid content-start gap-8">
                <Section title="Addresses by status">
                  <div className="h-44 rounded-[var(--radius-panel)] border border-line bg-surface p-3">
                    {o ? (
                      <ResponsiveContainer>
                        <BarChart data={o.ip_status.map((s) => ({ ...s, label: s.status === "dhcp" ? "DHCP" : s.status === "slaac" ? "SLAAC" : s.status[0].toUpperCase() + s.status.slice(1) }))} layout="vertical" margin={{ left: 8, right: 24 }}>
                          <XAxis type="number" hide allowDecimals={false} />
                          <YAxis type="category" dataKey="label" width={82} tickLine={false} axisLine={false} tick={{ fontSize: 12, fill: "var(--muted)" }} />
                          <ChartTooltip cursor={{ fill: "var(--surface-2)" }} contentStyle={{ background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 6, fontSize: 12 }} />
                          <Bar dataKey="count" name="Addresses" fill="var(--accent)" radius={[0, 3, 3, 0]} barSize={14} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full" />
                    )}
                  </div>
                </Section>
                <RecentChanges o={o} />
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function Inventory({ o }: { o: Overview | undefined }) {
  const items: { label: string; value?: number; sub?: string; to: string }[] = [
    { label: "Prefixes", value: o?.counts.prefixes, sub: o && `${formatCount(o.counts.prefixes_v4)} IPv4, ${formatCount(o.counts.prefixes_v6)} IPv6`, to: "/networks" },
    { label: "IP addresses", value: o?.counts.ip_addresses, sub: o && `${formatCount(o.counts.ip_assigned)} assigned`, to: "/ip-addresses" },
    { label: "VLANs", value: o?.counts.vlans, to: "/vlans" },
    { label: "Devices", value: o?.counts.devices, sub: o && `${o.counts.sites} site${o.counts.sites === 1 ? "" : "s"}`, to: "/devices" },
    { label: "VRFs", value: o?.counts.vrfs, sub: "plus the global table", to: "/networks" },
  ];
  return (
    <div className="grid grid-cols-2 overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface sm:grid-cols-3 lg:grid-cols-5">
      {items.map((it) => (
        <Link key={it.label} to={it.to} className="border-b border-r border-line px-4 py-3.5 last:border-r-0 hover:bg-surface-2/60 lg:border-b-0">
          <span className="block text-xs text-muted">{it.label}</span>
          {it.value === undefined ? (
            <Skeleton className="mt-1.5 h-6 w-16" />
          ) : (
            <span className="mt-0.5 block text-2xl font-medium tabular-nums tracking-[-0.02em]">{formatCount(it.value)}</span>
          )}
          <span className="block h-4 text-xs text-faint">{it.sub}</span>
        </Link>
      ))}
    </div>
  );
}

const OBJECT_LABELS: Record<string, string> = {
  "ipam.prefix": "prefix",
  "ipam.ipaddress": "IP address",
  "ipam.iprange": "IP range",
  "ipam.vlan": "VLAN",
  "ipam.vlangroup": "VLAN group",
  "ipam.vrf": "VRF",
  "ipam.role": "prefix role",
  "dcim.device": "device",
  "dcim.devicetype": "device type",
  "dcim.devicerole": "device role",
  "dcim.interface": "interface",
  "dcim.site": "site",
  "dcim.manufacturer": "manufacturer",
  "extras.tag": "tag",
  "tenancy.tenant": "tenant",
};

function objectLabel(type: string): string {
  return OBJECT_LABELS[type] ?? type.split(".").pop() ?? type;
}

function RecentChanges({ o }: { o: Overview | undefined }) {
  const nbName = useNetBoxLabel();
  const source = o?.recent_changes[0]?.source;
  return (
    <Section
      title="Recent changes"
      description={source === "lanvex" ? "From the Lanvex audit log." : source === "netbox" ? `From the ${nbName} change log, including changes made outside Lanvex.` : undefined}
      actions={
        <Link to="/activity" className="text-xs text-accent hover:underline">
          Activity log
        </Link>
      }
    >
      <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
        {o && !o.recent_changes_available && o.recent_changes_message && (
          <p className="border-b border-line px-4 py-2 text-xs text-muted">{o.recent_changes_message}</p>
        )}
        {!o ? (
          <div className="grid gap-3 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-4" />
            ))}
          </div>
        ) : o.recent_changes.length === 0 ? (
          <EmptyState icon={History} title="No changes recorded yet" />
        ) : (
          <ol>
            {o.recent_changes.map((c, i) => (
              <li key={i} className="flex items-baseline gap-3 border-b border-line px-4 py-2 text-[13px] last:border-0">
                <span className="w-16 shrink-0 text-xs capitalize text-muted">{c.action}</span>
                <span className="min-w-0 flex-1 truncate">
                  <span className="addr">{c.object_repr}</span>
                  <span className="text-muted"> {objectLabel(c.object_type)}</span>
                </span>
                <span className="shrink-0 text-xs text-faint" title={new Date(c.time).toLocaleString()}>
                  {c.user ? `${c.user}, ` : ""}
                  {relativeTime(c.time)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </Section>
  );
}
