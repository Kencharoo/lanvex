import { cn } from "@/lib/utils";

/** The Lanvex mark: two nodes linking down to a shared vertex. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("size-6", className)} aria-hidden>
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path d="M10 10.5 16 21.5 22 10.5" fill="none" stroke="var(--accent-ink)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="10" cy="10.5" r="2.6" fill="var(--accent-ink)" />
      <circle cx="22" cy="10.5" r="2.6" fill="var(--accent-ink)" />
      <circle cx="16" cy="21.5" r="3.4" fill="var(--accent-ink)" />
    </svg>
  );
}

export function Wordmark({ collapsed }: { collapsed?: boolean }) {
  return (
    <span className="flex items-center gap-2">
      <LogoMark />
      {!collapsed && <span className="text-[15px] font-semibold tracking-[-0.015em]">Lanvex</span>}
    </span>
  );
}
