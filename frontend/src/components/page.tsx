import { ChevronRight } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";

export function PageHeader({
  title,
  description,
  actions,
  crumbs,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  crumbs?: { label: string; to?: string }[];
}) {
  return (
    <header className="mb-5">
      {crumbs && crumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="mb-2">
          <ol className="flex items-center gap-1 text-xs text-muted">
            {crumbs.map((c, i) => (
              <li key={`${c.label}-${i}`} className="flex items-center gap-1">
                {i > 0 && <ChevronRight className="size-3 text-faint" aria-hidden />}
                {c.to ? (
                  <Link to={c.to} className="hover:text-ink">
                    {c.label}
                  </Link>
                ) : (
                  <span aria-current="page" className="text-ink">
                    {c.label}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">{title}</h1>
          {description && <p className="mt-1 max-w-[68ch] text-[13px] text-muted">{description}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}

export function Section({
  title,
  actions,
  children,
  description,
}: {
  title: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  description?: React.ReactNode;
}) {
  const id = React.useId();
  return (
    <section className="grid content-start gap-2.5" aria-labelledby={id}>
      <div className="flex items-end justify-between gap-2">
        <div>
          <h2 id={id} className="text-[15px] font-semibold">
            {title}
          </h2>
          {description && <p className="text-xs text-muted">{description}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** Two-column definition list used in detail panels. */
export function Facts({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(0,9rem)_1fr] gap-x-4 gap-y-2 text-[13px]">
      {items.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v ?? <span className="text-faint">—</span>}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
