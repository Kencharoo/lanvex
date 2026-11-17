import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { EthernetPort, Globe, Server } from "lucide-react";
import type { TopologyNode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { SIZE } from "./layout";

export type DeviceData = { node: TopologyNode; dim: boolean; match: boolean; leaf?: boolean };
export type SiteData = { label: string; width: number; height: number };
export type DeviceFlowNode = Node<DeviceData, "device">;
export type CircuitFlowNode = Node<DeviceData, "circuit">;
export type SiteFlowNode = Node<SiteData, "site">;

// Cables attach at the centre of the bottom (down to lower tiers) and the top (from upper tiers), or
// at the sides between nodes on the same tier. The handles are invisible and not draggable.
const hidden = "!pointer-events-none !opacity-0";
function Anchors() {
  return (
    <>
      <Handle id="t" type="target" position={Position.Top} isConnectable={false} className={hidden} />
      <Handle id="b" type="source" position={Position.Bottom} isConnectable={false} className={hidden} />
      <Handle id="l" type="target" position={Position.Left} isConnectable={false} className={hidden} />
      <Handle id="r" type="source" position={Position.Right} isConnectable={false} className={hidden} />
      {/* For a stack of end devices underneath: a line down the left edge into each one's left side. */}
      <Handle id="bl" type="source" position={Position.Bottom} isConnectable={false} className={hidden} style={{ left: 12 }} />
    </>
  );
}

const QUIET = new Set(["active", "connected"]);

export function DeviceNode({ data, selected }: NodeProps<DeviceFlowNode>) {
  const n = data.node;
  const sub = n.external ? "Another site" : [n.role?.name, n.device_type].filter(Boolean).join(" · ");
  const size = data.leaf ? SIZE.leaf : SIZE.device;
  return (
    <div
      style={{ width: size.width, height: size.height, "--role": n.color ?? "var(--line-strong)" } as React.CSSProperties}
      className={cn(
        "relative flex items-center gap-2.5 overflow-hidden rounded-[10px] border bg-surface pr-3 text-left shadow-sm transition-[opacity,box-shadow]",
        n.external ? "border-dashed border-line-strong" : "border-line",
        selected && "ring-2 ring-[var(--ring)] border-accent",
        data.match && !selected && "ring-2 ring-[var(--warn)]",
        data.dim && "opacity-35",
      )}
      title={n.name}
    >
      <Anchors />
      <span aria-hidden className="h-full w-1.5 shrink-0 bg-[var(--role)]" />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-ink">{n.name}</span>
          {n.status && !QUIET.has(n.status.value) && (
            <span className="shrink-0 rounded-full bg-surface-2 px-1.5 text-[10px] leading-4 text-muted">{n.status.label}</span>
          )}
        </span>
        {!data.leaf && <span className="block truncate text-[11px] text-muted">{sub || "Device"}</span>}
      </span>
      {n.server_id && (
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-accent-soft text-accent" title="Lanvex reads this server">
          <Server className="size-3.5" aria-label="Lanvex server" />
        </span>
      )}
      {n.switch_id && (
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-accent-soft text-accent" title="Lanvex reads this switch">
          <EthernetPort className="size-3.5" aria-label="Lanvex switch" />
        </span>
      )}
    </div>
  );
}

export function CircuitNode({ data, selected }: NodeProps<CircuitFlowNode>) {
  const n = data.node;
  return (
    <div
      style={SIZE.circuit}
      className={cn(
        "flex items-center gap-2 rounded-full border border-dashed border-line-strong bg-surface-2 px-3 shadow-sm transition-opacity",
        selected && "ring-2 ring-[var(--ring)] border-accent",
        data.match && !selected && "ring-2 ring-[var(--warn)]",
        data.dim && "opacity-35",
      )}
      title={`${n.name}${n.provider ? ` (${n.provider})` : ""}`}
    >
      <Anchors />
      <Globe className="size-4 shrink-0 text-muted" aria-hidden />
      <span className="min-w-0">
        <span className="block truncate text-[12px] font-medium text-ink">{n.name}</span>
        <span className="block truncate text-[10.5px] text-muted">{n.provider ?? "Circuit"}</span>
      </span>
    </div>
  );
}

export function SiteNode({ data }: NodeProps<SiteFlowNode>) {
  return (
    <div style={{ width: data.width, height: data.height }} className="rounded-[14px] border border-line bg-surface-2/60">
      <span className="absolute left-4 top-3 text-[12px] font-semibold uppercase tracking-wide text-muted">{data.label}</span>
    </div>
  );
}

export const nodeTypes = { device: DeviceNode, circuit: CircuitNode, site: SiteNode };
