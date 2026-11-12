import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldCheck, Undo2 } from "lucide-react";
import * as React from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { ChangeDetail } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { BeforeAfter, CheckList, ChangeStatusBadge, DiffView, Timeline, isActive } from "./shared";

export function ChangeDetailPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const [approving, setApproving] = React.useState(false);

  const q = useQuery({
    queryKey: ["changes", id],
    queryFn: () => api<ChangeDetail>(`/changes/${id}`),
    // Follow a running change closely; the server's own deadline is a matter of seconds.
    refetchInterval: (query) => (query.state.data && isActive(query.state.data.status) ? 2000 : false),
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["changes"] });
    void qc.invalidateQueries({ queryKey: ["servers"] });
  };
  const cancel = useMutation({
    mutationFn: () => api<ChangeDetail>(`/changes/${id}/cancel`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Change cancelled. Nothing was changed on the server.");
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const recheck = useMutation({
    mutationFn: () => api<ChangeDetail>(`/changes/${id}/recheck`, { method: "POST" }),
    onSuccess: (c) => {
      toast.message(c.status === "needs_attention" ? "Still no answer from the server." : "Lanvex found out what happened.");
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const c = q.data;
  const pending = c?.status === "pending_approval";
  if (q.error) {
    return (
      <>
        <PageHeader crumbs={[{ label: "Automation", to: "/automation" }, { label: "Change" }]} title="Change" />
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      </>
    );
  }
  if (!c) return <Skeleton className="h-64 w-full" />;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Automation", to: "/automation" }, { label: `Change #${c.number}` }]}
        title={
          <span className="flex flex-wrap items-center gap-2">
            Change #{c.number}: {c.interface} on {c.server_name}
            <ChangeStatusBadge status={c.status} />
          </span>
        }
        description={
          <>
            Prepared by {c.requested_by} {relativeTime(c.created_at)}
            {c.approved_by && <>, approved by {c.approved_by} {relativeTime(c.approved_at)}</>}
            {" · "}
            <Link to={`/servers/${c.server_id}`} className="text-accent hover:underline">
              Open server
            </Link>
          </>
        }
        actions={
          <>
            {c.can_cancel && (
              <Button variant="secondary" onClick={() => cancel.mutate()} loading={cancel.isPending}>
                Cancel change
              </Button>
            )}
            {c.can_recheck && (
              <Button variant="secondary" onClick={() => recheck.mutate()} loading={recheck.isPending}>
                <RefreshCw /> Check again
              </Button>
            )}
            {c.status === "pending_approval" && (c.can_approve || c.approve_blocker) && (
              <Button variant="primary" disabled={!c.can_approve} onClick={() => setApproving(true)}>
                <ShieldCheck /> Approve and apply
              </Button>
            )}
          </>
        }
      />

      <div className="grid gap-8">
        <Outcome change={c} />

        <Section title={pending ? "What changes" : "What was requested"} description={c.summary}>
          <BeforeAfter change={c} />
        </Section>

        {c.preview && (
          <Section
            title={pending ? "Exactly what Lanvex will do" : "What Lanvex was approved to do"}
            description={`Prepared by the helper on the server, against its configuration at that moment (${c.preview.backend === "netplan" ? "netplan" : "NetworkManager"}).`}
          >
            {c.preview.warnings.length > 0 && (
              <Notice tone="warn" title={pending ? "Before you approve" : "Shown before approval"}>
                <ul className="list-disc pl-4">
                  {c.preview.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </Notice>
            )}
            {c.preview.files.map((f) => (
              <div key={f.path} className="grid gap-1.5">
                <p className="text-xs text-muted">
                  {f.label}: <span className="addr text-ink">{f.path}</span>
                </p>
                <DiffView diff={f.diff} />
              </div>
            ))}
            <div className="grid gap-1 text-xs text-muted">
              <p>Then, on the server:</p>
              <ol className="list-decimal pl-5">
                {c.preview.commands.map((cmd) => (
                  <li key={cmd} className="addr">
                    {cmd}
                  </li>
                ))}
              </ol>
              <p>
                Before anything is written, the server arms a timer that restores {c.preview.files.length === 1 ? "this file" : "these files"} after{" "}
                {c.rollback_seconds} seconds unless Lanvex confirms the change. Values that look like passwords are shown as [redacted].
              </p>
            </div>
          </Section>
        )}

        <div className="grid gap-8 lg:grid-cols-2">
          {c.checks.length > 0 && (
            <Section title="Verification" description="Run on the server after the change, before Lanvex confirms it.">
              <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
                <CheckList checks={c.checks} />
              </div>
            </Section>
          )}
          <Section title="Timeline">
            <div className="rounded-[var(--radius-panel)] border border-line bg-surface p-4 pl-6">
              <Timeline log={c.log} />
            </div>
          </Section>
        </div>
      </div>

      <ApproveDialog change={c} open={approving} onClose={() => setApproving(false)} onApproved={refresh} />
    </>
  );
}

function Outcome({ change: c }: { change: ChangeDetail }) {
  switch (c.status) {
    case "pending_approval":
      return (
        <Notice tone="info" title="Nothing has been changed yet">
          {c.approve_blocker ?? "Review the change below. It is applied only after an administrator approves it."}
        </Notice>
      );
    case "approved":
      return <Notice title="Approved. Lanvex starts applying it in a moment." />;
    case "running":
      return <Notice title="Applying on the server">The server has backed up its configuration and armed its rollback timer.</Notice>;
    case "verifying":
      return (
        <Notice title="Applied; Lanvex is checking the result">
          Unless every check passes and Lanvex confirms, the server restores its previous configuration
          {c.deadline_at ? ` at ${absoluteTime(c.deadline_at)}` : ""}.
        </Notice>
      );
    case "succeeded":
      return <Notice title="Applied and verified">{c.outcome}</Notice>;
    case "rolled_back":
      return (
        <Notice tone="warn" title="Rolled back: the previous configuration is in place">
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
        <Notice tone="danger" title="Lanvex does not know the server's current state">
          {c.outcome}
        </Notice>
      );
  }
}

function ApproveDialog({ change: c, open, onClose, onApproved }: { change: ChangeDetail; open: boolean; onClose: () => void; onApproved: () => void }) {
  const [name, setName] = React.useState("");
  const approve = useMutation({
    mutationFn: () => api<ChangeDetail>(`/changes/${c.id}/approve`, { method: "POST", body: { confirm_name: name } }),
    onSuccess: () => {
      toast.success(`Change #${c.number} approved. Lanvex is applying it now.`);
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
      <DialogContent title={`Apply change #${c.number} to ${c.server_name}?`} description={c.summary}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            approve.mutate();
          }}
        >
          <DialogBody>
            <ol className="grid list-decimal gap-1.5 pl-5 text-[13px] text-muted">
              <li>The server backs up {c.preview?.files.map((f) => f.path).join(", ")} and arms a {c.rollback_seconds}-second rollback timer.</li>
              <li>It writes the new configuration and applies it.</li>
              <li>Lanvex signs in again and checks the result. Only if every check passes does it confirm the change.</li>
              <li>Otherwise, or if Lanvex cannot reach the server, the previous configuration comes back automatically.</li>
            </ol>
            {approve.error && !fieldError(approve.error, "confirm_name") && <Notice tone="danger">{errorMessage(approve.error)}</Notice>}
            <Field label={`Type ${c.server_name} to approve`} htmlFor="confirm-name" error={fieldError(approve.error, "confirm_name")}>
              <Input id="confirm-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" autoFocus />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              <Undo2 /> Not now
            </Button>
            <Button type="submit" variant="primary" disabled={name !== c.server_name} loading={approve.isPending}>
              Approve and apply
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
