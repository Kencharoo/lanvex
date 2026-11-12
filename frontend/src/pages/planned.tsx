import type { LucideIcon } from "lucide-react";
import { Share2 } from "lucide-react";
import { PageHeader } from "@/components/page";

/** Honest placeholders for phase-2+ areas: what it will do and what it needs. No fake controls. */
function Planned({ title, icon: Icon, summary, needs }: { title: string; icon: LucideIcon; summary: string; needs: string[] }) {
  return (
    <>
      <PageHeader title={title} description="Not available in this version of Lanvex." />
      <div className="grid max-w-2xl gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-6">
        <div className="flex items-center gap-3">
          <span className="grid size-9 place-items-center rounded-full border border-line bg-surface-2 text-muted">
            <Icon className="size-4" />
          </span>
          <p className="text-[13px] text-ink">{summary}</p>
        </div>
        <div>
          <p className="mb-1.5 text-[13px] font-medium">Before it ships</p>
          <ul className="grid list-disc gap-1 pl-5 text-[13px] text-muted">
            {needs.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}

export const TopologyPage = () => (
  <Planned
    title="Topology"
    icon={Share2}
    summary="An interactive map of routers, switches and servers, drawn from cables and interface connections documented in NetBox."
    needs={["Cable and link traces from NetBox's dcim API.", "Layout that stays readable beyond a few hundred devices."]}
  />
);
