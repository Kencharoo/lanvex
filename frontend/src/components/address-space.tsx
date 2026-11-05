import { parseCidr } from "@/lib/cidr";
import type { Utilization } from "@/lib/types";
import { cn, formatCount, formatPercent } from "@/lib/utils";
import { Tooltip } from "./ui/misc";

const CELLS = 16;

/**
 * Utilization drawn as address blocks: the prefix split into 16 equal blocks, filled in order.
 * Reads as "how much of this space is taken" rather than as a generic progress bar.
 */
export function UtilStrip({ u, className }: { u: Utilization | null; className?: string }) {
  if (!u) return <span className="text-xs text-faint">—</span>;
  const filled = (u.percent / 100) * CELLS;
  const basis =
    u.basis === "child_prefixes"
      ? `${formatCount(u.used)} of ${formatCount(u.total)} addresses covered by child prefixes`
      : u.basis === "marked_utilized"
        ? "Marked as fully utilized in NetBox"
        : `${formatCount(u.used)} of ${formatCount(u.total)} usable addresses recorded`;
  return (
    <Tooltip content={basis}>
      <span className={cn("inline-flex items-center gap-2", className)} aria-label={`${formatPercent(u.percent)} utilized. ${basis}`}>
        <span className="flex gap-px" aria-hidden>
          {Array.from({ length: CELLS }, (_, i) => {
            const fill = Math.max(0, Math.min(1, filled - i));
            return (
              <span key={i} className="relative h-3 w-1.5 overflow-hidden rounded-[1px] bg-[var(--strip-free)]">
                <span
                  className={cn("absolute inset-x-0 bottom-0", u.percent >= 90 ? "bg-warn" : "bg-[var(--strip-used)]")}
                  style={{ height: `${fill * 100}%` }}
                />
              </span>
            );
          })}
        </span>
        <span className="w-10 text-right text-xs tabular-nums text-muted">{formatPercent(u.percent)}</span>
      </span>
    </Tooltip>
  );
}

export interface MapSegment {
  cidr: string;
  kind: "child" | "free";
  label?: string;
  onClick?: () => void;
}

function span(parentCidr: string, cidr: string): { left: number; width: number } | null {
  const parent = parseCidr(parentCidr);
  const child = parseCidr(cidr);
  if (!parent.ok || !child.ok || parent.value.family !== child.value.family) return null;
  const scale = 1_000_000n;
  const left = Number(((child.value.network - parent.value.network) * scale) / parent.value.size) / 1e4;
  const width = Number((child.value.size * scale) / parent.value.size) / 1e4;
  if (left < 0 || left >= 100) return null;
  return { left, width: Math.max(width, 0.6) };
}

/** A to-scale map of a prefix: child prefixes and free blocks positioned by address. */
export function AddressMap({ prefix, segments }: { prefix: string; segments: MapSegment[] }) {
  const placed = segments
    .map((s) => ({ s, pos: span(prefix, s.cidr) }))
    .filter((x): x is { s: MapSegment; pos: { left: number; width: number } } => x.pos !== null);
  const parsed = parseCidr(prefix);
  return (
    <figure className="grid gap-2">
      <div className="relative h-9 overflow-hidden rounded-[var(--radius-control)] border border-line bg-surface-2">
        {placed.map(({ s, pos }) => {
          const block = (
            <button
              type="button"
              disabled={!s.onClick}
              onClick={s.onClick}
              aria-label={`${s.kind === "free" ? "Free" : "Child prefix"} ${s.cidr}${s.label ? `, ${s.label}` : ""}`}
              className={cn(
                "absolute inset-y-0 border-r border-surface text-left focus-visible:z-10",
                s.kind === "child"
                  ? "bg-[var(--strip-child)] hover:bg-[var(--strip-used)] disabled:hover:bg-[var(--strip-child)]"
                  : "bg-surface bg-[repeating-linear-gradient(135deg,var(--strip-free)_0_3px,transparent_3px_7px)] hover:bg-accent-soft",
              )}
              style={{ left: `${pos.left}%`, width: `${pos.width}%` }}
            >
              {pos.width > 14 && (
                <span className={cn("addr block truncate px-1.5 text-[11px]", s.kind === "child" ? "text-accent-ink dark:text-ink" : "text-muted")}>
                  {s.cidr}
                </span>
              )}
            </button>
          );
          return (
            <Tooltip key={s.cidr} content={`${s.kind === "free" ? "Free" : s.label || "Child prefix"}: ${s.cidr}`}>
              {block}
            </Tooltip>
          );
        })}
      </div>
      <figcaption className="flex justify-between text-xs text-muted">
        <span className="addr text-xs">{parsed.ok ? prefix.split("/")[0] : ""}</span>
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm bg-[var(--strip-child)]" aria-hidden /> Child prefixes
          </span>
          <span className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm border border-line bg-[repeating-linear-gradient(135deg,var(--strip-free)_0_2px,transparent_2px_4px)]" aria-hidden /> Free
          </span>
        </span>
      </figcaption>
    </figure>
  );
}
