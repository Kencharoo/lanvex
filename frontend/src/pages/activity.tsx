import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { History } from "lucide-react";
import * as React from "react";
import { DataTable, Pagination, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent } from "@/components/ui/dialog";
import { Select } from "@/components/ui/form";
import { Badge, EmptyState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useUrlState } from "@/lib/hooks";
import type { AuditEvent, Page } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";

const LIMIT = 50;

const ACTION_LABELS: Record<string, string> = {
  "auth.login": "Signed in",
  "auth.logout": "Signed out",
  "auth.password_change": "Changed password",
  "prefix.create": "Created prefix",
  "prefix.update": "Updated prefix",
  "prefix.delete": "Deleted prefix",
  "ip.create": "Added IP address",
  "ip.allocate": "Reserved IP address",
  "ip.update": "Updated IP address",
  "ip.assign": "Assigned IP address",
  "ip.release": "Released IP address",
  "ip.delete": "Deleted IP address",
  "vlan.create": "Created VLAN",
  "vlan.update": "Updated VLAN",
  "vlan.delete": "Deleted VLAN",
  "interface.vlans": "Changed interface VLANs",
  "user.create": "Created user",
  "user.update": "Updated user",
};

export function ActivityPage() {
  const { values, set, offset } = useUrlState(["q", "action", "outcome"] as const);
  const [open, setOpen] = React.useState<AuditEvent | null>(null);
  const query = { q: values.q, action: values.action, outcome: values.outcome, limit: LIMIT, offset };
  const list = useQuery({
    queryKey: ["audit", query],
    queryFn: ({ signal }) => api<Page<AuditEvent>>("/audit-events", { query, signal }),
    placeholderData: keepPreviousData,
  });

  const columns: Column<AuditEvent>[] = [
    {
      key: "time",
      header: "When",
      cell: (e) => <span title={absoluteTime(e.occurred_at)}>{relativeTime(e.occurred_at)}</span>,
      className: "whitespace-nowrap text-muted",
      headerClassName: "w-32",
    },
    { key: "actor", header: "User", cell: (e) => e.actor_username ?? <span className="text-faint">Unknown</span> },
    { key: "action", header: "Action", cell: (e) => ACTION_LABELS[e.action] ?? e.action },
    { key: "target", header: "Object", cell: (e) => (e.target_repr ? <span className="addr">{e.target_repr}</span> : <span className="text-faint">—</span>) },
    {
      key: "outcome",
      header: "Result",
      cell: (e) =>
        e.outcome === "success" ? <Badge tone="ok">Succeeded</Badge> : e.outcome === "denied" ? <Badge tone="warn">Denied</Badge> : <Badge tone="danger">Failed</Badge>,
    },
    { key: "ip", header: "From", cell: (e) => <span className="addr text-muted">{e.ip_address ?? "—"}</span> },
  ];

  return (
    <>
      <PageHeader title="Activity & Audit Logs" description="Every action taken through Lanvex, including failed and denied attempts. Changes made directly in NetBox appear in NetBox's own change log." />
      <Toolbar>
        <SearchInput label="Search activity" placeholder="Search user, object, action" value={values.q} onChange={(q) => set({ q })} />
        <Select aria-label="Area" className="w-auto" value={values.action} onChange={(e) => set({ action: e.target.value })}>
          <option value="">All areas</option>
          <option value="prefix.">Networks</option>
          <option value="ip.">IP addresses</option>
          <option value="vlan.">VLANs</option>
          <option value="interface.">Interfaces</option>
          <option value="auth.">Sign-in</option>
          <option value="user.">Users</option>
        </Select>
        <Select aria-label="Result" className="w-auto" value={values.outcome} onChange={(e) => set({ outcome: e.target.value })}>
          <option value="">Any result</option>
          <option value="success">Succeeded</option>
          <option value="failure">Failed</option>
          <option value="denied">Denied</option>
        </Select>
        {(values.q || values.action || values.outcome) && (
          <Button variant="ghost" size="sm" onClick={() => set({ q: null, action: null, outcome: null })}>
            Clear filters
          </Button>
        )}
      </Toolbar>
      <DataTable
        columns={columns}
        rows={list.data?.results}
        rowKey={(e) => e.id}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        onRowClick={setOpen}
        rowLabel={(e) => `Details for ${ACTION_LABELS[e.action] ?? e.action}`}
        empty={<EmptyState icon={History} title="No activity matches">Actions appear here as soon as anyone uses Lanvex.</EmptyState>}
        footer={list.data && <Pagination count={list.data.count} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o })} noun="events" />}
      />
      <Dialog open={!!open} onOpenChange={(o) => !o && setOpen(null)}>
        {open && (
          <DialogContent title={ACTION_LABELS[open.action] ?? open.action} description={absoluteTime(open.occurred_at)} width="max-w-xl">
            <DialogBody>
              <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-1.5 text-[13px]">
                <dt className="text-muted">User</dt>
                <dd>{open.actor_username ?? "Unknown"}</dd>
                <dt className="text-muted">Object</dt>
                <dd className="addr">{[open.target_type, open.target_repr].filter(Boolean).join(" ") || "—"}</dd>
                <dt className="text-muted">Result</dt>
                <dd className="capitalize">{open.outcome}</dd>
                <dt className="text-muted">Request ID</dt>
                <dd className="addr">{open.request_id ?? "—"}</dd>
              </dl>
              {open.detail && (
                <pre className="addr max-h-80 overflow-auto rounded-[var(--radius-control)] bg-surface-2 p-3 text-xs">{JSON.stringify(open.detail, null, 2)}</pre>
              )}
            </DialogBody>
          </DialogContent>
        )}
      </Dialog>
    </>
  );
}
