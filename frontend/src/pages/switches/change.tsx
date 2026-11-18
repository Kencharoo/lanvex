import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Undo2 } from "lucide-react";
import * as React from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { Notice, Skeleton } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { SwitchChangeDetail } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { ChangeStatusBadge, Timeline, isActive } from "../changes/shared";

export function vlansText(untagged: number[], tagged: number[]): string {
  const parts = [untagged.length ? `untagged ${untagged.join(", ")}` : null, tagged.length ? `tagged ${tagged.join(", ")}` : null].filter(Boolean);
  return parts.join(", ") || "no VLAN";
}

/** A change of a switch port's VLANs: what it writes, its approval, and how it ended. */
export function SwitchChangePage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const { isAdmin } = useSession();
  const [approving, setApproving] = React.useState(false);
  const q = useQuery({
    queryKey: ["switch-changes", id],
    queryFn: () => api<SwitchChangeDetail>(`/switch-changes/${id}`),
    refetchInterval: (query) => (query.state.data && isActive(query.state.data.status) ? 1500 : false),
  });
  const c = q.data;
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["switch-changes"] });
    if (c) void qc.invalidateQueries({ queryKey: ["switches", c.switch_id] });
  };
  React.useEffect(() => {
    // Finished: the switch was read again, so its ports show the result.
    if (c && !isActive(c.status) && c.status !== "pending_approval") void qc.invalidateQueries({ queryKey: ["switches", c.switch_id] });
  }, [c?.status, c?.switch_id, qc]); // eslint-disable-line react-hooks/exhaustive-deps
  const cancel = useMutation({
    mutationFn: () => api<SwitchChangeDetail>(`/switch-changes/${id}/cancel`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Change cancelled. Nothing was changed on the switch.");
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  if (q.error && !c) {
    return (
      <>
        <PageHeader crumbs={[{ label: "Automation", to: "/automation" }, { label: "Change" }]} title="Change" />
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      </>
    );
  }
  if (!c) return <Skeleton className="h-64 w-full" />;
  const pending = c.status === "pending_approval";

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Automation", to: "/automation" }, { label: `Change #${c.number}` }]}
        title={
          <span className="flex flex-wrap items-center gap-2">
            Change #{c.number}: {c.port} on {c.switch_name}
            <ChangeStatusBadge status={c.status} />
          </span>
        }
        description={
          <>
            Prepared by {c.requested_by} {relativeTime(c.created_at)}
            {c.approved_by && <>, approved by {c.approved_by} {relativeTime(c.approved_at)}</>}
            {" · "}
            <Link to={`/switches/${c.switch_id}`} className="text-accent hover:underline">
              Open switch
            </Link>
          </>
        }
        actions={
          isAdmin && (
            <>
              {(c.status === "pending_approval" || c.status === "approved") && (
                <Button variant="secondary" onClick={() => cancel.mutate()} loading={cancel.isPending}>
                  Cancel change
                </Button>
              )}
              {pending && (
                <Button variant="primary" disabled={!!c.approve_blocker} onClick={() => setApproving(true)}>
                  <ShieldCheck /> Approve and apply
                </Button>
              )}
            </>
          )
        }
      />

      <div className="grid gap-8">
        <Outcome change={c} />

        <Section title={pending ? "What changes" : "What was requested"} description={c.summary}>
          <PortBeforeAfter change={c} />
        </Section>

        <Section title={pending ? "Exactly what Lanvex will write" : "What Lanvex was approved to write"} description="SNMP SET, in this order">
          {c.warnings.length > 0 && (
            <Notice tone="warn" title={pending ? "Before you approve" : "Shown before approval"}>
              <ul className="list-disc pl-4">
                {c.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Notice>
          )}
          <ol className="grid gap-2 rounded-[var(--radius-panel)] border border-line bg-surface p-4 pl-9 text-[13px] list-decimal">
            {c.writes.map((w) => (
              <li key={w.text}>
                <span className="font-medium">{w.text}</span>
                <span className="addr block text-xs text-muted">{w.oids.join(", ")}</span>
              </li>
            ))}
          </ol>
          <p className="text-xs text-muted">Checked against the switch before writing and read back after. A refused write or a different result is undone.</p>
        </Section>

        <Section title="Timeline">
          <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4 pl-6">
            <Timeline log={c.log} />
          </div>
        </Section>
      </div>

      <ApproveDialog change={c} open={approving} onClose={() => setApproving(false)} onApproved={refresh} />
    </>
  );
}

function Outcome({ change: c }: { change: SwitchChangeDetail }) {
  switch (c.status) {
    case "pending_approval":
      return (
        <Notice tone="info" title="Not applied yet">
          {c.approve_blocker ?? "Written to the switch after approval."}
        </Notice>
      );
    case "approved":
      return <Notice title="Approved" />;
    case "running":
      return <Notice title="Writing" />;
    case "verifying":
      return <Notice title="Reading the port back" />;
    case "succeeded":
      return <Notice title="Applied and verified">{c.outcome}</Notice>;
    case "rolled_back":
      return (
        <Notice tone="warn" title="Undone">
          {c.outcome}
        </Notice>
      );
    case "failed":
      return (
        <Notice tone="danger" title="Not applied">
          {c.outcome}
        </Notice>
      );
    case "cancelled":
      return <Notice title="Cancelled">{c.outcome}</Notice>;
    case "needs_attention":
      return (
        <Notice tone="danger" title="Lanvex does not know the port's current state">
          {c.outcome}
        </Notice>
      );
  }
}

function PortBeforeAfter({ change: c }: { change: SwitchChangeDetail }) {
  const pending = c.status === "pending_approval";
  const b = c.before;
  const r = c.request;
  const list = (v: number[]) => (v.length ? v.join(", ") : <span className="text-faint">None</span>);
  const rows: [string, React.ReactNode, React.ReactNode, boolean][] = [
    ["Untagged VLAN", list(b.untagged), r.untagged, b.untagged.join() !== String(r.untagged)],
    ["Tagged VLANs", list(b.tagged), list(r.tagged), b.tagged.join() !== r.tagged.join()],
    ["PVID", b.pvid ?? <span className="text-faint">—</span>, r.untagged, b.pvid !== r.untagged],
  ];
  return (
    <div className="overflow-x-auto rounded-[var(--radius-panel)] border border-line bg-surface">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            <th className="px-3 py-2 font-medium">{c.port}</th>
            <th className="px-3 py-2 font-medium">{pending ? "Now (read for the preview)" : "Before"}</th>
            <th className="px-3 py-2 font-medium">{pending ? "After the change" : "Requested"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, before, after, changed]) => (
            <tr key={label} className="border-b border-line last:border-0">
              <th scope="row" className="px-3 py-2 text-left font-normal text-muted">
                {label}
              </th>
              <td className="px-3 py-2 tabular-nums">{before}</td>
              <td className={cn("px-3 py-2 tabular-nums", changed && "font-medium text-ink")}>
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

function ApproveDialog({ change: c, open, onClose, onApproved }: { change: SwitchChangeDetail; open: boolean; onClose: () => void; onApproved: () => void }) {
  const [name, setName] = React.useState("");
  const approve = useMutation({
    mutationFn: () => api<SwitchChangeDetail>(`/switch-changes/${c.id}/approve`, { method: "POST", body: { confirm_name: name } }),
    onSuccess: () => {
      toast.success(`Change #${c.number} approved. Lanvex is writing it now.`);
      onApproved();
      onClose();
    },
  });
  React.useEffect(() => {
    if (open) {
      setName("");
      approve.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Apply change #${c.number} to ${c.switch_name}?`} description={c.summary}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            approve.mutate();
          }}
        >
          <DialogBody>
            {c.warnings.length > 0 && (
              <ul className="grid list-disc gap-1 pl-5 text-[13px] text-muted">
                {c.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            {approve.error && !fieldError(approve.error, "confirm_name") && <Notice tone="danger">{errorMessage(approve.error)}</Notice>}
            <Field label={`Type ${c.port} to approve`} htmlFor="confirm-port" error={fieldError(approve.error, "confirm_name")}>
              <Input id="confirm-port" className="addr" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" autoFocus />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              <Undo2 /> Not now
            </Button>
            <Button type="submit" variant="primary" disabled={name !== c.port} loading={approve.isPending}>
              Approve and apply
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
