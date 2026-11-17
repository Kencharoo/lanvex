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
import type { ChangeOut, ChangeStatus } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { useSystemInfo } from "../servers";
import { CHANGE_STATUS, ChangeStatusBadge, isActive } from "./shared";

const LIMIT = 200;

/** Server configuration changes, across all servers: what waits for approval, what runs, and how each one ended. */
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

  const columns: Column<ChangeOut>[] = [
    { key: "number", header: "#", cell: (c) => <span className="addr text-muted">{c.number}</span>, headerClassName: "w-12" },
    {
      key: "what",
      header: "Change",
      cell: (c) => (
        <span className="grid">
          <span className="font-medium">
            {c.server_name} · <span className="addr">{c.interface}</span>
          </span>
          <span className="text-xs text-muted">{c.summary.replace(`${c.interface}: `, "")}</span>
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
      cell: (c) => {
        const at = c.finished_at ?? c.started_at ?? c.approved_at ?? c.created_at;
        return <span title={absoluteTime(at)}>{relativeTime(at)}</span>;
      },
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
        rows={list.data}
        rowKey={(c) => c.id}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        onRowClick={(c) => navigate(`/changes/${c.id}`)}
        rowLabel={(c) => `Open change #${c.number}`}
        empty={
          <EmptyState icon={Workflow} title={show === "all" ? "No changes yet" : "No changes match"}>
            {show === "all" ? "Open a server and choose Change IP settings to prepare one." : "Try another filter."}
          </EmptyState>
        }
      />
    </>
  );
}
