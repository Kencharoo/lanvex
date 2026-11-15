import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookCheck, Cable, ExternalLink, Radar } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/form";
import { Badge, EmptyState, Notice, Skeleton, type Tone } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { useNetBoxLabel } from "@/lib/system";
import type { DiscoveredHost, DiscoveredHosts, DiscoveredLink, DiscoveredLinks, WritePlan } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { ActionLine } from "./servers/record";

export function DiscoveryPage() {
  const nbName = useNetBoxLabel();
  return (
    <>
      <PageHeader
        title="Discovery"
        description={`What your servers see around them each time Lanvex reads them: hosts in their neighbour tables, and devices that announce themselves over LLDP. Compared with ${nbName}; nothing is recorded until you choose to.`}
      />
      <div className="grid gap-8">
        <HostsSection />
        <LinksSection />
      </div>
    </>
  );
}

function HostsSection() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const [onlyMissing, setOnlyMissing] = React.useState(true);
  const [picked, setPicked] = React.useState<string[]>([]);
  const [recording, setRecording] = React.useState(false);
  const q = useQuery({
    queryKey: ["discovery", "hosts", onlyMissing],
    queryFn: () => api<DiscoveredHosts>("/discovery/hosts", { query: { undocumented: onlyMissing } }),
  });
  const d = q.data;
  const selectable = (h: DiscoveredHost) => isAdmin && !h.netbox && !!d?.netbox_checked;
  const toggle = (a: string, on: boolean) => setPicked((p) => (on ? [...p, a] : p.filter((x) => x !== a)));

  const columns: Column<DiscoveredHost>[] = [
    ...(isAdmin
      ? [
          {
            key: "pick",
            header: <span className="sr-only">Pick</span>,
            headerClassName: "w-8",
            cell: (h: DiscoveredHost) =>
              selectable(h) ? (
                <input
                  type="checkbox"
                  aria-label={`Pick ${h.address}`}
                  className="size-4 rounded border-line accent-[var(--accent)]"
                  checked={picked.includes(h.address)}
                  onChange={(e) => toggle(h.address, e.target.checked)}
                />
              ) : null,
          },
        ]
      : []),
    {
      key: "address",
      header: "Address",
      cell: (h) => (
        <span className="grid">
          <span className="addr font-medium">{h.address}</span>
          {h.mac && <span className="addr text-xs text-muted">{h.mac}</span>}
        </span>
      ),
    },
    {
      key: "what",
      header: "What it is",
      cell: (h) =>
        h.server ? (
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge tone="info">Lanvex server</Badge>
            <Link to={`/servers/${h.server_id}`} className="text-[13px] hover:underline">
              {h.server}
            </Link>
          </span>
        ) : h.router ? (
          <Badge>Router</Badge>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
    {
      key: "seen",
      header: "Seen by",
      cell: (h) => (
        <span className="grid text-[13px]">
          {h.seen_by.slice(0, 2).map((s) => (
            <span key={s.server_id}>
              <Link to={`/servers/${s.server_id}`} className="hover:underline">
                {s.server_name}
              </Link>
              <span className="text-muted"> on {s.interface}</span>
            </span>
          ))}
          {h.seen_by.length > 2 && <span className="text-xs text-muted">and {h.seen_by.length - 2} more</span>}
        </span>
      ),
    },
    {
      key: "when",
      header: "Last seen",
      cell: (h) => (
        <span title={`First seen ${absoluteTime(h.first_seen)}`} className="text-[13px]">
          {relativeTime(h.last_seen)}
        </span>
      ),
    },
    {
      key: "netbox",
      header: `In ${nbName}`,
      cell: (h) =>
        h.netbox ? (
          <span className="text-[13px]">
            {h.netbox.url ? (
              <a href={h.netbox.url} target="_blank" rel="noreferrer" className="addr inline-flex items-center gap-1 text-accent hover:underline">
                {h.netbox.address} <ExternalLink className="size-3" />
              </a>
            ) : (
              <span className="addr">{h.netbox.address}</span>
            )}
            <span className="text-muted"> {h.netbox.assigned_to ? `on ${h.netbox.assigned_to}` : "unassigned"}</span>
          </span>
        ) : d?.netbox_checked ? (
          <Badge tone="warn">Not recorded</Badge>
        ) : (
          <span className="text-faint">Not checked</span>
        ),
    },
  ];

  const recordable = (d?.hosts ?? []).filter(selectable).map((h) => h.address);
  return (
    <Section
      title="Hosts seen"
      description="From the servers' neighbour tables (ARP and IPv6 neighbour discovery): hosts they exchanged packets with recently. Link-local addresses are left out."
      actions={<Checkbox id="disc-missing" label={`Only those not in ${nbName}`} checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} />}
    >
      {q.error ? (
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      ) : (
        <>
          {d && d.total > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-[13px] text-muted">
                {d.total} host{d.total === 1 ? "" : "s"} seen
                {d.netbox_checked ? `; ${d.undocumented} not recorded in ${nbName}.` : `. ${nbName} is not connected, so they were not compared.`}
              </p>
              {isAdmin && recordable.length > 0 && (
                <span className="flex items-center gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setPicked(picked.length === recordable.length ? [] : recordable)}>
                    {picked.length === recordable.length ? "Pick none" : "Pick all"}
                  </Button>
                  <Button size="sm" variant="secondary" disabled={picked.length === 0} onClick={() => setRecording(true)}>
                    <BookCheck /> Record {picked.length || ""} in {nbName}…
                  </Button>
                </span>
              )}
            </div>
          )}
          <DataTable
            dense
            loading={q.isPending}
            columns={columns}
            rows={d?.hosts}
            rowKey={(h) => h.address}
            empty={
              <EmptyState icon={Radar} title={d && d.total > 0 ? `Every host seen is recorded in ${nbName}` : "No hosts seen yet"}>
                {d && d.total > 0
                  ? "Untick the filter to list them all."
                  : "Each time Lanvex reads a server, it notes the hosts in the server's neighbour table. Add servers, or wait for their next read."}
              </EmptyState>
            }
          />
        </>
      )}
      <PlanDialog
        open={recording}
        onClose={() => setRecording(false)}
        onDone={() => setPicked([])}
        endpoint="/discovery/hosts/record"
        items={picked}
        title={`Record hosts in ${nbName}`}
        description={`Adds each host to ${nbName} as an unassigned IP address, with the prefix length of the network it was seen on and a note of who saw it. Assign them to devices there later.`}
      />
    </Section>
  );
}

const LINK: Record<DiscoveredLink["status"], { tone: Tone; label: string }> = {
  documented: { tone: "ok", label: "Documented" },
  different: { tone: "danger", label: "Different cable" },
  undocumented: { tone: "warn", label: "No cable recorded" },
  several: { tone: "neutral", label: "Shared segment" },
  not_linked: { tone: "neutral", label: "Server not linked" },
  virtual: { tone: "neutral", label: "Virtual machine" },
};

function LinksSection() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const [recordKey, setRecordKey] = React.useState<string | null>(null);
  const q = useQuery({ queryKey: ["discovery", "links"], queryFn: () => api<DiscoveredLinks>("/discovery/links") });
  const d = q.data;
  const columns: Column<DiscoveredLink>[] = [
    {
      key: "local",
      header: "Server",
      cell: (l) => (
        <span className="text-[13px]">
          <Link to={`/servers/${l.server_id}`} className="font-medium hover:underline">
            {l.server_name}
          </Link>
          <span className="text-muted"> / {l.interface}</span>
        </span>
      ),
    },
    {
      key: "neighbor",
      header: "Announces itself as",
      cell: (l) => (
        <span className="grid text-[13px]">
          <span>
            <span className="font-medium">{l.neighbor.system_name ?? l.neighbor.chassis_id ?? "?"}</span>
            <span className="text-muted"> / {l.neighbor.port_description ?? l.neighbor.port_id}</span>
          </span>
          <span className="text-xs text-muted">
            {[l.neighbor.capabilities.join(", "), l.neighbor.management_addresses.join(", "), `via ${l.neighbor.source === "lldpd" ? "lldpd" : "systemd-networkd"}`]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </span>
      ),
    },
    {
      key: "status",
      header: "Result",
      cell: (l) => (
        <span className="grid max-w-md gap-1">
          <span>
            <Badge tone={LINK[l.status].tone}>{LINK[l.status].label}</Badge>
          </span>
          {l.status === "different" && l.documented && <span className="text-xs text-muted">{nbName}: {l.documented}</span>}
          {l.problem && <span className="text-xs text-muted">{l.problem}</span>}
        </span>
      ),
    },
    {
      key: "action",
      header: <span className="sr-only">Actions</span>,
      cell: (l) =>
        isAdmin && l.status === "undocumented" && !l.problem ? (
          <Button size="sm" variant="secondary" onClick={() => setRecordKey(l.key)}>
            <Cable /> Record cable…
          </Button>
        ) : null,
    },
  ];
  return (
    <Section
      title="Connections seen over LLDP"
      description={`What is on the other end of each server interface, as that device announces it, compared with the cables ${nbName} documents. Through patch panels, the far end counts.`}
    >
      {q.error ? (
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      ) : (
        <>
          <DataTable
            dense
            loading={q.isPending}
            columns={columns}
            rows={d?.links}
            rowKey={(l) => l.key}
            empty={
              <EmptyState icon={Cable} title="No LLDP neighbours seen">
                Servers report LLDP neighbours when lldpd or systemd-networkd receives them, and the switch sends LLDP.
              </EmptyState>
            }
          />
          {d && d.servers_without_lldp.length > 0 && (
            <p className="text-xs text-muted">
              No LLDP neighbours from {d.servers_without_lldp.join(", ")}. Their server pages say why when Lanvex knows (for example, lldpd runs but Lanvex may not ask it).
            </p>
          )}
        </>
      )}
      <PlanDialog
        open={recordKey !== null}
        onClose={() => setRecordKey(null)}
        endpoint="/discovery/links/record"
        items={recordKey ? [recordKey] : []}
        title={`Record a cable in ${nbName}`}
        description={`Adds a connected cable between the two interfaces in ${nbName}, as seen over LLDP. The topology map shows it from then on.`}
      />
    </Section>
  );
}

/** Preview the NetBox writes for `items`, then make exactly those. */
function PlanDialog({
  open,
  onClose,
  onDone,
  endpoint,
  items,
  title,
  description,
}: {
  open: boolean;
  onClose: () => void;
  onDone?: () => void;
  endpoint: string;
  items: string[];
  title: string;
  description: string;
}) {
  const nbName = useNetBoxLabel();
  const qc = useQueryClient();
  const preview = useQuery({
    queryKey: ["discovery", "plan", endpoint, items],
    queryFn: () => api<WritePlan>(endpoint, { method: "POST", body: { items } }),
    enabled: open && items.length > 0,
    staleTime: 0,
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: (plan: WritePlan) => api<WritePlan>(endpoint, { method: "POST", body: { items, token: plan.token } }),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["discovery"], predicate: (q) => q.queryKey[1] !== "plan" });
      void qc.invalidateQueries({ queryKey: ["topology"] });
      if (!r.failed) {
        toast.success(`Recorded in ${nbName}`, { description: `${r.done.length} update${r.done.length === 1 ? "" : "s"}.` });
        onDone?.();
        onClose();
      }
    },
    onError: () => void preview.refetch(),
  });
  React.useEffect(() => {
    if (open) apply.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const plan = preview.data;
  const doable = plan?.actions.filter((a) => !a.problem).length ?? 0;
  const result = apply.data;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={title} description={description} width="max-w-xl">
        <DialogBody>
          {preview.isPending ? (
            <Skeleton className="h-16 w-full" />
          ) : preview.error ? (
            <Notice tone="danger">{errorMessage(preview.error)}</Notice>
          ) : result ? (
            <>
              {result.failed && (
                <Notice tone="danger" title={`${nbName} refused an update`}>
                  {result.error} The updates before it were made; the ones after it were not.
                </Notice>
              )}
              <ul className="grid gap-2">
                {result.done.map((a, i) => (
                  <ActionLine key={i} a={a} state="done" />
                ))}
                {result.failed && <ActionLine a={result.failed} state="failed" />}
              </ul>
            </>
          ) : plan ? (
            <>
              <p className="text-[13px] font-medium">What changes in {nbName}</p>
              <ul className="grid gap-2">
                {plan.actions.map((a, i) => (
                  <ActionLine key={i} a={a} />
                ))}
              </ul>
              {apply.error && <Notice tone="warn">{errorMessage(apply.error)}</Notice>}
            </>
          ) : null}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            {result ? "Close" : "Cancel"}
          </Button>
          {!result && (
            <Button variant="primary" disabled={!plan || doable === 0 || preview.isFetching} loading={apply.isPending} onClick={() => plan && apply.mutate(plan)}>
              {doable > 0 ? `Make ${doable} update${doable === 1 ? "" : "s"}` : "Nothing to record"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
