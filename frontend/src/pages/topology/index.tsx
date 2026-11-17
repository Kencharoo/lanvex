import "@xyflow/react/dist/style.css";
import "./topology.css";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { Cable, EthernetPort, ExternalLink, Globe, Map as MapIcon, Rows3, Server, Share2, X } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";
import { DataTable, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { Facts, PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Checkbox, Select } from "@/components/ui/form";
import { Badge, EmptyState, Notice, Skeleton, StatusBadge } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useUrlState } from "@/lib/hooks";
import { useNetBoxLabel } from "@/lib/system";
import type { Topology, TopologyLink, TopologyNode } from "@/lib/types";
import { NotConfigured, isNotConfigured } from "../networks";
import { orient, place, tiers, SIZE, type Placement } from "./layout";
import { nodeTypes, type DeviceData, type SiteData } from "./nodes";

const KIND_LABEL: Record<TopologyLink["kind"], string> = {
  data: "Data",
  patch: "Through a patch panel",
  circuit: "Provider circuit",
  console: "Console",
  power: "Power",
};

function edgeStyle(link: TopologyLink, selected: boolean, dim: boolean): React.CSSProperties {
  const planned = link.status && link.status.value !== "connected";
  const stroke = selected
    ? "var(--accent)"
    : link.kind === "circuit"
      ? "var(--info)"
      : link.kind === "data"
        ? "var(--muted)"
        : "var(--faint)";
  const dash = link.kind === "circuit" ? "6 4" : link.kind === "console" || link.kind === "power" ? "2 4" : undefined;
  return {
    stroke,
    strokeWidth: selected ? 2.5 : 1.5,
    strokeDasharray: planned ? "5 5" : dash,
    opacity: dim ? 0.2 : planned ? 0.75 : 1,
  };
}

function matches(n: TopologyNode, q: string): boolean {
  return [n.name, n.primary_ip, n.role?.name, n.device_type, n.provider, n.site?.name]
    .filter(Boolean)
    .some((v) => (v as string).toLowerCase().includes(q));
}

/** Zooms to what the search found, or to the device a shared link selected, once the map is drawn. */
function Focus({ ids, ready }: { ids: string[]; ready: boolean }) {
  const flow = useReactFlow();
  const key = ids.join(",");
  React.useEffect(() => {
    if (!ready || !key) return;
    const t = setTimeout(() => flow.fitView({ nodes: key.split(",").map((id) => ({ id })), duration: 400, maxZoom: 1.25, padding: 0.4 }), 60);
    return () => clearTimeout(t);
  }, [flow, key, ready]);
  return null;
}

export function TopologyPage() {
  const nbName = useNetBoxLabel();
  const { values, set } = useUrlState(["site", "q", "node", "view", "all"] as const);
  const query = { site_id: values.site || undefined };
  const data = useQuery({
    queryKey: ["topology", query],
    queryFn: ({ signal }) => api<Topology>("/topology", { query, signal }),
    placeholderData: keepPreviousData,
  });
  const topo = data.data;
  const showAll = values.all === "1";
  const view = values.view === "cables" ? "cables" : "map";
  const q = values.q.trim().toLowerCase();
  const [selectedLink, setSelectedLink] = React.useState<string | null>(null);
  // A link that opens the page with a device selected zooms to it once; clicking around does not.
  const linkedNode = React.useRef(values.node || null);

  const { nodes, links, unconnected, byId } = React.useMemo(() => {
    const all = topo?.nodes ?? [];
    const cabled = new Set([...(topo?.links ?? []), ...(topo?.seen ?? [])].flatMap((l) => [l.a, l.b]));
    const shown = showAll ? all : all.filter((n) => cabled.has(n.id));
    return {
      nodes: shown,
      links: topo?.links ?? [],
      unconnected: all.length - all.filter((n) => cabled.has(n.id)).length,
      byId: new Map(all.map((n) => [n.id, n])),
    };
  }, [topo, showAll]);

  const [placement, setPlacement] = React.useState<{ for: unknown; at: Placement } | null>(null);
  const [layoutError, setLayoutError] = React.useState<string | null>(null);
  React.useEffect(() => {
    let alive = true;
    setLayoutError(null);
    if (!topo || !nodes.length) return;
    const labels = new Map<string, string>(topo.sites.map((s) => [`site:${s.id}`, s.name]));
    labels.set("elsewhere", "Other sites");
    place(nodes, links, labels)
      .then((at) => alive && setPlacement({ for: nodes, at }))
      .catch((e: unknown) => alive && setLayoutError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [topo, nodes, links]);
  const laidOut = placement && placement.for === nodes ? placement.at : null;

  const found = React.useMemo(() => (q ? nodes.filter((n) => matches(n, q)).map((n) => n.id) : []), [nodes, q]);
  const foundSet = React.useMemo(() => new Set(found), [found]);

  const flowNodes = React.useMemo<Node[]>(() => {
    if (!laidOut) return [];
    const groups: Node<SiteData>[] = laidOut.groups.map((g) => ({
      id: g.id,
      type: "site",
      position: { x: g.x, y: g.y },
      data: { label: g.label, width: g.width, height: g.height },
      width: g.width,
      height: g.height,
      selectable: false,
      draggable: false,
      focusable: false,
      zIndex: -1,
    }));
    const items: Node<DeviceData>[] = nodes.flatMap((n) => {
      const at = laidOut.nodes.get(n.id);
      if (!at) return [];
      // Exact sizes up front: the minimap and edges need them before the browser has measured anything.
      const size = n.kind === "circuit" ? SIZE.circuit : laidOut.stacked.has(n.id) ? SIZE.leaf : SIZE.device;
      return [
        {
          id: n.id,
          type: n.kind,
          position: { x: at.x, y: at.y },
          width: size.width,
          height: size.height,
          parentId: at.parent,
          data: { node: n, dim: !!q && !foundSet.has(n.id), match: !!q && foundSet.has(n.id), leaf: laidOut.stacked.has(n.id) },
          selected: values.node === n.id,
          draggable: true,
          ariaLabel: `${n.kind === "circuit" ? "Circuit" : "Device"} ${n.name}`,
        },
      ];
    });
    return [...groups, ...items];
  }, [laidOut, nodes, q, foundSet, values.node]);

  // React Flow owns node state while the map is shown (dragging); a new layout or search replaces it.
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  React.useEffect(() => setRfNodes(flowNodes), [flowNodes, setRfNodes]);

  const flowEdges = React.useMemo<Edge[]>(() => {
    if (!laidOut) return [];
    const ends = orient(links, tiers(nodes, links));
    const groupAt = new Map(laidOut.groups.map((g) => [g.id, g]));
    const x = (id: string) => {
      const at = laidOut.nodes.get(id)!;
      return at.x + (at.parent ? (groupAt.get(at.parent)?.x ?? 0) : 0);
    };
    return links
      .map((l, i): Edge | null => {
        if (!laidOut.nodes.has(l.a) || !laidOut.nodes.has(l.b)) return null;
        const pair = ends[i];
        // Down from the upper end; between peers, from the left one to the right one; into a stack
        // of end devices, down the left edge.
        const [source, target] = pair ?? (x(l.a) <= x(l.b) ? [l.a, l.b] : [l.b, l.a]);
        const intoStack = laidOut.stacked.has(target);
        const dim = !!q && !foundSet.has(l.a) && !foundSet.has(l.b);
        return {
          id: l.id,
          source,
          target,
          type: intoStack ? "smoothstep" : "default",
          sourceHandle: intoStack ? "bl" : pair ? "b" : "r",
          targetHandle: intoStack || !pair ? "l" : "t",
          selected: selectedLink === l.id,
          style: edgeStyle(l, selectedLink === l.id, dim),
          interactionWidth: 16,
          ariaLabel: `Cable ${l.label || `#${l.cable_id}`}`,
        };
      })
      .filter((e): e is Edge => e !== null)
      .concat(
        (topo?.seen ?? [])
          .filter((l) => laidOut.nodes.has(l.a) && laidOut.nodes.has(l.b))
          .map((l): Edge => {
            const [source, target] = x(l.a) <= x(l.b) ? [l.a, l.b] : [l.b, l.a];
            const dim = !!q && !foundSet.has(l.a) && !foundSet.has(l.b);
            return {
              id: l.id,
              source,
              target,
              sourceHandle: "r",
              targetHandle: "l",
              selectable: false,
              focusable: false,
              style: {
                stroke: l.status === "different" ? "var(--danger)" : "var(--warn)",
                strokeWidth: 1.5,
                strokeDasharray: "2 5",
                opacity: dim ? 0.2 : 1,
              },
              ariaLabel: `Seen by a switch: ${l.a_port} to ${l.b_port ?? "?"}`,
            };
          }),
      );
  }, [laidOut, nodes, links, q, foundSet, selectedLink, topo]);

  const selectedNode = values.node ? byId.get(values.node) : undefined;
  const link = selectedLink ? links.find((l) => l.id === selectedLink) : undefined;
  const select = (node: string | null) => {
    setSelectedLink(null);
    set({ node });
  };

  const header = (
    <PageHeader
      title="Topology"
    />
  );
  if (isNotConfigured(data.error)) {
    return (
      <>
        {header}
        <NotConfigured />
      </>
    );
  }

  return (
    <>
      {header}
      <Toolbar>
        <Select
          aria-label="Site"
          className="w-auto"
          value={values.site}
          onChange={(e) => {
            setSelectedLink(null);
            set({ site: e.target.value || null, node: null });
          }}
        >
          <option value="">All sites</option>
          {topo?.sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </Select>
        <SearchInput value={values.q} onChange={(v) => set({ q: v || null })} placeholder="Find a device, IP or role" label="Find on the map" />
        {unconnected > 0 && (
          <Checkbox
            id="topology-all"
            label={`Show ${unconnected} device${unconnected === 1 ? "" : "s"} without cables`}
            checked={showAll}
            onChange={(e) => set({ all: e.target.checked ? "1" : null })}
          />
        )}
        <div className="ml-auto flex gap-1 rounded-[var(--radius-control)] border border-line bg-surface p-0.5" role="group" aria-label="View">
          <Button size="sm" variant={view === "map" ? "secondary" : "ghost"} aria-pressed={view === "map"} onClick={() => set({ view: null })}>
            <MapIcon /> Map
          </Button>
          <Button size="sm" variant={view === "cables" ? "secondary" : "ghost"} aria-pressed={view === "cables"} onClick={() => set({ view: "cables" })}>
            <Rows3 /> Cables
          </Button>
        </div>
      </Toolbar>

      {topo?.truncated && (
        <Notice tone="warn" title="Only part of the network is shown">
          Limited to 2,000 devices and 5,000 cables. Choose a site.
        </Notice>
      )}
      {q && topo && (
        <p className="mb-2 text-[13px] text-muted" role="status">
          {found.length ? `${found.length} found and highlighted.` : "Nothing on the map matches."}
        </p>
      )}

      {data.isPending ? (
        <Skeleton className="h-[calc(100dvh-15rem)] min-h-[440px] w-full" />
      ) : data.error ? (
        <Notice tone="danger" title={`${nbName} did not answer`}>
          {data.error instanceof Error ? data.error.message : String(data.error)}{" "}
          <button className="underline" onClick={() => void data.refetch()}>
            Try again
          </button>
        </Notice>
      ) : !topo?.nodes.length ? (
        <EmptyState icon={Share2} title={values.site ? "No devices at this site" : "Nothing documented yet"}>
          Record devices and the cables between them in {nbName}; they appear here.
        </EmptyState>
      ) : view === "cables" ? (
        <CableList links={links} byId={byId} onOpen={(id) => { set({ view: null, node: null }); setSelectedLink(id); }} />
      ) : !nodes.length ? (
        <EmptyState icon={Cable} title={values.site ? "No cables documented at this site" : "No cables documented"}>
          {topo.nodes.length} device{topo.nodes.length === 1 ? " is" : "s are"} recorded, but no cables between them yet.{" "}
          <button className="underline" onClick={() => set({ all: "1" })}>
            Show them anyway
          </button>
        </EmptyState>
      ) : (
        <div className="topology relative h-[calc(100dvh-15rem)] min-h-[440px] overflow-hidden rounded-[var(--radius-panel)] border border-line">
          {layoutError ? (
            <Notice tone="danger" title="The map could not be drawn">
              {layoutError} The Cables view lists the same data.
            </Notice>
          ) : !laidOut ? (
            <div className="grid h-full place-items-center text-[13px] text-muted" role="status">
              Arranging {nodes.length} devices…
            </div>
          ) : (
            <ReactFlowProvider>
              <ReactFlow
                nodes={rfNodes}
                onNodesChange={onNodesChange}
                edges={flowEdges}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.15, maxZoom: 1.2, minZoom: 0.35 }}
                minZoom={0.05}
                maxZoom={2.5}
                nodesConnectable={false}
                elementsSelectable
                onNodeClick={(_, n) => n.type !== "site" && select(n.id)}
                onEdgeClick={(_, e) => {
                  set({ node: null });
                  setSelectedLink(e.id);
                }}
                onPaneClick={() => select(null)}
              >
                <Background gap={22} size={1} />
                <Controls showInteractive={false} position="bottom-left" />
                {nodes.length > 30 && (
                  <MiniMap
                    pannable
                    zoomable
                    position="bottom-right"
                    ariaLabel="Overview of the whole map"
                    // SVG fill attributes cannot use CSS variables, hence plain colours.
                    nodeColor={(n) => (n.type === "site" ? "rgba(128,128,128,0.12)" : ((n.data as DeviceData).node?.color ?? "#8a929e"))}
                  />
                )}
                <Focus ids={found.length ? found : linkedNode.current ? [linkedNode.current] : []} ready={!!laidOut} />
              </ReactFlow>
            </ReactFlowProvider>
          )}
          {selectedNode && <NodePanel node={selectedNode} links={links} byId={byId} nbName={nbName} onSelect={select} />}
          {link && <LinkPanel link={link} byId={byId} nbName={nbName} onClose={() => setSelectedLink(null)} onSelect={select} />}
        </div>
      )}
      {topo && (
        <Legend skipped={topo.skipped_cables} seen={topo.seen.length > 0} />
      )}
    </>
  );
}

function Legend({ skipped, seen }: { skipped: number; seen: boolean }) {
  const line = (style: React.CSSProperties) => (
    <svg width="28" height="8" aria-hidden className="shrink-0">
      <line x1="0" y1="4" x2="28" y2="4" style={{ strokeWidth: 1.5, ...style }} />
    </svg>
  );
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-muted">
      <span className="flex items-center gap-1.5">{line({ stroke: "var(--muted)" })} Cable</span>
      <span className="flex items-center gap-1.5">{line({ stroke: "var(--faint)" })} Through a patch panel</span>
      <span className="flex items-center gap-1.5">{line({ stroke: "var(--info)", strokeDasharray: "6 4" })} Provider circuit</span>
      <span className="flex items-center gap-1.5">{line({ stroke: "var(--muted)", strokeDasharray: "5 5", opacity: 0.75 })} Planned or not connected</span>
      {seen && (
        <>
          <span className="flex items-center gap-1.5">{line({ stroke: "var(--warn)", strokeDasharray: "2 5" })} Seen by a switch, no cable recorded</span>
          <span className="flex items-center gap-1.5">{line({ stroke: "var(--danger)", strokeDasharray: "2 5" })} Seen by a switch, recorded differently</span>
        </>
      )}
      <span className="flex items-center gap-1.5">
        <span className="grid size-5 place-items-center rounded bg-accent-soft text-accent">
          <Server className="size-3" />
        </span>
        Read by Lanvex
      </span>
      {skipped > 0 && <span>{skipped} cable{skipped === 1 ? "" : "s"} to power feeds or other non-devices not drawn.</span>}
    </div>
  );
}

function Panel({ title, sub, onClose, children }: { title: React.ReactNode; sub?: React.ReactNode; onClose: () => void; children: React.ReactNode }) {
  return (
    <aside
      className="absolute inset-x-2 bottom-2 z-10 max-h-[60%] overflow-auto rounded-[var(--radius-panel)] border border-line bg-surface p-4 shadow-[var(--shadow-pop)] sm:inset-x-auto sm:bottom-auto sm:right-3 sm:top-3 sm:max-h-[calc(100%-1.5rem)] sm:w-80"
      aria-label="Details"
    >
      <div className="mb-3 flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-[15px] font-semibold">{title}</h2>
          {sub && <p className="text-[12px] text-muted">{sub}</p>}
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close details" onClick={onClose}>
          <X />
        </Button>
      </div>
      {children}
    </aside>
  );
}

function NodePanel({
  node,
  links,
  byId,
  nbName,
  onSelect,
}: {
  node: TopologyNode;
  links: TopologyLink[];
  byId: Map<string, TopologyNode>;
  nbName: string;
  onSelect: (id: string | null) => void;
}) {
  const mine = links.filter((l) => l.a === node.id || l.b === node.id);
  const facts: [string, React.ReactNode][] =
    node.kind === "circuit"
      ? [["Provider", node.provider ?? "—"]]
      : [
          ["Role", node.role?.name ?? "—"],
          ["Type", node.device_type ?? "—"],
          ["Site", node.site?.name ?? (node.external ? "Another site" : "—")],
          ["Status", node.status ? <StatusBadge value={node.status.value} label={node.status.label} /> : "—"],
          ["Management IP", node.primary_ip ? <span className="addr">{node.primary_ip}</span> : "—"],
        ];
  return (
    <Panel title={node.name} sub={node.kind === "circuit" ? "Provider circuit" : node.external ? "Device at another site" : "Device"} onClose={() => onSelect(null)}>
      <Facts items={facts} />
      <h3 className="mb-1.5 mt-4 text-[12px] font-semibold uppercase tracking-wide text-muted">
        Cables ({mine.length})
      </h3>
      <ul className="grid gap-1.5 text-[13px]">
        {mine.map((l) => {
          const here = l.a === node.id;
          const other = byId.get(here ? l.b : l.a);
          return (
            <li key={l.id} className="flex flex-wrap items-baseline gap-x-1.5">
              <span className="addr text-muted">{(here ? l.a_ports : l.b_ports).join(", ")}</span>
              <span className="text-faint">→</span>
              <button className="font-medium hover:underline" onClick={() => other && onSelect(other.id)}>
                {other?.name ?? "?"}
              </button>
              <span className="addr text-muted">{(here ? l.b_ports : l.a_ports).join(", ")}</span>
              {l.status && l.status.value !== "connected" && <Badge tone="warn">{l.status.label}</Badge>}
            </li>
          );
        })}
      </ul>
      <div className="mt-4 flex flex-wrap gap-2">
        {node.kind === "device" && (
          <Button size="sm" asChild>
            <Link to={`/devices/${node.object_id}`}>Open device</Link>
          </Button>
        )}
        {node.server_id && (
          <Button size="sm" asChild>
            <Link to={`/servers/${node.server_id}`}>
              <Server /> Open server
            </Link>
          </Button>
        )}
        {node.switch_id && (
          <Button size="sm" asChild>
            <Link to={`/switches/${node.switch_id}`}>
              <EthernetPort /> Open switch
            </Link>
          </Button>
        )}
        {node.netbox_url && (
          <Button size="sm" variant="ghost" asChild>
            <a href={node.netbox_url}>
              <ExternalLink /> Open in {nbName}
            </a>
          </Button>
        )}
      </div>
    </Panel>
  );
}

function LinkPanel({
  link,
  byId,
  nbName,
  onClose,
  onSelect,
}: {
  link: TopologyLink;
  byId: Map<string, TopologyNode>;
  nbName: string;
  onClose: () => void;
  onSelect: (id: string | null) => void;
}) {
  const end = (id: string, ports: string[]) => (
    <span>
      <button className="font-medium hover:underline" onClick={() => onSelect(id)}>
        {byId.get(id)?.name ?? "?"}
      </button>{" "}
      <span className="addr text-muted">{ports.join(", ")}</span>
    </span>
  );
  return (
    <Panel title={link.label || `Cable #${link.cable_id}`} sub={KIND_LABEL[link.kind]} onClose={onClose}>
      <Facts
        items={[
          ["From", end(link.a, link.a_ports)],
          ["To", end(link.b, link.b_ports)],
          ["Status", link.status ? <StatusBadge value={link.status.value} label={link.status.label} /> : "—"],
          ["Type", link.type ? <span className="uppercase">{link.type}</span> : "—"],
          [
            "Colour",
            link.color ? (
              <span className="flex items-center gap-1.5">
                <span className="size-3 rounded-full border border-line" style={{ background: link.color }} /> {link.color}
              </span>
            ) : (
              "—"
            ),
          ],
        ]}
      />
      {link.netbox_url && (
        <Button size="sm" variant="ghost" className="mt-4" asChild>
          <a href={link.netbox_url}>
            <ExternalLink /> Open in {nbName}
          </a>
        </Button>
      )}
    </Panel>
  );
}

function CableList({ links, byId, onOpen }: { links: TopologyLink[]; byId: Map<string, TopologyNode>; onOpen: (id: string) => void }) {
  const name = (id: string) => byId.get(id)?.name ?? "?";
  const columns: Column<TopologyLink>[] = [
    {
      key: "a",
      header: "From",
      cell: (l) => (
        <span>
          <span className="font-medium">{name(l.a)}</span> <span className="addr text-muted">{l.a_ports.join(", ")}</span>
        </span>
      ),
    },
    {
      key: "b",
      header: "To",
      cell: (l) => (
        <span className="flex items-center gap-1.5">
          {byId.get(l.b)?.kind === "circuit" && <Globe className="size-3.5 text-muted" aria-hidden />}
          <span className="font-medium">{name(l.b)}</span> <span className="addr text-muted">{l.b_ports.join(", ")}</span>
        </span>
      ),
    },
    { key: "kind", header: "Kind", cell: (l) => KIND_LABEL[l.kind], className: "text-muted" },
    { key: "status", header: "Status", cell: (l) => (l.status ? <StatusBadge value={l.status.value} label={l.status.label} /> : "—") },
    { key: "label", header: "Label", cell: (l) => l.label || <span className="text-faint">—</span> },
  ];
  return (
    <DataTable
      columns={columns}
      rows={links}
      rowKey={(l) => l.id}
      onRowClick={(l) => onOpen(l.id)}
      rowLabel={(l) => `Show cable ${l.label || `#${l.cable_id}`} on the map`}
      empty={<EmptyState icon={Cable} title="No cables documented" />}
    />
  );
}

