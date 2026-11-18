import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Workflow } from "lucide-react";
import { useNavigate } from "react-router";
import { DataTable, Toolbar, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { EmptyState, Notice } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useUrlState } from "@/lib/hooks";
import type { ChangeOut, ChangeStatus, SwitchChangeOut } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { useSystemInfo } from "../servers";
import { CHANGE_STATUS, ChangeStatusBadge, isActive } from "./shared";

const LIMIT = 200;

/** A server or a switch change, as one row. */
interface Row {
  id: string;
  to: string;
  number: number;
  target: string;
  part: string;
  summary: string;
  status: ChangeStatus;
  requested_by: string;
  approved_by: string | null;
  at: string;
}

function serverRow(c: ChangeOut): Row {
  return {
    id: c.id,
    to: `/changes/${c.id}`,
    number: c.number,
    target: c.server_name,
    part: c.interface,
    summary: c.summary.replace(`${c.interface}: `, ""),
    status: c.status,
    requested_by: c.requested_by,
    approved_by: c.approved_by,
    at: c.finished_at ?? c.started_at ?? c.approved_at ?? c.created_at,
  };
}

function switchRow(c: SwitchChangeOut): Row {
  return {
    id: c.id,
    to: `/changes/switch/${c.id}`,
    number: c.number,
    target: c.switch_name,
    part: c.port,
    summary: c.summary.replace(`${c.port}: `, ""),
    status: c.status,
    requested_by: c.requested_by,
    approved_by: c.approved_by,
    at: c.finished_at ?? c.started_at ?? c.approved_at ?? c.created_at,
  };
}

/** Server and switch changes: what waits for approval, what runs, and how each one ended. */
export function AutomationPage() {
  const navigate = useNavigate();
  const info = useSystemInfo();
  const { values, set } = useUrlState(["show"] as const);
  const show = values.show || "all";
  const query = show === "open" ? { open: true, limit: LIMIT } : show === "all" ? { limit: LIMIT } : { status: show, limit: LIMIT };
  const list = useQuery({
    queryKey: ["changes", "list", query],
    queryFn: ({ signal }) => api<ChangeOut[]>("/changes", { query, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.some((c) => isActive(c.status)) ? 3000 : false),
  });
  const switches = useQuery({
    queryKey: ["switch-changes", "list"],
    queryFn: ({ signal }) => api<SwitchChangeOut[]>("/switch-changes", { signal }),
    refetchInterval: (q) => (q.state.data?.some((c) => isActive(c.status)) ? 3000 : false),
  });
  const keep = (c: { status: ChangeStatus }) => (show === "all" ? true : show === "open" ? c.status === "pending_approval" || isActive(c.status) : c.status === show);
  const rows =
    list.data && switches.data
      ? [...list.data.map(serverRow), ...switches.data.filter(keep).map(switchRow)].sort((a, b) => b.at.localeCompare(a.at))
      : undefined;

  const columns: Column<Row>[] = [
    { key: "number", header: "#", cell: (c) => <span className="addr text-muted">{c.number}</span>, headerClassName: "w-12" },
    {
      key: "what",
      header: "Change",
      cell: (c) => (
        <span className="grid">
          <span className="font-medium">
            {c.target} · <span className="addr">{c.part}</span>
          </span>
          <span className="text-xs text-muted">{c.summary}</span>
        </span>
      ),
    },
    { key: "status", header: "Status", cell: (c) => <ChangeStatusBadge status={c.status} /> },
    {
      key: "who",
      header: "Prepared / approved by",
      cell: (c) => (
        <span className="text-muted">
          {c.requested_by}
          {c.approved_by && <> / {c.approved_by}</>}
        </span>
      ),
    },
    {
      key: "when",
      header: "Last update",
      cell: (c) => <span title={absoluteTime(c.at)}>{relativeTime(c.at)}</span>,
      className: "whitespace-nowrap text-muted",
    },
  ];

  return (
    <>
      <PageHeader
        title="Automation"
      />
      {info.data && !info.data.automation_enabled && (
        <Notice tone="info" title="Changes are turned off">
          Set <span className="addr">LANVEX_AUTOMATION_ENABLED=true</span> and restart Lanvex to enable them.
        </Notice>
      )}
      <Toolbar>
        <Select aria-label="Show" className="w-auto" value={show} onChange={(e) => set({ show: e.target.value === "all" ? null : e.target.value })}>
          <option value="all">All changes</option>
          <option value="open">Not finished yet</option>
          {(Object.keys(CHANGE_STATUS) as ChangeStatus[]).map((s) => (
            <option key={s} value={s}>
              {CHANGE_STATUS[s].label}
            </option>
          ))}
        </Select>
        {show !== "all" && (
          <Button variant="ghost" size="sm" onClick={() => set({ show: null })}>
            Show all
          </Button>
        )}
      </Toolbar>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(c) => c.id}
        loading={list.isPending || switches.isPending}
        error={list.error ?? switches.error}
        onRetry={() => {
          void list.refetch();
          void switches.refetch();
        }}
        onRowClick={(c) => navigate(c.to)}
        rowLabel={(c) => `Open change #${c.number}`}
        empty={
          <EmptyState icon={Workflow} title={show === "all" ? "No changes yet" : "No changes match"}>
            {show === "all" ? "Prepare one from a server's network settings or a switch port's VLANs." : "Try another filter."}
          </EmptyState>
        }
      />
    </>
  );
}
