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
import type { DiscoveredHost, DiscoveredHosts, DiscoveredLink, DiscoveredLinks, PortFinding, PortFindings, WritePlan } from "@/lib/types";
import { absoluteTime, relativeTime } from "@/lib/utils";
import { ActionLine } from "./servers/record";
import { EndpointLine, PortStatusBadge } from "./switches/shared";

export function DiscoveryPage() {
  return (
    <>
      <PageHeader
        title="Discovery"
      />
      <div className="grid gap-8">
        <HostsSection />
        <SwitchPortsSection />
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
      key: "port",
      header: "Switch port",
      cell: (h) =>
        h.switch_port ? (
          <span className="grid text-[13px]">
            <span>
              <Link to={`/switches/${h.switch_port.switch_id}`} className="hover:underline">
                {h.switch_port.switch_name}
              </Link>
              <span className="text-muted"> / {h.switch_port.port}</span>
            </span>
            <span className="text-xs text-muted">
              {[h.switch_port.vlan != null ? `VLAN ${h.switch_port.vlan}` : null, h.switch_port.current ? null : `seen ${relativeTime(h.switch_port.last_seen)}`].filter(Boolean).join(" · ")}
            </span>
          </span>
        ) : (
          <span className="text-faint">—</span>
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
      description="ARP and IPv6 neighbour tables of your servers"
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
        description="Added as unassigned IP addresses."
      />
    </Section>
  );
}

function SwitchPortsSection() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const [onlyOpen, setOnlyOpen] = React.useState(true);
  const [recordKey, setRecordKey] = React.useState<string | null>(null);
  const q = useQuery({ queryKey: ["discovery", "ports"], queryFn: () => api<PortFindings>("/discovery/ports") });
  const d = q.data;
  if (d && d.switches_read === 0) return null;
  const rows = (d?.ports ?? []).filter((p) => !onlyOpen || !["documented", "not_linked", "not_checked"].includes(p.status));
  const unlinked = [...new Set((d?.ports ?? []).filter((p) => p.status === "not_linked").map((p) => p.switch_name))];
  const compared = (d?.ports ?? []).some((p) => !["not_linked", "not_checked"].includes(p.status));
  const columns: Column<PortFinding>[] = [
    {
      key: "port",
      header: "Switch port",
      cell: (p) => (
        <span className="grid text-[13px]">
          <span>
            <Link to={`/switches/${p.switch_id}`} className="font-medium hover:underline">
              {p.switch_name}
            </Link>
            <span className="text-muted"> / {p.port}</span>
          </span>
          {(p.alias || p.uplink) && <span className="text-xs text-muted">{[p.alias, p.uplink ? "uplink" : null].filter(Boolean).join(" · ")}</span>}
        </span>
      ),
    },
    {
      key: "seen",
      header: "Connected",
      cell: (p) => (
        <span className="grid gap-1">
          {p.endpoints.slice(0, 3).map((e, i) => (
            <EndpointLine key={`${e.via}-${e.mac ?? e.name}-${i}`} e={e} />
          ))}
          {p.endpoints.length > 3 && <span className="text-xs text-muted">and {p.endpoints.length - 3} more</span>}
        </span>
      ),
    },
    {
      key: "status",
      header: "Result",
      cell: (p) => (
        <span className="grid max-w-md gap-1">
          <span>
            <PortStatusBadge status={p.status} />
          </span>
          {p.documented && p.status !== "documented" && <span className="text-xs text-muted">{nbName}: {p.documented}</span>}
          {p.problem && <span className="text-xs text-muted">{p.problem}</span>}
        </span>
      ),
    },
    {
      key: "action",
      header: <span className="sr-only">Actions</span>,
      cell: (p) =>
        isAdmin && p.status === "undocumented" && !p.problem ? (
          <Button size="sm" variant="secondary" onClick={() => setRecordKey(p.key)}>
            <Cable /> Record cable…
          </Button>
        ) : null,
    },
  ];
  return (
    <Section
      title="Switch ports"
      description={`MAC address tables and LLDP of your switches, compared with the cables in ${nbName}`}
      actions={<Checkbox id="disc-ports-open" label={`Only those that differ from ${nbName}`} checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} />}
    >
      {q.error ? (
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      ) : (
        <>
          {unlinked.length > 0 && (
            <p className="text-[13px] text-muted">
              Not compared: {unlinked.join(", ")} {unlinked.length === 1 ? "is" : "are"} not linked to {unlinked.length === 1 ? "its device" : "their devices"} in {nbName}.
            </p>
          )}
          <DataTable
            dense
            loading={q.isPending}
            columns={columns}
            rows={rows}
            rowKey={(p) => p.key}
            empty={
              <EmptyState
                icon={Cable}
                title={!d?.ports.length ? "Nothing seen on switch ports yet" : compared ? `Every port compared matches ${nbName}` : "No port compared yet"}
              >
                {d && d.ports.length ? "Untick the filter to list them all." : "Add switches, or wait for their next read."}
              </EmptyState>
            }
          />
        </>
      )}
      <PlanDialog
        open={recordKey !== null}
        onClose={() => setRecordKey(null)}
        endpoint="/discovery/ports/record"
        items={recordKey ? [recordKey] : []}
        title={`Record a cable in ${nbName}`}
        description="Added as a connected cable."
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
      description={`Compared with the cables in ${nbName}`}
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
                Requires lldpd or systemd-networkd on the server and LLDP on the switch.
              </EmptyState>
            }
          />
          {d && d.servers_without_lldp.length > 0 && (
            <p className="text-xs text-muted">
              No LLDP data from {d.servers_without_lldp.join(", ")}.
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
        description="Added as a connected cable."
      />
    </Section>
  );
}

/** Preview the NetBox writes for `items`, then make exactly those. */
export function PlanDialog({
  open,
  onClose,
  onDone,
  endpoint,
  items,
  title,
  description,
  refresh,
}: {
  open: boolean;
  onClose: () => void;
  onDone?: () => void;
  endpoint: string;
  items: string[];
  title: string;
  description: string;
  /** Another query to refresh once recorded. */
  refresh?: unknown[];
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
      if (refresh) void qc.invalidateQueries({ queryKey: refresh });
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
