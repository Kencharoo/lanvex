import { X } from "lucide-react";
import { Dialog as D } from "radix-ui";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

function Overlay() {
  return (
    <D.Overlay className="fixed inset-0 z-50 bg-[rgb(10_14_20/40%)] backdrop-blur-[1px] data-[state=open]:animate-[fade-in_120ms_ease-out]" />
  );
}

interface ContentProps extends Omit<React.ComponentProps<typeof D.Content>, "title"> {
  title: string;
  description?: React.ReactNode;
  width?: string;
}

export function DialogContent({ title, description, width = "max-w-lg", className, children, ...props }: ContentProps) {
  return (
    <D.Portal>
      <Overlay />
      <D.Content
        className={cn(
          "fixed left-1/2 top-[12vh] z-50 w-[calc(100vw-2rem)] -translate-x-1/2 rounded-[var(--radius-panel)] border border-line bg-surface shadow-pop outline-none",
          "max-h-[80vh] overflow-y-auto data-[state=open]:animate-[pop-in_140ms_ease-out]",
          width,
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="grid gap-1">
            <D.Title className="text-base font-semibold">{title}</D.Title>
            {description ? (
              <D.Description className="text-[13px] text-muted">{description}</D.Description>
            ) : (
              <D.Description className="sr-only">{title}</D.Description>
            )}
          </div>
          <D.Close className="-mr-1 rounded p-1 text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close">
            <X className="size-4" />
          </D.Close>
        </div>
        {children}
      </D.Content>
    </D.Portal>
  );
}

export function DialogBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("grid gap-4 px-5 py-4", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex items-center justify-end gap-2 border-t border-line bg-surface-2/50 px-5 py-3", className)}
      {...props}
    />
  );
}

/** Right-hand detail panel. The list stays visible behind it. */
export function SheetContent({
  title,
  description,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<typeof D.Content>, "title"> & { title: React.ReactNode; description?: React.ReactNode }) {
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-40 bg-[rgb(10_14_20/18%)]" />
      <D.Content
        className={cn(
          "fixed inset-y-0 right-0 z-40 flex w-full max-w-[560px] flex-col border-l border-line bg-surface shadow-pop outline-none",
          "data-[state=open]:animate-[sheet-in_160ms_ease-out]",
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="grid min-w-0 gap-1">
            <D.Title className="truncate text-base font-semibold">{title}</D.Title>
            {description ? (
              <D.Description asChild>
                <div className="text-[13px] text-muted">{description}</div>
              </D.Description>
            ) : (
              <D.Description className="sr-only">Details</D.Description>
            )}
          </div>
          <D.Close className="-mr-1 rounded p-1 text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close panel">
            <X className="size-4" />
          </D.Close>
        </div>
        <div className="flex-1 overflow-y-auto">{children}</div>
      </D.Content>
    </D.Portal>
  );
}
