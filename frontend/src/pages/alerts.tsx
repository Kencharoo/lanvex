import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon, AlertTriangle, BellOff, Check, Info } from "lucide-react";
import { Link } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { useUrlState } from "@/lib/hooks";
import type { Alert, AlertKind, AlertList, Severity } from "@/lib/types";
import { absoluteTime, cn, relativeTime } from "@/lib/utils";

export const KIND_LABEL: Record<AlertKind, string> = {
  unreachable: "Unreachable",
  read_failed: "Read failed",
  drift: "Drift",
  pool_full: "DHCP pool",
  unknown_device: "Unknown device",
  change_failed: "Change",
  port_down: "Port down",
  device_moved: "Device moved",
};

const SEVERITY_ICON: Record<Severity, { icon: typeof Info; className: string; label: string }> = {
  critical: { icon: AlertOctagon, className: "text-danger", label: "Critical" },
  warning: { icon: AlertTriangle, className: "text-warn", label: "Warning" },
  info: { icon: Info, className: "text-info", label: "Info" },
};

export function SeverityIcon({ severity }: { severity: Severity }) {
  const s = SEVERITY_ICON[severity];
  return <s.icon className={cn("size-4 shrink-0", s.className)} aria-label={s.label} />;
}

const STATES = [
  { v: "open", label: "Open" },
  { v: "resolved", label: "Resolved" },
  { v: "all", label: "All" },
] as const;

export function AlertsPage() {
  const { isAdmin } = useSession();
  const { values: url, set: setUrl } = useUrlState(["state"] as const);
  const state = STATES.find((s) => s.v === url.state)?.v ?? "open";
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["alerts", state],
    queryFn: () => api<AlertList>(`/alerts?state=${state}`),
    refetchInterval: 30_000,
  });
  const ack = useMutation({
    mutationFn: (a: Alert) => api<Alert>(`/alerts/${a.id}/acknowledge`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["alerts"] }),
    onError: (e) => toast.error(errorMessage(e)),
  });

  const columns: Column<Alert>[] = [
    {
      key: "severity",
      header: <span className="sr-only">Severity</span>,
      className: "w-8",
      cell: (a) => <SeverityIcon severity={a.severity} />,
    },
    {
      key: "title",
      header: "Alert",
      cell: (a) => (
        <div className="min-w-0">
          <div className={cn("font-medium", a.state === "resolved" && "text-muted")}>{a.title}</div>
          {a.detail.length > 0 && (
            <ul className="mt-0.5 grid gap-0.5 text-xs text-muted">
              {a.detail.slice(0, 6).map((d) => (
                <li key={d} className="truncate">
                  {d}
                </li>
              ))}
              {a.detail.length > 6 && <li>+{a.detail.length - 6} more</li>}
            </ul>
          )}
        </div>
      ),
    },
    {
      key: "kind",
      header: "Type",
      className: "whitespace-nowrap",
      cell: (a) => <Badge>{KIND_LABEL[a.kind] ?? a.kind}</Badge>,
    },
    {
      key: "server",
      header: "Source",
      className: "whitespace-nowrap",
      cell: (a) =>
        a.server_id || a.switch_id ? (
          <Link
            to={a.switch_id ? `/switches/${a.switch_id}` : `/servers/${a.server_id}`}
            className="hover:text-accent"
            onClick={(e) => e.stopPropagation()}
          >
            {a.server_name}
          </Link>
        ) : (
          a.server_name || "—"
        ),
    },
    {
      key: "when",
      header: "Since",
      className: "whitespace-nowrap text-muted",
      cell: (a) => (
        <span title={absoluteTime(a.opened_at)}>
          {a.state === "resolved" && a.resolved_at ? `Resolved ${relativeTime(a.resolved_at)}` : relativeTime(a.opened_at)}
        </span>
      ),
    },
    {
      key: "ack",
      header: <span className="sr-only">Acknowledge</span>,
      className: "w-px whitespace-nowrap text-right",
      cell: (a) =>
        a.acknowledged_at ? (
          <span className="inline-flex items-center gap-1 text-xs text-muted" title={`${a.acknowledged_by ?? ""}, ${absoluteTime(a.acknowledged_at)}`}>
            <Check className="size-3.5" /> {a.acknowledged_by}
          </span>
        ) : a.state === "open" && isAdmin ? (
          <Button size="sm" variant="ghost" loading={ack.isPending && ack.variables?.id === a.id} onClick={() => ack.mutate(a)}>
            Acknowledge
          </Button>
        ) : null,
    },
  ];

  return (
    <>
      <PageHeader
        title="Alerts"
        actions={
          isAdmin && (
            <Link to="/settings#monitoring" className="text-[13px] text-muted hover:text-ink">
              Monitoring settings
            </Link>
          )
        }
      />
      <div role="tablist" aria-label="Alerts" className="mb-3 inline-grid w-fit grid-cols-3 gap-1 rounded-[var(--radius-control)] bg-surface-2 p-1">
        {STATES.map((s) => (
          <button
            key={s.v}
            role="tab"
            aria-selected={state === s.v}
            onClick={() => setUrl({ state: s.v === "open" ? null : s.v })}
            className={cn("rounded px-3 py-1 text-[13px]", state === s.v ? "bg-surface font-medium shadow-sm" : "text-muted hover:text-ink")}
          >
            {s.label}
            {s.v === "open" && q.data && q.data.open > 0 && <span className="ml-1.5 tabular-nums text-muted">{q.data.open}</span>}
          </button>
        ))}
      </div>
      <DataTable
        columns={columns}
        rows={q.data?.alerts}
        rowKey={(a) => a.id}
        loading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={<EmptyState icon={BellOff} title={state === "open" ? "No open alerts" : "No alerts"} />}
      />
    </>
  );
}
