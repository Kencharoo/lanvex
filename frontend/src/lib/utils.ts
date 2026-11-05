import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const whole = new Intl.NumberFormat();

/** Address counts get huge in IPv6; keep them readable. */
export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e15) return `2^${Math.round(Math.log2(n))}`;
  return n >= 100_000 ? compact.format(n) : whole.format(n);
}

export function formatPercent(p: number): string {
  if (p === 0) return "0%";
  if (p > 0 && p < 0.1) return "<0.1%";
  return `${p >= 10 ? Math.round(p) : p.toFixed(1)}%`;
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
  return new Date(iso).toLocaleDateString();
}

export function absoluteTime(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}
