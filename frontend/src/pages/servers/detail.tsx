import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, KeyRound, MoreHorizontal, Pencil, RefreshCw, ShieldAlert, Trash2, Unlink } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { CodeBlock } from "@/components/copy";
import { DataTable, type Column } from "@/components/data-table";
import { Facts, PageHeader, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Badge, EmptyState, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Notice, Skeleton, type Tone } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { Comparison, ComparisonRow, HostFacts, HostInterface, HostKeyScan, HostRoute, NetBoxRef, Server, Snapshot, SnapshotMeta } from "@/lib/types";
import { absoluteTime, cn, relativeTime } from "@/lib/utils";
import {
  ConnectionBadge,
  CredentialsForm,
  EMPTY_CREDENTIALS,
  HostKeyVerification,
  InstallKeyInstructions,
  credentialPayload,
  credentialsComplete,
  type CredentialDraft,
} from "./shared";

const AUTH_LABEL = { generated_key: "Key created by Lanvex", private_key: "Uploaded private key", password: "Password" } as const;

export function ServerDetailPage() {
  const { id = "" } = useParams();
  const { isAdmin } = useSession();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [dialog, setDialog] = React.useState<null | "edit" | "credentials" | "hostkey" | "delete">(null);

  const server = useQuery({ queryKey: ["servers", id], queryFn: () => api<Server>(`/servers/${id}`) });
  const latest = useQuery({ queryKey: ["servers", id, "latest"], queryFn: () => api<Snapshot | null>(`/servers/${id}/snapshots/latest`) });
  const history = useQuery({ queryKey: ["servers", id, "history"], queryFn: () => api<SnapshotMeta[]>(`/servers/${id}/snapshots`) });
  const comparison = useQuery({
    queryKey: ["servers", id, "comparison"],
    queryFn: () => api<Comparison>(`/servers/${id}/comparison`),
    enabled: !!latest.data,
  });

  const refreshAll = () => void qc.invalidateQueries({ queryKey: ["servers"] });

  const collect = useMutation({
    mutationFn: () => api<Snapshot>(`/servers/${id}/collect`, { method: "POST" }),
    onSuccess: (snap) => {
      if (snap.status === "ok") toast.success("Read the server's network state", { description: snap.message ?? `Took ${snap.duration_ms} ms. Nothing on the server was changed.` });
      else toast.error(snap.message ?? "Reading the server failed.");
      refreshAll();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const test = useMutation({
    mutationFn: () => api<{ status: string; message: string }>(`/servers/${id}/test`, { method: "POST" }),
    onSuccess: (r) => {
      (r.status === "ok" ? toast.success : toast.error)(r.message);
      refreshAll();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const s = server.data;
  const facts = latest.data?.facts ?? null;

  if (server.error) {
    return (
      <>
        <PageHeader crumbs={[{ label: "Servers", to: "/servers" }, { label: "Not found" }]} title="Server" />
        <Notice tone="danger">{errorMessage(server.error)}</Notice>
      </>
    );
  }

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Servers", to: "/servers" }, { label: s?.name ?? "…" }]}
        title={s ? s.name : <Skeleton className="h-6 w-48" />}
        description={
          s && (
            <span className="flex flex-wrap items-center gap-2">
              <span className="addr">
                {s.username}@{s.address}
                {s.port !== 22 ? `:${s.port}` : ""}
              </span>
              <ConnectionBadge status={s.last_status} />
              {s.description && <span>{s.description}</span>}
            </span>
          )
        }
        actions={
          s &&
          isAdmin && (
            <>
              <Button variant="primary" onClick={() => collect.mutate()} loading={collect.isPending}>
                <RefreshCw /> Read now
              </Button>
              <Menu>
                <MenuTrigger asChild>
                  <Button size="icon" variant="secondary" aria-label="Server actions">
                    <MoreHorizontal />
                  </Button>
                </MenuTrigger>
                <MenuContent>
                  <MenuItem onSelect={() => test.mutate()}>Test connection</MenuItem>
                  <MenuItem onSelect={() => setDialog("edit")}>
                    <Pencil /> Edit details
                  </MenuItem>
                  <MenuItem onSelect={() => setDialog("credentials")}>
                    <KeyRound /> Replace credentials
                  </MenuItem>
                  <MenuItem onSelect={() => setDialog("hostkey")}>
                    <ShieldAlert /> Verify a new host key
                  </MenuItem>
                  <MenuSeparator />
                  <MenuItem danger onSelect={() => setDialog("delete")}>
                    <Trash2 /> Remove server
                  </MenuItem>
                </MenuContent>
              </Menu>
            </>
          )
        }
      />

      {s && <StatusNotice server={s} onRepin={() => setDialog("hostkey")} hasFacts={!!facts} />}

      {latest.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : !facts ? (
        <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
          <EmptyState
            icon={RefreshCw}
            title="No network state read yet"
            action={isAdmin && <Button variant="primary" onClick={() => collect.mutate()} loading={collect.isPending}>Read now</Button>}
          >
            Lanvex signs in over SSH and runs a fixed set of read-only commands. Nothing on the server is changed.
          </EmptyState>
        </div>
      ) : (
        <div className="grid gap-8">
          <p className="-mt-2 text-xs text-muted">
            {s?.last_status && s.last_status !== "ok" ? "Last successful read" : "Live state read"} over SSH {relativeTime(latest.data!.taken_at)} ({absoluteTime(latest.data!.taken_at)}). Everything below is what
            the server reported, not what NetBox documents.
          </p>
          <SystemSummary facts={facts} />
          {facts.warnings.length > 0 && (
            <Notice tone="warn" title={`${facts.warnings.length} thing${facts.warnings.length === 1 ? "" : "s"} to check`}>
              <ul className="list-disc pl-4">
                {facts.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Notice>
          )}
          <InterfacesSection facts={facts} />
          <div className="grid gap-8 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <RoutesSection routes={facts.routes} />
            <DnsSection facts={facts} />
          </div>
          {s && <ComparisonSection server={s} q={comparison} isAdmin={isAdmin} onLinked={refreshAll} />}
        </div>
      )}

      {s && (
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <AccessSection server={s} />
          <HistorySection rows={history.data} loading={history.isPending} />
        </div>
      )}

      {s && <EditDialog server={s} open={dialog === "edit"} onClose={() => setDialog(null)} onSaved={refreshAll} />}
      {s && <CredentialsDialog server={s} open={dialog === "credentials"} onClose={() => setDialog(null)} onSaved={refreshAll} />}
      {s && <HostKeyDialog server={s} open={dialog === "hostkey"} onClose={() => setDialog(null)} onSaved={refreshAll} />}
      {s && (
        <DeleteDialog
          server={s}
          open={dialog === "delete"}
          onClose={() => setDialog(null)}
          onDeleted={() => {
            void qc.invalidateQueries({ queryKey: ["servers"] });
            navigate("/servers");
          }}
        />
      )}
    </>
  );
}

function StatusNotice({ server: s, onRepin, hasFacts }: { server: Server; onRepin: () => void; hasFacts: boolean }) {
  const { isAdmin } = useSession();
  if (s.last_status === "host_key_mismatch") {
    return (
      <Notice
        tone="danger"
        title="The server's host key changed"
        className="mb-6"
        action={isAdmin && <Button size="sm" onClick={onRepin}>Verify new key</Button>}
      >
        Lanvex refused to connect. This is expected after reinstalling the server; otherwise treat it as a possible interception and check before trusting the new key.
      </Notice>
    );
  }
  if (s.last_status === "auth_failed" && s.authorized_keys_line && !hasFacts) {
    return (
      <div className="mb-6 grid gap-3 rounded-[var(--radius-panel)] border border-warn/30 bg-warn-soft p-4">
        <p className="text-[13px] font-medium">The server does not accept Lanvex's key yet</p>
        <InstallKeyInstructions username={s.username} line={s.authorized_keys_line} />
      </div>
    );
  }
  if (s.last_status && s.last_status !== "ok") {
    return (
      <Notice tone="danger" title="Last attempt failed" className="mb-6">
        {s.last_status_message} {s.last_checked_at && <span className="text-faint">({relativeTime(s.last_checked_at)})</span>}
      </Notice>
    );
  }
  return null;
}

function SystemSummary({ facts }: { facts: HostFacts }) {
  const os = facts.system.os;
  const items: { label: string; value: React.ReactNode; sub?: React.ReactNode }[] = [
    {
      label: "Operating system",
      value: os.pretty_name ?? "Unknown",
      sub: os.supported ? <span className="text-ok">Supported</span> : <span className="text-warn">{os.support_note}</span>,
    },
    { label: "Network stack", value: facts.stack.primary, sub: <span title={facts.stack.evidence.join("\n")}>{Object.entries(facts.stack.units).filter(([, v]) => v === "active").map(([k]) => k).join(", ") || "No managing service active"}</span> },
    { label: "Hostname", value: <span className="addr">{facts.system.hostname ?? "—"}</span> },
    { label: "Kernel", value: <span className="addr">{facts.system.kernel ?? "—"}</span>, sub: facts.system.iproute2_version && `iproute2 ${facts.system.iproute2_version}` },
  ];
  return (
    <div className="grid grid-cols-1 overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface sm:grid-cols-2 xl:grid-cols-4">
      {items.map((it) => (
        <div key={it.label} className="border-b border-line px-4 py-3 last:border-b-0 sm:border-r xl:border-b-0 xl:last:border-r-0">
          <p className="text-xs text-muted">{it.label}</p>
          <p className="mt-0.5 text-[15px] font-medium">{it.value}</p>
          {it.sub && <p className="text-xs text-muted">{it.sub}</p>}
        </div>
      ))}
    </div>
  );
}

const KIND_LABEL: Record<string, string> = { ethernet: "Ethernet", loopback: "Loopback", bridge: "Bridge", vlan: "VLAN", bond: "Bond", veth: "veth", wireguard: "WireGuard", tun: "Tunnel", tunnel: "Tunnel", dummy: "Dummy", macvlan: "macvlan" };

function InterfacesSection({ facts }: { facts: HostFacts }) {
  const rows = [...facts.interfaces].sort((a, b) => Number(a.kind === "loopback") - Number(b.kind === "loopback") || a.index - b.index);
  const columns: Column<HostInterface>[] = [
    {
      key: "name",
      header: "Interface",
      cell: (i) => (
        <span className="grid">
          <span className="font-medium">{i.name}</span>
          <span className="text-xs text-muted">
            {KIND_LABEL[i.kind] ?? i.kind}
            {i.vlan_id != null && ` ${i.vlan_id} on ${i.parent}`}
            {i.master && `, member of ${i.master}`}
          </span>
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (i) => {
        const tone: Tone = i.oper_state === "up" ? "ok" : i.oper_state === "unknown" && i.carrier ? "neutral" : i.admin_up ? "warn" : "neutral";
        const label = !i.admin_up ? "Disabled" : i.oper_state === "up" ? "Up" : !i.carrier ? "No carrier" : i.oper_state[0].toUpperCase() + i.oper_state.slice(1);
        return <Badge tone={tone}>{label}</Badge>;
      },
    },
    {
      key: "addr",
      header: "Addresses",
      cell: (i) =>
        i.addresses.length ? (
          <span className="grid gap-0.5">
            {i.addresses.map((a) => (
              <span key={a.address} className="flex items-center gap-1.5">
                <span className={cn("addr", a.scope === "link" && "text-muted")}>{a.address}</span>
                {a.dynamic && <Badge tone="info">{a.family === 6 ? "Auto" : "DHCP"}</Badge>}
                {a.scope === "link" && <span className="text-2xs text-faint">link-local</span>}
                {a.deprecated && <Badge tone="warn">Deprecated</Badge>}
              </span>
            ))}
          </span>
        ) : (
          <span className="text-faint">None</span>
        ),
    },
    { key: "mac", header: "MAC", cell: (i) => (i.mac ? <span className="addr text-muted">{i.mac}</span> : <span className="text-faint">—</span>) },
    { key: "mtu", header: "MTU", cell: (i) => <span className="tabular-nums">{i.mtu ?? "—"}</span> },
    {
      key: "mgr",
      header: "Managed by",
      cell: (i) =>
        i.managed_by ? (
          <span className="grid">
            <span>{i.managed_by}</span>
            <span className="text-xs text-muted">
              {i.manager_state}
              {i.manager_profile && i.manager_profile !== i.name && `, profile ${i.manager_profile}`}
            </span>
          </span>
        ) : (
          <span className="text-faint">{i.manager_state === "connected (externally)" ? "Configured outside NetworkManager" : "Not managed"}</span>
        ),
    },
  ];
  return (
    <Section title="Interfaces" description={`${facts.interfaces.length} on the server, as reported by ip.`}>
      <DataTable dense columns={columns} rows={rows} rowKey={(i) => i.name} />
    </Section>
  );
}

function RoutesSection({ routes }: { routes: HostRoute[] }) {
  const sorted = [...routes].sort((a, b) => Number(b.destination.endsWith("/0")) - Number(a.destination.endsWith("/0")) || a.family - b.family);
  const columns: Column<HostRoute>[] = [
    {
      key: "dst",
      header: "Destination",
      cell: (r) => (
        <span className="flex items-center gap-2">
          <span className="addr">{r.destination}</span>
          {r.destination.endsWith("/0") && (r.type == null || r.type === "unicast") && <Badge tone="accent">Default</Badge>}
          {r.type && r.type !== "unicast" && <Badge>{r.type}</Badge>}
        </span>
      ),
    },
    { key: "via", header: "Via", cell: (r) => (r.gateway ? <span className="addr">{r.gateway}</span> : <span className="text-muted">Direct</span>) },
    { key: "dev", header: "Interface", cell: (r) => r.interface ?? "—" },
    { key: "metric", header: "Metric", cell: (r) => <span className="tabular-nums">{r.metric ?? <span className="text-faint">—</span>}</span> },
    {
      key: "src",
      header: "Origin",
      cell: (r) => (
        <span className="text-muted">
          {r.protocol ?? "—"}
          {r.flags.includes("linkdown") && <Badge tone="warn" className="ml-1.5">Link down</Badge>}
        </span>
      ),
    },
  ];
  return (
    <Section title="Routes" description="Main routing table, IPv4 and IPv6.">
      <DataTable dense columns={columns} rows={sorted} rowKey={(r) => `${r.family}-${r.destination}-${r.gateway}-${r.interface}-${r.protocol}-${r.metric}`} empty={<p className="p-4 text-[13px] text-muted">No routes reported.</p>} />
    </Section>
  );
}

function DnsSection({ facts }: { facts: HostFacts }) {
  const d = facts.dns;
  return (
    <Section title="DNS" description={`Applications use: ${d.source}.`}>
      <div className="grid gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4 text-[13px]">
        <Facts
          items={[
            ["Name servers", d.effective_servers.length ? <span className="addr grid">{d.effective_servers.map((s) => <span key={s}>{s}</span>)}</span> : "None"],
            ["Search domains", d.resolv_conf_search.length ? d.resolv_conf_search.join(", ") : null],
          ]}
        />
        {d.resolved && (
          <div className="grid gap-1.5">
            <p className="text-xs font-medium text-muted">systemd-resolved, per interface</p>
            <table className="w-full text-[13px]">
              <tbody>
                {d.resolved.filter((r) => r.servers.length || r.domains.length).map((r) => (
                  <tr key={r.scope} className="border-t border-line">
                    <td className="py-1.5 pr-3 align-top text-muted">{r.scope === "global" ? "Global" : r.scope}</td>
                    <td className="addr py-1.5 align-top">{r.servers.join(" ") || "—"}</td>
                    <td className="py-1.5 pl-3 align-top text-muted">{r.domains.join(" ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!d.uses_resolved_stub && <p className="text-xs text-warn">resolv.conf does not point at systemd-resolved, so these per-interface servers are not what most applications use.</p>}
          </div>
        )}
      </div>
    </Section>
  );
}

const CMP: Record<ComparisonRow["status"], { tone: Tone; label: string }> = {
  match: { tone: "ok", label: "Matches" },
  mask_differs: { tone: "warn", label: "Mask differs" },
  interface_differs: { tone: "warn", label: "Interface differs" },
  only_on_server: { tone: "info", label: "Not documented" },
  only_in_netbox: { tone: "danger", label: "Not on server" },
};

function ComparisonSection({
  server,
  q,
  isAdmin,
  onLinked,
}: {
  server: Server;
  q: ReturnType<typeof useQuery<Comparison>>;
  isAdmin: boolean;
  onLinked: () => void;
}) {
  const c = q.data;
  const link = useMutation({
    mutationFn: (ref: { object_type: string; object_id: number } | null) =>
      api<Server>(`/servers/${server.id}`, { method: "PATCH", body: ref ? { netbox: ref } : { unlink_netbox: true } }),
    onSuccess: (_s, ref) => {
      toast.success(ref ? "Linked to NetBox" : "Unlinked from NetBox");
      onLinked();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const columns: Column<ComparisonRow>[] = [
    { key: "addr", header: "Address", cell: (r) => <span className="addr font-medium">{r.address}</span> },
    {
      key: "status",
      header: "Result",
      cell: (r) => {
        let { tone, label } = CMP[r.status];
        if (r.status === "only_on_server") {
          // Without a link there is nothing to compare against, only "is it in NetBox at all".
          if (!c?.linked) [tone, label] = r.recorded_elsewhere ? ["neutral", "In NetBox"] : ["info", "Not in NetBox"];
          else if (r.recorded_elsewhere) [tone, label] = ["warn", "Documented elsewhere"];
        }
        return <Badge tone={tone}>{label}</Badge>;
      },
    },
    {
      key: "server",
      header: "On the server",
      cell: (r) => (r.server_address ? <span><span className="addr">{r.server_address}</span><span className="text-muted"> on {r.server_interface}</span></span> : <span className="text-faint">—</span>),
    },
    {
      key: "netbox",
      header: "In NetBox",
      cell: (r) =>
        r.netbox_address ? (
          <span><span className="addr">{r.netbox_address}</span>{r.netbox_interface && <span className="text-muted"> on {r.netbox_interface}</span>}</span>
        ) : r.recorded_elsewhere ? (
          <span className="text-muted">Recorded as {r.recorded_elsewhere}</span>
        ) : (
          <span className="text-faint">Not recorded</span>
        ),
    },
  ];
  return (
    <Section
      title="Documented in NetBox vs. on the server"
      description="Compares addresses, ignoring loopback and link-local. Read-only: differences are shown, not fixed."
      actions={
        c?.linked && c.netbox ? (
          <span className="flex items-center gap-2 text-xs">
            {c.netbox.url ? (
              <a href={c.netbox.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                {c.netbox.name ?? `#${c.netbox.object_id}`} <ExternalLink className="size-3" />
              </a>
            ) : (
              <span>{c.netbox.name ?? `#${c.netbox.object_id}`}</span>
            )}
            {isAdmin && (
              <Button size="sm" variant="ghost" onClick={() => link.mutate(null)} loading={link.isPending}>
                <Unlink /> Unlink
              </Button>
            )}
          </span>
        ) : undefined
      }
    >
      {q.isPending ? (
        <Skeleton className="h-24 w-full" />
      ) : q.error ? (
        <Notice tone="danger">{errorMessage(q.error)}</Notice>
      ) : c ? (
        <>
          {!c.linked && (
            <div className="grid gap-3 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
              <p className="text-[13px]">
                Link this server to its NetBox device or virtual machine to compare against the addresses documented for it. Until then, Lanvex only shows whether each address appears
                anywhere in NetBox.
              </p>
              {isAdmin && (
                <>
                  {c.suggestions.length > 0 && (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-muted">Same name in NetBox:</span>
                      {c.suggestions.map((sug) => (
                        <Button key={`${sug.object_type}-${sug.object_id}`} size="sm" onClick={() => link.mutate({ object_type: sug.object_type, object_id: sug.object_id })} loading={link.isPending}>
                          Link to {sug.name} ({sug.object_type === "dcim.device" ? "device" : "VM"})
                        </Button>
                      ))}
                    </div>
                  )}
                  <LinkPicker onPick={(ref) => link.mutate({ object_type: ref.object_type, object_id: ref.object_id })} busy={link.isPending} />
                </>
              )}
            </div>
          )}
          <DataTable dense columns={columns} rows={c.rows} rowKey={(r) => r.address} empty={<p className="p-4 text-[13px] text-muted">No addresses to compare.</p>} />
        </>
      ) : null}
    </Section>
  );
}

function LinkPicker({ onPick, busy }: { onPick: (r: NetBoxRef) => void; busy: boolean }) {
  const [q, setQ] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const results = useQuery({
    queryKey: ["servers", "link-candidates", debounced],
    queryFn: () => api<{ results: NetBoxRef[] }>("/servers/link-candidates", { query: { q: debounced } }),
    enabled: debounced.length > 0,
  });
  const [picked, setPicked] = React.useState("");
  const options = results.data?.results ?? [];
  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
      <Field label="Find in NetBox" htmlFor="link-q">
        <Input id="link-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Device or VM name" />
      </Field>
      <Field label="Object" htmlFor="link-pick">
        <Select id="link-pick" value={picked} onChange={(e) => setPicked(e.target.value)} disabled={!options.length}>
          <option value="">{debounced ? (results.isFetching ? "Searching…" : options.length ? "Choose" : "No matches") : "Type to search"}</option>
          {options.map((o) => (
            <option key={`${o.object_type}:${o.object_id}`} value={`${o.object_type}:${o.object_id}`}>
              {o.name} ({o.object_type === "dcim.device" ? "device" : "VM"})
            </option>
          ))}
        </Select>
      </Field>
      <Button
        disabled={!picked}
        loading={busy}
        onClick={() => {
          const o = options.find((x) => `${x.object_type}:${x.object_id}` === picked);
          if (o) onPick(o);
        }}
      >
        Link
      </Button>
    </div>
  );
}

function AccessSection({ server: s }: { server: Server }) {
  return (
    <Section title="Access" description="How Lanvex connects. Credentials are encrypted and never displayed.">
      <div className="grid gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
        <Facts
          items={[
            ["Sign-in", `${AUTH_LABEL[s.auth_method]} as ${s.username}`],
            ["Host key", <span key="hk" className="grid"><span className="addr break-all">{s.host_key_fingerprint}</span><span className="text-xs text-muted">{s.host_key_algorithm}, pinned {relativeTime(s.host_key_pinned_at)}</span></span>],
            ["Capabilities", <span key="cap" className="grid"><span>Read system, interfaces, addresses, routes, DNS and network stack</span><span className="text-xs text-muted">Read-only. This adapter cannot change the server.</span></span>],
          ]}
        />
        {s.authorized_keys_line && (
          <div className="grid gap-1.5">
            <p className="text-xs font-medium text-muted">Public key to authorize on the server</p>
            <CodeBlock text={s.authorized_keys_line} label="Copy key" />
          </div>
        )}
      </div>
    </Section>
  );
}

function HistorySection({ rows, loading }: { rows: SnapshotMeta[] | undefined; loading: boolean }) {
  const columns: Column<SnapshotMeta>[] = [
    { key: "t", header: "When", cell: (r) => <span title={absoluteTime(r.taken_at)}>{relativeTime(r.taken_at)}</span> },
    { key: "s", header: "Result", cell: (r) => <ConnectionBadge status={r.status} /> },
    { key: "d", header: "Took", cell: (r) => (r.duration_ms != null ? `${r.duration_ms} ms` : "—"), className: "tabular-nums text-muted" },
    { key: "b", header: "By", cell: (r) => r.requested_by ?? "—", className: "text-muted" },
  ];
  return (
    <Section title="Reads" description="Each read is kept as a snapshot; older ones are pruned.">
      <DataTable dense columns={columns} rows={rows} rowKey={(r) => r.id} loading={loading} empty={<p className="p-4 text-[13px] text-muted">Not read yet.</p>} />
    </Section>
  );
}

function EditDialog({ server: s, open, onClose, onSaved }: { server: Server; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = React.useState({ name: s.name, description: s.description, username: s.username });
  React.useEffect(() => {
    if (open) setF({ name: s.name, description: s.description, username: s.username });
  }, [open, s]);
  const save = useMutation({
    mutationFn: () => api<Server>(`/servers/${s.id}`, { method: "PATCH", body: f }),
    onSuccess: () => {
      toast.success("Saved");
      onSaved();
      onClose();
    },
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Edit ${s.name}`} description="To move the server to another address, remove it and add it again so its host key is verified.">
        <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
          <DialogBody>
            <Field label="Name" htmlFor="ed-name" error={fieldError(save.error, "name")}>
              <Input id="ed-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxLength={100} required />
            </Field>
            <Field label="Description" htmlFor="ed-desc" optional>
              <Input id="ed-desc" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} maxLength={200} />
            </Field>
            <Field label="User on the server" htmlFor="ed-user" error={fieldError(save.error, "username")}>
              <Input id="ed-user" className="addr" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required />
            </Field>
            {save.error && !fieldError(save.error, "name", "username") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" loading={save.isPending}>Save changes</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CredentialsDialog({ server: s, open, onClose, onSaved }: { server: Server; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [c, setC] = React.useState<CredentialDraft>(EMPTY_CREDENTIALS);
  const [result, setResult] = React.useState<Server | null>(null);
  React.useEffect(() => {
    if (open) {
      setC(EMPTY_CREDENTIALS);
      setResult(null);
    }
  }, [open]);
  const save = useMutation({
    mutationFn: () => api<{ server: Server }>(`/servers/${s.id}/credentials`, { method: "PUT", body: credentialPayload(c) }),
    onSuccess: (r) => {
      onSaved();
      if (r.server.authorized_keys_line) setResult(r.server);
      else {
        toast.success("Credentials replaced");
        onClose();
      }
    },
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Replace credentials" description="The old credentials are discarded." width="max-w-xl">
        <DialogBody>
          {result ? (
            <>
              <Notice title="New key created">Authorize it on the server and remove the old key from authorized_keys.</Notice>
              <InstallKeyInstructions username={result.username} line={result.authorized_keys_line!} />
            </>
          ) : (
            <CredentialsForm value={c} onChange={setC} errors={{ private_key: fieldError(save.error, "private_key"), passphrase: fieldError(save.error, "passphrase"), password: fieldError(save.error, "password") }} />
          )}
          {save.error && !fieldError(save.error, "private_key", "passphrase", "password") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
        </DialogBody>
        <DialogFooter>
          {result ? (
            <Button variant="primary" onClick={onClose}>Done</Button>
          ) : (
            <>
              <Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending} disabled={!credentialsComplete(c)}>Replace credentials</Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function HostKeyDialog({ server: s, open, onClose, onSaved }: { server: Server; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [scan, setScan] = React.useState<HostKeyScan | null>(null);
  const [confirmed, setConfirmed] = React.useState(false);
  const fetchKey = useMutation({
    mutationFn: () => api<HostKeyScan>("/servers/scan-host-key", { method: "POST", body: { address: s.address, port: s.port } }),
    onSuccess: (r) => {
      setScan(r);
      setConfirmed(false);
    },
  });
  React.useEffect(() => {
    if (open) {
      setScan(null);
      setConfirmed(false);
      fetchKey.mutate();
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const pin = useMutation({
    mutationFn: () => api<Server>(`/servers/${s.id}/host-key`, { method: "PUT", body: { public_key: scan!.public_key, fingerprint_sha256: scan!.fingerprint_sha256 } }),
    onSuccess: () => {
      toast.success("New host key trusted", { description: "Test the connection to confirm." });
      onSaved();
      onClose();
    },
  });
  const same = scan?.fingerprint_sha256 === s.host_key_fingerprint;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Verify a new host key" description={`For ${s.address}:${s.port}`} width="max-w-xl">
        <DialogBody>
          {fetchKey.isPending && <Skeleton className="h-24 w-full" />}
          {fetchKey.error && <Notice tone="danger">{errorMessage(fetchKey.error)}</Notice>}
          {scan && same && <Notice title="Unchanged">The server still presents the key Lanvex has pinned. Nothing to do.</Notice>}
          {scan && !same && <HostKeyVerification scan={scan} confirmed={confirmed} onConfirm={setConfirmed} previous={s.host_key_fingerprint} />}
          {pin.error && <Notice tone="danger">{errorMessage(pin.error)}</Notice>}
        </DialogBody>
        <DialogFooter>
          <Button onClick={onClose}>{same ? "Close" : "Cancel"}</Button>
          {!same && <Button variant="primary" disabled={!scan || !confirmed} loading={pin.isPending} onClick={() => pin.mutate()}>Trust new key</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteDialog({ server: s, open, onClose, onDeleted }: { server: Server; open: boolean; onClose: () => void; onDeleted: () => void }) {
  const [confirm, setConfirm] = React.useState("");
  React.useEffect(() => setConfirm(""), [open]);
  const del = useMutation({
    mutationFn: () => api(`/servers/${s.id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast.success(`Removed ${s.name}`);
      onClose();
      onDeleted();
    },
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Remove ${s.name}?`} description="Lanvex deletes its stored credentials and read history for this server." role="alertdialog">
        <form onSubmit={(e) => { e.preventDefault(); if (confirm === s.name) del.mutate(); }}>
          <DialogBody>
            {s.authorized_keys_line && <p className="text-[13px] text-muted">The server itself is not touched. Remove Lanvex's key from its authorized_keys file yourself.</p>}
            <Field label={`Type ${s.name} to confirm`} htmlFor="del-srv">
              <Input id="del-srv" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" autoFocus />
            </Field>
            {del.error && <Notice tone="danger">{errorMessage(del.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="danger" disabled={confirm !== s.name} loading={del.isPending}>Remove server</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
