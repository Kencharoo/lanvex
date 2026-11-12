import { CheckCircle2, CircleHelp, XCircle } from "lucide-react";
import type * as React from "react";
import { Badge, type Tone } from "@/components/ui/misc";
import type { ChangeCheck, ChangeDetail, ChangeStatus } from "@/lib/types";
import { absoluteTime, cn } from "@/lib/utils";

/** Wording reflects what Lanvex knows, never more: "rolled back" and "needs attention" are not successes. */
export const CHANGE_STATUS: Record<ChangeStatus, { tone: Tone; label: string; active?: boolean }> = {
  pending_approval: { tone: "accent", label: "Waiting for approval" },
  approved: { tone: "info", label: "Approved, queued", active: true },
  running: { tone: "info", label: "Applying", active: true },
  verifying: { tone: "info", label: "Verifying", active: true },
  succeeded: { tone: "ok", label: "Applied and verified" },
  rolled_back: { tone: "warn", label: "Rolled back" },
  failed: { tone: "danger", label: "Not applied" },
  cancelled: { tone: "neutral", label: "Cancelled" },
  needs_attention: { tone: "danger", label: "Needs attention" },
};

export function ChangeStatusBadge({ status }: { status: ChangeStatus }) {
  const s = CHANGE_STATUS[status];
  return (
    <Badge tone={s.tone}>
      <span className={cn("size-1.5 rounded-full bg-current", s.active && "animate-pulse")} aria-hidden />
      {s.label}
    </Badge>
  );
}

export function isActive(status: ChangeStatus) {
  return !!CHANGE_STATUS[status].active;
}

/** A unified diff with added and removed lines marked. Text, so it can be copied and read by screen readers. */
export function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="max-h-[28rem] overflow-auto rounded-[var(--radius-control)] border border-line bg-surface-2/40 py-2 font-mono text-xs leading-5">
      {lines.map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("@@") ? "hunk" : line[0];
        return (
          <div
            key={i}
            className={cn(
              "whitespace-pre-wrap px-3 [overflow-wrap:anywhere]",
              kind === "+" && "bg-ok-soft text-ok",
              kind === "-" && "bg-danger-soft text-danger",
              kind === "hunk" && "text-faint",
              kind === "file" && "text-muted",
            )}
          >
            {kind === "+" || kind === "-" ? <span className="sr-only">{kind === "+" ? "added: " : "removed: "}</span> : null}
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

export function CheckList({ checks }: { checks: ChangeCheck[] }) {
  return (
    <ul className="grid gap-2">
      {checks.map((c) => {
        const Icon = c.ok === true ? CheckCircle2 : c.ok === false ? XCircle : CircleHelp;
        return (
          <li key={c.id} className="flex gap-2.5 text-[13px]">
            <Icon className={cn("mt-0.5 size-4 shrink-0", c.ok === true ? "text-ok" : c.ok === false ? "text-danger" : "text-faint")} aria-hidden />
            <span className="grid">
              <span className="font-medium">
                {c.label}
                <span className="sr-only">{c.ok === true ? ": passed" : c.ok === false ? ": failed" : ": not checked"}</span>
              </span>
              <span className="text-xs text-muted">{c.detail}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function Timeline({ log }: { log: ChangeDetail["log"] }) {
  return (
    <ol className="grid gap-3 border-l border-line pl-4">
      {log.map((e, i) => (
        <li key={i} className="relative text-[13px]">
          <span
            className={cn(
              "absolute -left-[21px] top-1.5 size-2.5 rounded-full border-2 border-surface",
              e.level === "error" ? "bg-danger" : e.level === "warning" ? "bg-warn" : "bg-line-strong",
            )}
            aria-hidden
          />
          <p className="text-xs text-faint">{absoluteTime(e.at)}</p>
          <p className={cn(e.level === "error" && "text-danger")}>{e.message}</p>
        </li>
      ))}
    </ol>
  );
}

function list(values: string[] | undefined, empty = "None") {
  return values && values.length ? values.join(", ") : <span className="text-faint">{empty}</span>;
}

type Row = [string, React.ReactNode, React.ReactNode, boolean];

/** DNS is only part of a change when the request says so; null means "left as it is". */
function dnsRow(label: string, b: ChangeDetail["before"], before: string[] | undefined, after: string[] | null): Row {
  const now = b && b.dns_known ? list(before) : <span className="text-faint">Not reported</span>;
  if (after === null) return [label, now, <span className="text-faint">Unchanged</span>, false];
  return [label, now, list(after), !b || !b.dns_known || (before ?? []).join() !== after.join()];
}

/** The interface before (as last read) and after (as requested), row by row, changes highlighted. */
export function BeforeAfter({ change }: { change: ChangeDetail }) {
  const b = change.before;
  const r = change.request;
  const rows: Row[] = [
    ["Method", b ? (b.method === "dhcp" ? "DHCP" : "Static") : "—", r.method === "dhcp" ? "DHCP" : "Static", !!b && b.method !== r.method],
    [
      "Addresses",
      b ? list([...b.addresses, ...b.dynamic_addresses.map((a) => `${a} (DHCP)`)]) : "—",
      r.method === "dhcp" ? <span className="text-faint">From DHCP</span> : list(r.addresses),
      !b || b.addresses.join() !== r.addresses.join() || (r.method === "dhcp") !== (b.method === "dhcp"),
    ],
    ["Default gateway", b ? b.gateway ?? <span className="text-faint">None</span> : "—", r.method === "dhcp" ? <span className="text-faint">From DHCP</span> : r.gateway ?? <span className="text-faint">None</span>, !b || (b.gateway ?? null) !== (r.gateway ?? null)],
    dnsRow("DNS servers", b, b?.dns_servers, r.dns_servers),
    dnsRow("Search domains", b, b?.dns_search, r.dns_search),
  ];
  return (
    <div className="overflow-x-auto rounded-[var(--radius-panel)] border border-line bg-surface">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            <th className="px-3 py-2 font-medium">{change.interface}</th>
            <th className="px-3 py-2 font-medium">{change.status === "pending_approval" ? "Now (last read)" : "Before"}</th>
            <th className="px-3 py-2 font-medium">{change.status === "pending_approval" ? "After the change" : "Requested"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, before, after, changed]) => (
            <tr key={label} className="border-b border-line last:border-0">
              <th scope="row" className="px-3 py-2 text-left font-normal text-muted">
                {label}
              </th>
              <td className="addr px-3 py-2">{before}</td>
              <td className={cn("addr px-3 py-2", changed && "font-medium text-ink")}>
                {after}
                {changed && <span className="sr-only"> (changes)</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

