import { Check, Copy } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for non-secure contexts (plain HTTP on a LAN).
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = React.useState(false);
  return (
    <Button
      type="button"
      size="sm"
      variant="secondary"
      onClick={async () => {
        if (await copyText(text)) {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } else {
          toast.error("Copy failed. Select the text and copy it manually.");
        }
      }}
    >
      {done ? <Check /> : <Copy />} {done ? "Copied" : label}
    </Button>
  );
}

/** A block of text meant to be copied exactly (a key, a command). Wraps rather than scrolls. */
export function CodeBlock({ text, label, className }: { text: string; label?: string; className?: string }) {
  return (
    <div className={cn("grid gap-2 rounded-[var(--radius-control)] border border-line bg-surface-2/60 p-3", className)}>
      <code className="addr block whitespace-pre-wrap text-xs leading-relaxed [overflow-wrap:anywhere]">{text}</code>
      <div className="flex justify-end">
        <CopyButton text={text} label={label} />
      </div>
    </div>
  );
}
