import { AlertTriangle, Info, XCircle } from "lucide-react";
import { DropdownMenu as DM, Tooltip as T } from "radix-ui";
import * as React from "react";
import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded bg-surface-2", className)} {...props} />;
}

const toneClasses = {
  neutral: "bg-surface-2 text-muted",
  accent: "bg-accent-soft text-accent",
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
} as const;
export type Tone = keyof typeof toneClasses;

export function Badge({ tone = "neutral", className, ...props }: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn("inline-flex h-5 items-center gap-1 rounded px-1.5 text-xs font-medium", toneClasses[tone], className)}
      {...props}
    />
  );
}

const statusTone: Record<string, Tone> = {
  active: "ok",
  container: "neutral",
  reserved: "warn",
  deprecated: "danger",
  dhcp: "info",
  slaac: "info",
  planned: "accent",
  staged: "accent",
  offline: "neutral",
  failed: "danger",
  inventory: "neutral",
  decommissioning: "warn",
};

/** Documented NetBox status. Never used to imply live operational state. */
export function StatusBadge({ value, label }: { value: string; label?: string }) {
  const tone = statusTone[value] ?? "neutral";
  return (
    <Badge tone={tone}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {label ?? value}
    </Badge>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
  className,
  action,
}: {
  tone?: "info" | "warn" | "danger";
  title?: string;
  children?: React.ReactNode;
  className?: string;
  action?: React.ReactNode;
}) {
  const Icon = tone === "danger" ? XCircle : tone === "warn" ? AlertTriangle : Info;
  const styles = {
    info: "border-line bg-surface-2/60 text-ink [&_svg]:text-accent",
    warn: "border-warn/30 bg-warn-soft text-ink [&_svg]:text-warn",
    danger: "border-danger/30 bg-danger-soft text-ink [&_svg]:text-danger",
  }[tone];
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex gap-2.5 rounded-[var(--radius-control)] border px-3 py-2.5 text-[13px]", styles, className)}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="grid flex-1 gap-0.5">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className="text-muted [&_strong]:text-ink">{children}</div>}
      </div>
      {action}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center">
      <div className="mb-3 grid size-10 place-items-center rounded-full border border-line bg-surface-2 text-muted">
        <Icon className="size-5" />
      </div>
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 max-w-sm text-[13px] text-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export const TooltipProvider = T.Provider;

export function Tooltip({ content, children, side = "top" }: { content: React.ReactNode; children: React.ReactElement; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <T.Root>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content
          side={side}
          sideOffset={6}
          className="z-50 max-w-xs rounded-md bg-ink px-2 py-1 text-xs text-canvas shadow-pop"
        >
          {content}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}

export const Menu = DM.Root;
export const MenuTrigger = DM.Trigger;

export function MenuContent({ className, ...props }: React.ComponentProps<typeof DM.Content>) {
  return (
    <DM.Portal>
      <DM.Content
        sideOffset={4}
        align="end"
        className={cn("z-50 min-w-44 rounded-[var(--radius-control)] border border-line bg-surface p-1 shadow-pop", className)}
        {...props}
      />
    </DM.Portal>
  );
}

export function MenuItem({ className, danger, ...props }: React.ComponentProps<typeof DM.Item> & { danger?: boolean }) {
  return (
    <DM.Item
      className={cn(
        "flex cursor-default select-none items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2 [&_svg]:size-4 [&_svg]:text-muted",
        danger && "text-danger [&_svg]:text-danger",
        className,
      )}
      {...props}
    />
  );
}

export function MenuSeparator() {
  return <DM.Separator className="my-1 h-px bg-line" />;
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="rounded border border-line bg-surface-2 px-1 font-sans text-2xs text-muted">{children}</kbd>;
}
