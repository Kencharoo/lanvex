import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { useNetBoxLabel } from "@/lib/system";
import type { ComparisonRow, HostFacts, RecordAction, RecordPlan, Server } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A comparison row that differs, so that recording it means something. */
export const differs = (r: ComparisonRow) => r.status !== "match";

/** How the server's change form starts when a change is prepared from a comparison row. */
export interface ChangeSeed {
  iface: string;
  addresses: string[];
  note: string;
}

/**
 * The server change that makes the server match the documentation for one row, or why there is none.
 * Changes cover static IPv4 addresses on one interface, so that is what this can prepare.
 */
export function changeSeedFor(r: ComparisonRow, facts: HostFacts, nbName: string): ChangeSeed | string {
  if (r.address.includes(":")) return "Lanvex changes IPv4 settings only.";
  const name = r.status === "only_in_netbox" ? r.netbox_interface : r.server_interface;
  if (r.status === "interface_differs") return `Move the address between interfaces on the server by hand, or record the server's interface in ${nbName}.`;
  if (!name) return `${nbName} does not say which interface the address belongs to.`;
  const iface = facts.interfaces.find((i) => i.name === name);
  if (!iface) return `The server has no interface ${name}.`;
  if (iface.kind === "loopback" || iface.master) return `Lanvex cannot change ${name}: it is ${iface.master ? `part of ${iface.master}` : "the loopback interface"}.`;
  const v4 = iface.addresses.filter((a) => a.family === 4);
  if (v4.some((a) => a.dynamic)) return `${name} gets its address from DHCP.`;
  const statics = v4.map((a) => a.address);
  if (r.status === "only_in_netbox" && r.netbox_address) {
    return { iface: name, addresses: [...statics, r.netbox_address], note: `Adds ${r.netbox_address}, as documented in ${nbName}.` };
  }
  if (r.status === "only_on_server" && r.server_address) {
    const rest = statics.filter((a) => a !== r.server_address);
    return { iface: name, addresses: rest.length ? rest : [""], note: `Removes ${r.server_address}, which ${nbName} does not document for this server.` };
  }
  if (r.status === "mask_differs" && r.server_address && r.netbox_address) {
    return {
      iface: name,
      addresses: statics.map((a) => (a === r.server_address ? r.netbox_address! : a)),
      note: `Changes ${r.server_address} to ${r.netbox_address}, as documented in ${nbName}.`,
    };
  }
  return "Nothing to change.";
}

export function ActionLine({ a, state }: { a: RecordAction; state?: "done" | "failed" | "skipped" }) {
  const blocked = !!a.problem;
  return (
    <li className="flex items-start gap-2 text-[13px]">
      {blocked || state === "failed" ? (
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-label="Not possible" />
      ) : (
        <Check className={cn("mt-0.5 size-4 shrink-0", state === "done" ? "text-ok" : "text-muted")} aria-label={state === "done" ? "Done" : "Will be done"} />
      )}
      <span className="grid gap-0.5">
        <span className={cn(blocked && "text-muted line-through decoration-faint")}>{a.text}</span>
        {a.problem && <span className="text-xs text-warn">{a.problem}</span>}
      </span>
    </li>
  );
}

export function RecordDialog({
  server: s,
  rows,
  initial,
  open,
  onClose,
}: {
  server: Server;
  rows: ComparisonRow[];
  initial: string[];
  open: boolean;
  onClose: () => void;
}) {
  const nbName = useNetBoxLabel();
  const qc = useQueryClient();
  const candidates = rows.filter(differs);
  const [picked, setPicked] = React.useState<string[]>(initial);
  React.useEffect(() => {
    if (open) setPicked(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const addresses = candidates.map((r) => r.address).filter((a) => picked.includes(a));

  const preview = useQuery({
    queryKey: ["servers", s.id, "record", addresses],
    queryFn: () => api<RecordPlan>(`/servers/${s.id}/record`, { method: "POST", body: { addresses } }),
    enabled: open && addresses.length > 0,
    staleTime: 0,
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: (plan: RecordPlan) => api<RecordPlan>(`/servers/${s.id}/record`, { method: "POST", body: { addresses, token: plan.token } }),
    onSuccess: (result) => {
      void qc.invalidateQueries({ queryKey: ["servers", s.id, "comparison"] });
      if (!result.failed) {
        toast.success(`Recorded in ${nbName}`, { description: `${result.done.length} update${result.done.length === 1 ? "" : "s"}. Nothing on the server was changed.` });
        onClose();
      }
    },
    onError: () => void preview.refetch(),
  });
  React.useEffect(() => {
    if (open) apply.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, addresses.join(",")]);

  const plan = preview.data;
  const doable = plan?.actions.filter((a) => !a.problem).length ?? 0;
  const result = apply.data;

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`Record in ${nbName}`}
        description={`Update ${nbName} so it documents what ${s.name} had when it was last read. This changes documentation only: nothing on the server is changed.`}
        width="max-w-xl"
      >
        <DialogBody>
          {candidates.length > 1 && (
            <fieldset className="grid gap-1.5">
              <legend className="mb-1.5 text-[13px] font-medium">Addresses</legend>
              {candidates.map((r) => (
                <label key={r.address} className="flex items-center gap-2 text-[13px]">
                  <input
                    type="checkbox"
                    className="size-4 rounded border-line accent-[var(--accent)]"
                    checked={picked.includes(r.address)}
                    disabled={apply.isPending}
                    onChange={(e) => setPicked((p) => (e.target.checked ? [...p, r.address] : p.filter((x) => x !== r.address)))}
                  />
                  <span className="addr">{r.address}</span>
                  <span className="text-xs text-muted">
                    {r.status === "only_on_server" ? "on the server only" : r.status === "only_in_netbox" ? `in ${nbName} only` : r.status === "mask_differs" ? "prefix length differs" : "interface differs"}
                  </span>
                </label>
              ))}
            </fieldset>
          )}

          {addresses.length === 0 ? (
            <p className="text-[13px] text-muted">Pick at least one address.</p>
          ) : preview.isPending ? (
            <Skeleton className="h-20 w-full" />
          ) : preview.error ? (
            <Notice tone="danger">{errorMessage(preview.error)}</Notice>
          ) : result ? (
            <div className="grid gap-2">
              {result.failed && (
                <Notice tone="danger" title={`${nbName} refused an update`}>
                  {result.error} The updates before it were made; the ones after it were not.
                </Notice>
              )}
              <ul className="grid gap-2">
                {result.done.map((a, i) => (
                  <ActionLine key={`d${i}`} a={a} state="done" />
                ))}
                {result.failed && <ActionLine a={result.failed} state="failed" />}
              </ul>
            </div>
          ) : plan ? (
            <div className="grid gap-2">
              <p className="text-[13px] font-medium">{plan.actions.length ? `What changes in ${nbName}` : "Nothing to record"}</p>
              <ul className="grid gap-2">
                {plan.actions.map((a, i) => (
                  <ActionLine key={i} a={a} />
                ))}
              </ul>
              {apply.error && <Notice tone="warn">{errorMessage(apply.error)}</Notice>}
            </div>
          ) : null}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            {result ? "Close" : "Cancel"}
          </Button>
          {!result && (
            <Button variant="primary" disabled={!plan || doable === 0 || preview.isFetching} loading={apply.isPending} onClick={() => plan && apply.mutate(plan)}>
              {doable > 0 ? `Make ${doable} update${doable === 1 ? "" : "s"}` : "Record"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
