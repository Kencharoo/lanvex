import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookCheck, Check, RefreshCw, ShieldCheck, Undo2 } from "lucide-react";
import * as React from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import { useSession } from "@/app/session";
import { useNetBoxLabel } from "@/lib/system";
import type { ChangeDetail, Comparison, Server } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { differs, RecordDialog } from "../servers/record";
import { BeforeAfter, CheckList, ChangeStatusBadge, DiffView, Timeline, isActive } from "./shared";

const BACKEND_LABEL: Record<string, string> = { netplan: "netplan", networkmanager: "NetworkManager", ifupdown: "ifupdown" };

export function ChangeDetailPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const [approving, setApproving] = React.useState(false);
  const { isAdmin } = useSession();

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
  if (q.error && !c) {
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
        {q.error && isActive(c.status) && (
          <Notice tone="danger" title="Lanvex not reachable">
            {errorMessage(q.error)}. Unless confirmed, the server rolls back{c.deadline_at ? ` at ${absoluteTime(c.deadline_at)}` : ""}.
          </Notice>
        )}
        {c.can_decide && <BrowserDecision change={c} onDecided={refresh} />}
        <Outcome change={c} />
        {c.status === "succeeded" && isAdmin && c.kind === "interface_ipv4" && <Documentation change={c} />}

        <Section title={pending ? "What changes" : "What was requested"} description={c.summary}>
          <BeforeAfter change={c} />
        </Section>

        {c.preview && (
          <Section
            title={pending ? "Exactly what Lanvex will do" : "What Lanvex was approved to do"}
            description={BACKEND_LABEL[c.preview.backend] ?? c.preview.backend}
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
              <p>Automatic rollback after {c.rollback_seconds} s unless confirmed. Secrets are redacted.</p>
            </div>
          </Section>
        )}

        <div className="grid gap-8 lg:grid-cols-2">
          {c.checks.length > 0 && (
            <Section title="Verification">
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

/** After a successful change: does NetBox document the interface as it is now? Offer to record it. */
function Documentation({ change: c }: { change: ChangeDetail }) {
  const nbName = useNetBoxLabel();
  const [open, setOpen] = React.useState(false);
  const server = useQuery({ queryKey: ["servers", c.server_id], queryFn: () => api<Server>(`/servers/${c.server_id}`) });
  const comparison = useQuery({
    queryKey: ["servers", c.server_id, "comparison"],
    queryFn: () => api<Comparison>(`/servers/${c.server_id}/comparison`),
  });
  const s = server.data;
  const cmp = comparison.data;
  if (!s || !cmp) return null;
  if (!cmp.linked) {
    return (
      <Notice title={`${nbName} was not updated`}>
        <Link to={`/servers/${s.id}`} className="text-accent hover:underline">
          Link {s.name}
        </Link>{" "}
        to a device to record changes.
      </Notice>
    );
  }
  const rows = cmp.rows.filter((r) => differs(r) && (r.server_interface === c.interface || r.netbox_interface === c.interface));
  if (rows.length === 0) {
    return <Notice title={`${nbName} matches the server`} />;
  }
  return (
    <>
      <Notice
        tone="warn"
        title={`${nbName} does not document this yet`}
        action={
          <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
            <BookCheck /> Record in {nbName}…
          </Button>
        }
      >
        {c.interface}: {rows.map((r) => r.server_address ?? r.netbox_address).join(", ")}
      </Notice>
      <RecordDialog server={s} rows={cmp.rows} initial={rows.map((r) => r.address)} open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function Outcome({ change: c }: { change: ChangeDetail }) {
  switch (c.status) {
    case "pending_approval":
      return (
        <Notice tone="info" title="Not applied yet">
          {c.approve_blocker ?? "Applied after approval."}
        </Notice>
      );
    case "approved":
      return <Notice title="Approved" />;
    case "running":
      return <Notice title="Applying" />;
    case "verifying":
      if (c.browser?.waiting_since && !c.browser.decision) {
        return c.can_decide ? null : (
          <Notice title="Waiting for an administrator to keep it">
            Rolls back{c.deadline_at ? ` at ${absoluteTime(c.deadline_at)}` : ""} without a decision.
          </Notice>
        );
      }
      if (c.browser?.decision) {
        return (
          <Notice title={c.browser.decision === "keep" ? `Kept by ${c.browser.by}; confirming` : `Undone by ${c.browser.by}; rolling back`} />
        );
      }
      return (
        <Notice title="Verifying">
          Rolls back{c.deadline_at ? ` at ${absoluteTime(c.deadline_at)}` : ""} unless every check passes{c.confirm_in_browser ? " and it is kept" : ""}.
        </Notice>
      );
    case "succeeded":
      return <Notice title="Applied and verified">{c.outcome}</Notice>;
    case "rolled_back":
      return (
        <Notice tone="warn" title="Rolled back">
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

/** Seconds left until `until`, updated every second. */
function useSecondsLeft(until: number | null): number | null {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (until === null) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [until]);
  return until === null ? null : Math.max(0, Math.floor((until - now) / 1000));
}

/** The change is applied and checked; Lanvex reaches this server only through loopback, so an administrator
 * keeps it here. That the Keep request reaches Lanvex at all shows this browser's way to Lanvex still works. */
function BrowserDecision({ change: c, onDecided }: { change: ChangeDetail; onDecided: () => void }) {
  const [ack, setAck] = React.useState(false);
  // Lanvex takes no decision in the last 15 seconds before the server's own deadline.
  const left = useSecondsLeft(c.deadline_at ? new Date(c.deadline_at).getTime() - 15_000 : null);
  const where = c.your_address;
  const decide = useMutation({
    mutationFn: (decision: "keep" | "undo") =>
      api<ChangeDetail>(`/changes/${c.id}/decision`, { method: "POST", body: { decision, acknowledge_indirect: ack } }),
    onSuccess: (_, decision) => {
      toast.success(decision === "keep" ? "Kept. Lanvex confirms the change on the server." : "Undoing: the server restores its previous configuration.");
      onDecided();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const expired = left === 0;
  return (
    <section aria-label="Keep or undo the change" className="grid gap-3 rounded-[var(--radius-panel)] border-2 border-accent bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[15px] font-semibold">Does everything still work?</h2>
        {left !== null && (
          <span className="addr text-[13px] tabular-nums text-muted" aria-live="polite">
            {expired ? "Time is up" : `${left} s left`}
          </span>
        )}
      </div>
      <p className="text-[13px] text-muted">
        All checks on {c.server_name} passed. Keep it only if everything you need still works; otherwise it rolls back{c.deadline_at ? ` at ${absoluteTime(c.deadline_at)}` : ""}.
      </p>
      {where &&
        (where.on_server ? (
          <p className="text-[13px]">
            Connected through <span className="addr">{where.host}</span>, an address of {c.server_name}: it still works.
          </p>
        ) : (
          <Notice tone="warn" title={where.vpn ? `${where.host} is ${c.server_name}'s VPN address (${where.interface})` : `${where.host} is not one of ${c.server_name}'s addresses`}>
            <p>
              {where.vpn
                ? `This proves only the VPN works. Use the server's LAN address, or check the network yourself.`
                : `This does not prove ${c.server_name}'s network works. Use one of its addresses, or check it yourself.`}
            </p>
            <div className="pt-2">
              <Checkbox id="ack-indirect" label="I checked that the server's network still works" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            </div>
          </Notice>
        ))}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={expired || (where !== null && !where.on_server && !ack)} loading={decide.isPending && decide.variables === "keep"} onClick={() => decide.mutate("keep")}>
          <Check /> Keep the change
        </Button>
        <Button variant="secondary" disabled={expired} loading={decide.isPending && decide.variables === "undo"} onClick={() => decide.mutate("undo")}>
          <Undo2 /> Undo it now
        </Button>
      </div>
    </section>
  );
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
              <li>
                {c.confirm_in_browser
                  ? "Lanvex checks the result. If every check passes, you choose Keep on this page; only then does Lanvex confirm the change."
                  : "Lanvex signs in again and checks the result. Only if every check passes does it confirm the change."}
              </li>
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
