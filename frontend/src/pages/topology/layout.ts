import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk-api";
import workerUrl from "elkjs/lib/elk-worker.min.js?url";
import type { TopologyLink, TopologyNode } from "@/lib/types";

/** Node sizes in the map; the node components render at exactly these sizes. */
export const SIZE = {
  device: { width: 200, height: 56 },
  circuit: { width: 172, height: 44 },
  leaf: { width: 176, height: 40 },
} as const;

/**
 * End devices with a single cable up (servers under an access switch) are stacked in a column under
 * that switch instead of spreading the bottom row across thousands of pixels.
 */
export const STACK = { indent: 24, top: 16, gap: 6 } as const;

const GROUP_PADDING = "[top=44,left=20,bottom=20,right=20]";

export interface Group {
  id: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Placement {
  /** Positions of nodes; relative to their group when `parent` is set. */
  nodes: Map<string, { x: number; y: number; parent?: string }>;
  groups: Group[];
  /** Nodes drawn in a stack under the one device they are cabled to. */
  stacked: Set<string>;
}

/** End devices to stack: one cable, to a node on a higher tier that has other cables too, same site view. */
export function stacks(nodes: TopologyNode[], links: TopologyLink[], tier: Map<string, number>): Map<string, string[]> {
  const ends = new Map<string, string[]>();
  for (const l of links) {
    ends.set(l.a, [...(ends.get(l.a) ?? []), l.b]);
    ends.set(l.b, [...(ends.get(l.b) ?? []), l.a]);
  }
  const out = new Map<string, string[]>();
  for (const n of [...nodes].sort((x, y) => x.name.localeCompare(y.name))) {
    const up = ends.get(n.id) ?? [];
    // Devices from another site keep their own frame ("Other sites") and full card.
    if (n.kind !== "device" || n.external || up.length !== 1) continue;
    const parent = up[0];
    if ((ends.get(parent)?.length ?? 0) < 2 || (tier.get(parent) ?? 0) >= (tier.get(n.id) ?? 0)) continue;
    out.set(parent, [...(out.get(parent) ?? []), n.id]);
  }
  return out;
}

let elk: InstanceType<typeof ELK> | null = null;
// The layout runs in a web worker, so a large map never blocks the page.
const engine = () => (elk ??= new ELK({ workerUrl }));

/** Which site frame a node is drawn in. Circuits join sites, so they stay outside every frame. */
export function groupOf(node: TopologyNode): string | null {
  if (node.kind === "circuit") return null;
  if (node.site) return `site:${node.site.id}`;
  return "elsewhere";
}

// Where a device sits in a top-down map, by what its role says: the way out (circuits) on top, then
// core, distribution, access, patch panels, and the devices people use or run at the bottom.
const ROLE_TIERS: [RegExp, number][] = [
  [/\bpatch\b/, 2.5],
  [/\b(servers?|host|hypervisor|storage|nas|san|workstation|desktop|laptop|printer|phone|camera|ap|access point|wireless|wlan|iot)\b/, 3],
  [/\b(core|spine|router|firewall|edge|border|wan|gateway|internet)\b/, 0],
  [/\b(distribution|aggregation|agg|dist)\b/, 1],
  [/\b(access|leaf|tor|switch)\b/, 2],
];

function roleTier(n: TopologyNode): number | undefined {
  if (n.kind === "circuit") return -1;
  const text = `${n.role?.name ?? ""} ${n.role?.slug ?? ""}`.toLowerCase().replace(/[-_]/g, " ");
  return ROLE_TIERS.find(([re]) => re.test(text))?.[1];
}

/**
 * A tier for every node: from its role when the role says where it belongs, otherwise one below the
 * nearest node that has a tier (in a part of the network where no role says anything, the device
 * with the most cables is the top).
 */
export function tiers(nodes: TopologyNode[], links: TopologyLink[]): Map<string, number> {
  const tier = new Map<string, number>();
  const next = new Map<string, string[]>();
  for (const l of links) {
    next.set(l.a, [...(next.get(l.a) ?? []), l.b]);
    next.set(l.b, [...(next.get(l.b) ?? []), l.a]);
  }
  for (const n of nodes) {
    const t = roleTier(n);
    if (t !== undefined) tier.set(n.id, t);
  }
  const spread = (start: string[]) => {
    const queue = [...start].sort((x, y) => tier.get(x)! - tier.get(y)!);
    while (queue.length) {
      const id = queue.shift()!;
      for (const other of next.get(id) ?? []) {
        if (tier.has(other)) continue;
        tier.set(other, tier.get(id)! + 1);
        queue.push(other);
      }
    }
  };
  spread([...tier.keys()]);
  // Parts of the network that no role describes: start from their busiest device.
  const degree = (id: string) => next.get(id)?.length ?? 0;
  for (const n of [...nodes].sort((x, y) => degree(y.id) - degree(x.id))) {
    if (tier.has(n.id)) continue;
    tier.set(n.id, 0);
    spread([n.id]);
  }
  return tier;
}

/**
 * Cables have no direction; the map draws each one from the upper end down to the lower end.
 * Cables between two nodes on the same tier (two core switches, say) are "peers": drawn side by
 * side and left out of the vertical layout.
 */
export function orient(links: TopologyLink[], tier: Map<string, number>): ([string, string] | null)[] {
  return links.map((l) => {
    const [ta, tb] = [tier.get(l.a) ?? 0, tier.get(l.b) ?? 0];
    if (ta === tb) return null;
    return ta < tb ? [l.a, l.b] : [l.b, l.a];
  });
}

export async function place(nodes: TopologyNode[], links: TopologyLink[], groupLabels: Map<string, string>): Promise<Placement> {
  const tier = tiers(nodes, links);
  const stack = stacks(nodes, links, tier);
  const stacked = new Set([...stack.values()].flat());
  const laid = nodes.filter((n) => !stacked.has(n.id));
  const frames = new Set(laid.map(groupOf).filter((g): g is string => g !== null));
  const grouped = frames.size > 1;
  // A device with a stack is laid out as tall as device and stack together.
  const box = (n: TopologyNode): ElkNode => {
    const leaves = stack.get(n.id)?.length ?? 0;
    if (!leaves) return { id: n.id, ...SIZE[n.kind] };
    return {
      id: n.id,
      width: Math.max(SIZE.device.width, STACK.indent + SIZE.leaf.width),
      height: SIZE.device.height + STACK.top + leaves * (SIZE.leaf.height + STACK.gap) - STACK.gap,
    };
  };

  const children: ElkNode[] = [];
  if (grouped) {
    for (const frame of frames) {
      children.push({
        id: frame,
        layoutOptions: { "elk.padding": GROUP_PADDING },
        children: laid.filter((n) => groupOf(n) === frame).map(box),
      });
    }
    children.push(...laid.filter((n) => groupOf(n) === null).map(box));
  } else {
    children.push(...laid.map(box));
  }
  const edges: ElkExtendedEdge[] = orient(links, tier).flatMap((pair, i) =>
    pair && !stacked.has(pair[0]) && !stacked.has(pair[1]) ? [{ id: `e${i}`, sources: [pair[0]], targets: [pair[1]] }] : [],
  );

  const result = await engine().layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.spacing.nodeNode": "28",
      "elk.layered.spacing.nodeNodeBetweenLayers": "64",
      "elk.spacing.componentComponent": "48",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
    },
    children,
    edges,
  });

  const placement: Placement = { nodes: new Map(), groups: [], stacked };
  for (const child of result.children ?? []) {
    if (child.children?.length || frames.has(child.id)) {
      placement.groups.push({
        id: child.id,
        label: groupLabels.get(child.id) ?? child.id,
        x: child.x ?? 0,
        y: child.y ?? 0,
        width: child.width ?? 0,
        height: child.height ?? 0,
      });
      for (const n of child.children ?? []) placement.nodes.set(n.id, { x: n.x ?? 0, y: n.y ?? 0, parent: child.id });
    } else {
      placement.nodes.set(child.id, { x: child.x ?? 0, y: child.y ?? 0 });
    }
  }
  for (const [parent, leaves] of stack) {
    const at = placement.nodes.get(parent);
    if (!at) continue;
    leaves.forEach((id, i) =>
      placement.nodes.set(id, {
        x: at.x + STACK.indent,
        y: at.y + SIZE.device.height + STACK.top + i * (SIZE.leaf.height + STACK.gap),
        parent: at.parent,
      }),
    );
  }
  return placement;
}
