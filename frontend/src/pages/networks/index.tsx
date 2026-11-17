import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Network, Plus, Settings } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";
import { UtilStrip } from "@/components/address-space";
import { DataTable, Pagination, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Select } from "@/components/ui/form";
import { EmptyState, StatusBadge } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { ApiError, api } from "@/lib/api";
import { useLookup, useUrlState } from "@/lib/hooks";
import type { Page, Prefix } from "@/lib/types";
import { PrefixSheet } from "./prefix-sheet";
import { PrefixWizard } from "./prefix-wizard";
import { useNetBoxLabel } from "@/lib/system";

const LIMIT = 50;

export function NotConfigured() {
  return (
    <EmptyState
      icon={Settings}
      title="Connect Lanvex to NetBox"
      action={
        <Button asChild variant="primary">
          <Link to="/settings">Open connection settings</Link>
        </Button>
      }
    >
      Set LANVEX_NETBOX_URL and LANVEX_NETBOX_TOKEN, then restart Lanvex.
    </EmptyState>
  );
}

export function isNotConfigured(e: unknown) {
  return e instanceof ApiError && e.code === "netbox_not_configured";
}

export function NetworksPage() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const { values, set, offset } = useUrlState(["q", "family", "status", "vrf", "prefix"] as const);
  const [creating, setCreating] = React.useState(false);
  const vrfs = useLookup("vrfs");

  const query = {
    q: values.q,
    family: values.family,
    status: values.status,
    vrf_id: values.vrf && values.vrf !== "global" ? values.vrf : undefined,
    global_only: values.vrf === "global" || undefined,
    limit: LIMIT,
    offset,
  };
  const list = useQuery({
    queryKey: ["prefixes", "list", query],
    queryFn: ({ signal }) => api<Page<Prefix>>("/prefixes", { query, signal }),
    placeholderData: keepPreviousData,
  });

  const filtered = !!(values.q || values.family || values.status || values.vrf);
  const minDepth = Math.min(...(list.data?.results.map((p) => p.depth) ?? [0]));

  const columns: Column<Prefix>[] = [
    {
      key: "prefix",
      header: "Prefix",
      cell: (p) => (
        <span className="flex items-center" style={{ paddingLeft: `${Math.max(0, p.depth - minDepth) * 16}px` }}>
          {p.depth > minDepth && <span className="mr-1.5 h-3 w-2 border-b border-l border-line-strong" aria-hidden />}
          <span className="addr font-medium">{p.prefix}</span>
        </span>
      ),
    },
    { key: "name", header: "Name", cell: (p) => p.name || <span className="text-faint">—</span>, className: "max-w-64 truncate" },
    { key: "status", header: "Status", cell: (p) => <StatusBadge value={p.status.value} label={p.status.label} /> },
    { key: "vrf", header: "VRF", cell: (p) => p.vrf?.name ?? <span className="text-muted">Global</span> },
    { key: "vlan", header: "VLAN", cell: (p) => (p.vlan ? `${p.vlan.vid}` : <span className="text-faint">—</span>), className: "tabular-nums" },
    { key: "util", header: "Utilization", cell: (p) => <UtilStrip u={p.utilization} />, headerClassName: "w-44" },
  ];

  const selected = values.prefix ? Number(values.prefix) : null;

  return (
    <>
      <PageHeader
        title="Networks"
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> New network
            </Button>
          )
        }
      />
      {isNotConfigured(list.error) ? (
        <NotConfigured />
      ) : (
        <>
          <Toolbar>
            <SearchInput label="Search prefixes" placeholder="Search prefix or name" value={values.q} onChange={(q) => set({ q })} />
            <Select aria-label="Address family" className="w-auto" value={values.family} onChange={(e) => set({ family: e.target.value })}>
              <option value="">IPv4 and IPv6</option>
              <option value="4">IPv4</option>
              <option value="6">IPv6</option>
            </Select>
            <Select aria-label="Status" className="w-auto" value={values.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="">Any status</option>
              <option value="active">Active</option>
              <option value="reserved">Reserved</option>
              <option value="container">Container</option>
              <option value="deprecated">Deprecated</option>
            </Select>
            <Select aria-label="VRF" className="w-auto" value={values.vrf} onChange={(e) => set({ vrf: e.target.value })}>
              <option value="">All VRFs</option>
              <option value="global">Global table</option>
              {vrfs.data?.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                </option>
              ))}
            </Select>
            {filtered && (
              <Button variant="ghost" size="sm" onClick={() => set({ q: null, family: null, status: null, vrf: null })}>
                Clear filters
              </Button>
            )}
          </Toolbar>
          <DataTable
            columns={columns}
            rows={list.data?.results}
            rowKey={(p) => p.id}
            loading={list.isPending}
            error={list.error}
            onRetry={() => void list.refetch()}
            onRowClick={(p) => set({ prefix: p.id, offset: offset || null })}
            rowLabel={(p) => `Open ${p.prefix}`}
            selectedKey={selected}
            empty={
              filtered ? (
                <EmptyState icon={Network} title="No prefixes match these filters">
                  Try a broader search or clear the filters.
                </EmptyState>
              ) : (
                <EmptyState
                  icon={Network}
                  title="No networks yet"
                  action={
                    isAdmin && (
                      <Button variant="primary" onClick={() => setCreating(true)}>
                        <Plus /> New network
                      </Button>
                    )
                  }
                >
                  Document your first prefix. It is recorded in {nbName}; no device is configured.
                </EmptyState>
              )
            }
            footer={list.data && <Pagination count={list.data.count} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o, prefix: values.prefix })} noun="prefixes" />}
          />
        </>
      )}

      <PrefixWizard open={creating} onOpenChange={setCreating} onCreated={(p) => set({ prefix: p.id, q: null, status: null, family: null, vrf: null })} />
      <Dialog open={selected !== null} onOpenChange={(o) => !o && set({ prefix: null, offset: offset || null })}>
        {selected !== null && <PrefixSheet id={selected} onClose={() => set({ prefix: null, offset: offset || null })} onSelect={(id) => set({ prefix: id, offset: offset || null })} />}
      </Dialog>
    </>
  );
}
