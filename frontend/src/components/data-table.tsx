import { ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { Notice, Skeleton } from "@/components/ui/misc";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: React.ReactNode;
  cell: (row: T) => React.ReactNode;
  className?: string;
  headerClassName?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string | number;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  empty?: React.ReactNode;
  onRowClick?: (row: T) => void;
  rowLabel?: (row: T) => string;
  selectedKey?: string | number | null;
  footer?: React.ReactNode;
  dense?: boolean;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading,
  error,
  onRetry,
  empty,
  onRowClick,
  rowLabel,
  selectedKey,
  footer,
  dense,
}: DataTableProps<T>) {
  const cellPad = dense ? "px-3 py-1.5" : "px-3 py-2.5";
  return (
    <div className="overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-line bg-surface-2/60 text-left">
              {columns.map((c) => (
                <th key={c.key} scope="col" className={cn("h-8 px-3 text-xs font-medium text-muted", c.headerClassName)}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading &&
              Array.from({ length: 6 }).map((_, i) => (
                <tr key={`sk-${i}`} className="border-b border-line last:border-0">
                  {columns.map((c) => (
                    <td key={c.key} className={cellPad}>
                      <Skeleton className="h-4 w-[70%]" />
                    </td>
                  ))}
                </tr>
              ))}
            {!loading &&
              rows?.map((row) => {
                const key = rowKey(row);
                return (
                  <tr
                    key={key}
                    className={cn(
                      "border-b border-line last:border-0",
                      onRowClick && "cursor-pointer hover:bg-surface-2/70",
                      selectedKey === key && "bg-accent-soft/60 hover:bg-accent-soft/60",
                    )}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    onKeyDown={
                      onRowClick
                        ? (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              onRowClick(row);
                            }
                          }
                        : undefined
                    }
                    tabIndex={onRowClick ? 0 : undefined}
                    title={onRowClick && rowLabel ? rowLabel(row) : undefined}
                  >
                    {columns.map((c) => (
                      <td key={c.key} className={cn(cellPad, "align-middle", c.className)}>
                        {c.cell(row)}
                      </td>
                    ))}
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
      {!loading && error ? (
        <div className="p-4">
          <Notice
            tone="danger"
            title="Could not load data"
            action={
              onRetry && (
                <Button size="sm" onClick={onRetry}>
                  <RefreshCw /> Retry
                </Button>
              )
            }
          >
            {errorMessage(error)}
          </Notice>
        </div>
      ) : null}
      {!loading && !error && rows?.length === 0 && empty}
      {footer}
    </div>
  );
}

export function Pagination({
  count,
  limit,
  offset,
  onChange,
  noun = "items",
}: {
  count: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
  noun?: string;
}) {
  if (count === 0) return null;
  const from = offset + 1;
  const to = Math.min(offset + limit, count);
  return (
    <div className="flex items-center justify-between border-t border-line px-3 py-2 text-xs text-muted">
      <span>
        {from.toLocaleString()}–{to.toLocaleString()} of {count.toLocaleString()} {noun}
      </span>
      <div className="flex gap-1">
        <Button size="icon-sm" variant="ghost" aria-label="Previous page" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          <ChevronLeft />
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Next page" disabled={to >= count} onClick={() => onChange(offset + limit)}>
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

export function SearchInput({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  label: string;
}) {
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => setDraft(value), [value]);
  React.useEffect(() => {
    const t = setTimeout(() => draft !== value && onChange(draft), 250);
    return () => clearTimeout(t);
  }, [draft, value, onChange]);
  return (
    <div className="relative w-full max-w-72">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
      <Input
        type="search"
        aria-label={label}
        className="pl-8"
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
    </div>
  );
}

export function Toolbar({ children }: { children: React.ReactNode }) {
  return <div className="mb-3 flex flex-wrap items-center gap-2">{children}</div>;
}
