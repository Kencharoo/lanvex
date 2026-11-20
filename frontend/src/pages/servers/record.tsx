import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { inNetworkOf, parseCidr } from "@/lib/cidr";
import { useNetBoxLabel } from "@/lib/system";
import type { ComparisonRow, HostFacts, RecordAction, RecordPlan, Server, SettingRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A comparison row that differs, so that recording it means something. */
export const differs = (r: { status: string }) => r.status !== "match";

/** How the server's change form starts when a change is prepared from a comparison row: the interface,
 * and what to set on it (the rest starts as the server has it). */
export interface ChangeSeed {
  iface: string;
  addresses?: string[];
  gateway?: string;
  dns?: { servers: string; search: string };
  routes?: { to: string; via: string; metric: string }[];
  note: string;
}

export const settingLabel = (r: SettingRow) =>
  r.kind === "gateway" ? `Default gateway (${r.key})` : r.kind === "dns_servers" ? "DNS servers" : r.kind === "dns_search" ? "Search domains" : `Route to ${r.key}`;

const isDefault = (dst: string) => dst === "0.0.0.0/0" || dst === "::/0" || dst === "default";
const network = (dst: string) => {
  const p = parseCidr(dst);
  return p.ok ? p.value.canonical : (p.suggestion ?? dst);
};

/** Why Lanvex cannot change an interface of the server, or null. */
function unchangeable(name: string | null | undefined, facts: HostFacts): string | null {
  if (!name) return "The server does not say which interface this is on.";
  const iface = facts.interfaces.find((i) => i.name === name);
  if (!iface) return `The server has no interface ${name}.`;
  if (iface.kind === "loopback" || iface.master) return `Lanvex cannot change ${name}: it is ${iface.master ? `part of ${iface.master}` : "the loopback interface"}.`;
  return null;
}

/**
 * The server change that makes a gateway, the DNS settings or a static route match the documentation,
 * or why there is none. `routesDocumented`: the documentation lists the server's routes, so a route
 * missing from it is one to remove.
 */
export function settingSeedFor(r: SettingRow, facts: HostFacts, nbName: string, routesDocumented: boolean): ChangeSeed | string {
  if (r.kind === "gateway") {
    if (!r.documented) return `${nbName} documents no gateway for ${r.prefix}.`;
    if (r.key !== "IPv4") return "Change the IPv6 gateway with Change IP settings.";
    const why = unchangeable(r.interface, facts);
    if (why) return why;
    const iface = facts.interfaces.find((i) => i.name === r.interface)!;
    if (iface.addresses.some((a) => a.family === 4 && a.dynamic)) return `${iface.name} gets its address and gateway from DHCP.`;
    return { iface: iface.name, gateway: r.documented, note: `Sets the default gateway to ${r.documented}, as documented for ${r.prefix} in ${nbName}.` };
  }
  if (r.kind === "dns_servers" || r.kind === "dns_search") {
    if (r.status === "only_on_server") return `${nbName} documents no ${r.kind === "dns_servers" ? "DNS servers" : "search domains"} for this server.`;
    const scopes = facts.dns.resolved ?? [];
    const name =
      scopes.find((x) => x.servers.length && !unchangeable(x.scope, facts))?.scope ??
      facts.routes.find((rt) => rt.family === 4 && isDefault(rt.destination))?.interface ??
      null;
    const why = unchangeable(name, facts);
    if (why) return why;
    const scope = scopes.find((x) => x.scope === name);
    const servers = r.kind === "dns_servers" ? (r.documented ?? "") : (scope?.servers ?? []).filter((x) => !x.includes(":")).join(", ");
    const search = r.kind === "dns_search" ? (r.documented ?? "") : (scope?.domains ?? []).filter((d) => d !== "~.").join(", ");
    return { iface: name!, dns: { servers, search }, note: `Sets the ${r.kind === "dns_servers" ? "DNS servers" : "search domains"} of ${name} as documented in ${nbName}.` };
  }
  if (r.status === "only_on_server" && !routesDocumented) return `${nbName} documents no routes for this server.`;
  const via = r.documented ?? "";
  const name = r.interface ?? facts.interfaces.find((i) => i.addresses.some((a) => a.scope !== "link" && inNetworkOf(a.address, via)))?.name;
  if (!name) return `No interface of the server is in the network of ${via}.`;
  const why = unchangeable(name, facts);
  if (why) return why;
  const current = facts.routes
    .filter((rt) => rt.interface === name && rt.gateway && !isDefault(rt.destination) && (rt.protocol === "static" || rt.protocol === "boot"))
    .map((rt) => ({ to: rt.destination, via: rt.gateway ?? "", metric: rt.metric == null ? "" : String(rt.metric) }));
  const old = current.find((rt) => network(rt.to) === r.key);
  const routes = current.filter((rt) => rt !== old);
  if (r.documented) routes.push({ to: r.key, via: r.documented, metric: old?.metric ?? "" });
  const note =
    r.status === "only_on_server"
      ? `Removes the route to ${r.key}, which ${nbName} does not document.`
      : r.status === "only_documented"
        ? `Adds the route to ${r.key} via ${via}, as documented in ${nbName}.`
        : `Routes ${r.key} via ${via}, as documented in ${nbName}.`;
  return { iface: name, routes, note };
}

/**
 * The server change that makes the server match the documentation for one row, or why there is none.
 * It prepares static IPv4 addresses on one interface; IPv6 is set in the change form itself.
 */
export function changeSeedFor(r: ComparisonRow, facts: HostFacts, nbName: string): ChangeSeed | string {
  if (r.address.includes(":")) return "Change IPv6 addresses with Change IP settings.";
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

const SETTING_STATUS: Record<SettingRow["status"], string> = {
  match: "matches",
  differs: "differs",
  only_on_server: "not documented",
  only_documented: "not on the server",
};

export function RecordDialog({
  server: s,
  rows,
  settings = [],
  initial,
  open,
  onClose,
}: {
  server: Server;
  rows: ComparisonRow[];
  settings?: SettingRow[];
  initial: { addresses: string[]; settings: string[] };
  open: boolean;
  onClose: () => void;
}) {
  const nbName = useNetBoxLabel();
  const qc = useQueryClient();
  const candidates = rows.filter(differs);
  const settingCandidates = settings.filter(differs);
  const [picked, setPicked] = React.useState<string[]>([]);
  const [pickedSettings, setPickedSettings] = React.useState<string[]>([]);
  React.useEffect(() => {
    if (open) {
      setPicked(initial.addresses);
      setPickedSettings(initial.settings);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const addresses = candidates.map((r) => r.address).filter((a) => picked.includes(a));
  const settingIds = settingCandidates.map((r) => r.id).filter((x) => pickedSettings.includes(x));
  const picks = { addresses, settings: settingIds };
  const nothing = addresses.length === 0 && settingIds.length === 0;

  const preview = useQuery({
    queryKey: ["servers", s.id, "record", addresses, settingIds],
    queryFn: () => api<RecordPlan>(`/servers/${s.id}/record`, { method: "POST", body: picks }),
    enabled: open && !nothing,
    staleTime: 0,
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: (plan: RecordPlan) => api<RecordPlan>(`/servers/${s.id}/record`, { method: "POST", body: { ...picks, token: plan.token } }),
    onSuccess: (result) => {
      void qc.invalidateQueries({ queryKey: ["servers", s.id, "comparison"] });
      if (!result.failed) {
        toast.success(`Recorded in ${nbName}`, { description: `${result.done.length} update${result.done.length === 1 ? "" : "s"}` });
        onClose();
      }
    },
    onError: () => void preview.refetch(),
  });
  React.useEffect(() => {
    if (open) apply.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, addresses.join(","), settingIds.join(",")]);

  const plan = preview.data;
  const doable = plan?.actions.filter((a) => !a.problem).length ?? 0;
  const result = apply.data;
  const toggle = (set: React.Dispatch<React.SetStateAction<string[]>>, key: string, on: boolean) => set((p) => (on ? [...p, key] : p.filter((x) => x !== key)));
  const box = "size-4 rounded border-line accent-[var(--accent)]";

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`Record in ${nbName}`}
        description={`From the last read of ${s.name}`}
        width="max-w-xl"
      >
        <DialogBody>
          {candidates.length + settingCandidates.length > 1 && (
            <div className="grid gap-3">
              {candidates.length > 0 && (
                <fieldset className="grid gap-1.5">
                  <legend className="mb-1.5 text-[13px] font-medium">Addresses</legend>
                  {candidates.map((r) => (
                    <label key={r.address} className="flex items-center gap-2 text-[13px]">
                      <input type="checkbox" className={box} checked={picked.includes(r.address)} disabled={apply.isPending} onChange={(e) => toggle(setPicked, r.address, e.target.checked)} />
                      <span className="addr">{r.address}</span>
                      <span className="text-xs text-muted">
                        {r.status === "only_on_server" ? "on the server only" : r.status === "only_in_netbox" ? `in ${nbName} only` : r.status === "mask_differs" ? "prefix length differs" : "interface differs"}
                      </span>
                    </label>
                  ))}
                </fieldset>
              )}
              {settingCandidates.length > 0 && (
                <fieldset className="grid gap-1.5">
                  <legend className="mb-1.5 text-[13px] font-medium">Gateway, DNS and routes</legend>
                  {settingCandidates.map((r) => (
                    <label key={r.id} className="flex items-center gap-2 text-[13px]">
                      <input type="checkbox" className={box} checked={pickedSettings.includes(r.id)} disabled={apply.isPending} onChange={(e) => toggle(setPickedSettings, r.id, e.target.checked)} />
                      <span>{settingLabel(r)}</span>
                      <span className="text-xs text-muted">{SETTING_STATUS[r.status]}</span>
                    </label>
                  ))}
                </fieldset>
              )}
            </div>
          )}

          {nothing ? (
            <p className="text-[13px] text-muted">Pick at least one difference.</p>
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
