import { useQuery } from "@tanstack/react-query";
import { ExternalLink, MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import * as React from "react";
import { Link } from "react-router";
import { AddressMap, UtilStrip, type MapSegment } from "@/components/address-space";
import { Facts, Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, SheetContent } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Notice, Skeleton, StatusBadge } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError, useLookup, useNetBoxWrite } from "@/lib/hooks";
import type { AvailableIP, AvailablePrefix, IPAddress, Page, Prefix, PrefixStatus, WriteResult } from "@/lib/types";
import { absoluteTime, formatCount, relativeTime } from "@/lib/utils";
import { useNetBoxLabel } from "@/lib/system";

export function PrefixSheet({ id, onClose, onSelect }: { id: number; onClose: () => void; onSelect: (id: number) => void }) {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const [editing, setEditing] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const q = useQuery({ queryKey: ["prefixes", "detail", id], queryFn: () => api<Prefix>(`/prefixes/${id}`) });
  const p = q.data;

  const children = useQuery({
    queryKey: ["prefixes", "children", id, p?.prefix, p?.vrf?.id],
    queryFn: () =>
      api<Page<Prefix>>("/prefixes", {
        query: { within: p!.prefix, vrf_id: p!.vrf?.id, global_only: !p!.vrf, limit: 200, include_utilization: false },
      }),
    enabled: !!p,
  });
  const free = useQuery({
    queryKey: ["prefixes", "free", id],
    queryFn: () => api<AvailablePrefix[]>(`/prefixes/${id}/available-prefixes`),
    enabled: !!p,
  });
  const nextIps = useQuery({
    queryKey: ["prefixes", "next-ips", id],
    queryFn: () => api<AvailableIP[]>(`/prefixes/${id}/available-ips`, { query: { limit: 5 } }),
    enabled: !!p && p.status.value !== "container",
  });

  const allocate = useNetBoxWrite({
    fn: () => api<WriteResult & { ip_address: IPAddress }>(`/prefixes/${id}/allocate-ip`, { method: "POST", body: { status: "reserved" } }),
    success: (r) => `Reserved ${r.ip_address.address}`,
    invalidate: [["prefixes"], ["ip-addresses"]],
  });

  const direct = (children.data?.results ?? []).filter((c) => c.id !== id && c.depth === (p?.depth ?? 0) + 1);
  // Free *prefix* space is only meaningful where the space is subdivided; a leaf network's
  // occupancy is its IP addresses, which the strip above already shows.
  const showMap = p?.status.value === "container" || direct.length > 0;
  const segments: MapSegment[] = [
    ...direct.map((c) => ({ cidr: c.prefix, kind: "child" as const, label: c.name, onClick: () => onSelect(c.id) })),
    ...(free.data ?? []).map((f) => ({ cidr: f.prefix, kind: "free" as const })),
  ];

  return (
    <>
      <SheetContent
        title={p ? <span className="addr text-base">{p.prefix}</span> : <Skeleton className="h-5 w-40" />}
        description={p ? p.name || "No name" : undefined}
      >
        {q.isPending && (
          <div className="grid gap-3 p-5">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        )}
        {q.error && (
          <div className="p-5">
            <Notice tone="danger" title="Could not load this prefix">
              {errorMessage(q.error)}
            </Notice>
          </div>
        )}
        {p && (
          <div className="grid gap-6 p-5">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge value={p.status.value} label={p.status.label} />
              {p.is_pool && <span className="text-xs text-muted">Pool</span>}
              <div className="ml-auto flex gap-2">
                {p.meta.netbox_url && (
                  <Button asChild size="sm" variant="ghost">
                    <a href={p.meta.netbox_url} target="_blank" rel="noreferrer">
                      <ExternalLink /> Open in {nbName}
                    </a>
                  </Button>
                )}
                {isAdmin && (
                  <Menu>
                    <MenuTrigger asChild>
                      <Button size="icon-sm" variant="secondary" aria-label="Prefix actions">
                        <MoreHorizontal />
                      </Button>
                    </MenuTrigger>
                    <MenuContent>
                      <MenuItem onSelect={() => setEditing(true)}>
                        <Pencil /> Edit details
                      </MenuItem>
                      <MenuSeparator />
                      <MenuItem danger onSelect={() => setDeleting(true)}>
                        <Trash2 /> Delete prefix
                      </MenuItem>
                    </MenuContent>
                  </Menu>
                )}
              </div>
            </div>

            <Section
              title="Address space"
              description={p.utilization?.basis === "child_prefixes" ? "Coverage by child prefixes." : `Addresses recorded in ${nbName}, out of usable addresses.`}
            >
              <div className="flex items-center justify-between">
                <UtilStrip u={p.utilization} />
                {p.utilization && (
                  <span className="text-xs text-muted">
                    {formatCount(p.utilization.used)} of {formatCount(p.utilization.total)}
                  </span>
                )}
              </div>
              {children.isPending || free.isPending ? (
                <Skeleton className="h-9 w-full" />
              ) : showMap ? (
                <AddressMap prefix={p.prefix} segments={segments} />
              ) : null}
              {showMap && free.data && free.data.length > 0 && (
                <p className="text-xs text-muted">
                  Largest free block: <span className="addr text-xs text-ink">{[...free.data].sort((a, b) => b.size - a.size)[0].prefix}</span>
                  {free.data.length > 1 && ` (${free.data.length} free blocks)`}
                </p>
              )}
            </Section>

            {p.status.value !== "container" && (
              <Section
                title="Next available addresses"
                actions={
                  isAdmin && (nextIps.data?.length ?? 0) > 0 ? (
                    <Button size="sm" onClick={() => allocate.mutate(undefined)} loading={allocate.isPending}>
                      <Plus /> Reserve next
                    </Button>
                  ) : undefined
                }
              >
                {nextIps.isPending ? (
                  <Skeleton className="h-4 w-1/2" />
                ) : nextIps.data?.length ? (
                  <ul className="flex flex-wrap gap-1.5">
                    {nextIps.data.map((a) => (
                      <li key={a.address} className="addr rounded border border-line bg-surface-2/60 px-1.5 py-0.5 text-xs">
                        {a.address}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[13px] text-muted">No free addresses remain.</p>
                )}
                {allocate.error && <Notice tone="danger">{errorMessage(allocate.error)}</Notice>}
              </Section>
            )}

            <Section title="Details">
              <Facts
                items={[
                  ["VRF", p.vrf?.name ?? "Global table"],
                  ["VLAN", p.vlan ? `${p.vlan.name} (${p.vlan.vid})` : null],
                  ["Gateway", p.gateway ? <span className="addr">{p.gateway.address}</span> : null],
                  ["Role", p.role?.name],
                  ["Tenant", p.tenant?.name],
                  ["Scope", p.scope ? `${p.scope.name}` : null],
                  ["Child prefixes", p.children ? String(p.children) : "0"],
                  [
                    "IP addresses",
                    <Link key="ips" to={`/ip-addresses?parent=${encodeURIComponent(p.prefix)}${p.vrf ? `&vrf_id=${p.vrf.id}` : "&global_only=1"}`} className="text-accent hover:underline">
                      View addresses in this prefix
                    </Link>,
                  ],
                  ["Last updated", <span key="upd" title={absoluteTime(p.meta.last_updated)}>{relativeTime(p.meta.last_updated)}</span>],
                ]}
              />
              {p.comments && <p className="whitespace-pre-wrap rounded-[var(--radius-control)] bg-surface-2/60 p-3 text-[13px]">{p.comments}</p>}
            </Section>
          </div>
        )}
      </SheetContent>
      {p && <EditPrefixDialog prefix={p} open={editing} onOpenChange={setEditing} />}
      {p && <DeletePrefixDialog prefix={p} open={deleting} onOpenChange={setDeleting} onDeleted={onClose} />}
    </>
  );
}

function EditPrefixDialog({ prefix, open, onOpenChange }: { prefix: Prefix; open: boolean; onOpenChange: (o: boolean) => void }) {
  const nbName = useNetBoxLabel();
  const vlans = useLookup("vlans", undefined, open);
  const roles = useLookup("prefix-roles", undefined, open);
  const sites = useLookup("sites", undefined, open);
  // A scope other than a site (a region, a location) is kept as it is; it is edited in NetBox.
  const otherScope = prefix.scope_type && prefix.scope_type !== "dcim.site" ? prefix.scope : null;
  const [form, setForm] = React.useState(() => initial(prefix));
  React.useEffect(() => {
    if (open) setForm(initial(prefix));
  }, [open, prefix]);
  const set = (k: keyof ReturnType<typeof initial>, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const save = useNetBoxWrite({
    fn: () => {
      const body: Record<string, unknown> = {
        name: form.name,
        status: form.status,
        vlan_id: form.vlan_id ? Number(form.vlan_id) : null,
        role_id: form.role_id ? Number(form.role_id) : null,
        comments: form.comments,
      };
      if (!otherScope && form.site_id !== initial(prefix).site_id) body.site_id = form.site_id ? Number(form.site_id) : null;
      if (form.status !== "container") {
        if (form.gateway.trim() && form.gateway.trim() !== prefix.gateway?.address) body.gateway = form.gateway.trim();
        if (!form.gateway.trim() && prefix.gateway) body.clear_gateway = true;
      }
      return api<WriteResult & { prefix: Prefix }>(`/prefixes/${prefix.id}`, { method: "PATCH", body });
    },
    success: (r) => `Saved ${r.prefix.prefix}`,
    invalidate: [["prefixes"]],
    onSuccess: () => onOpenChange(false),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={`Edit ${prefix.prefix}`} description={`Changes are saved to ${nbName}.`}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(undefined);
          }}
        >
          <DialogBody>
            <Field label="Name" htmlFor="ep-name" error={fieldError(save.error, "name", "description")}>
              <Input id="ep-name" value={form.name} onChange={(e) => set("name", e.target.value)} maxLength={200} />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Status" htmlFor="ep-status">
                <Select id="ep-status" value={form.status} onChange={(e) => set("status", e.target.value)}>
                  <option value="active">Active</option>
                  <option value="reserved">Reserved</option>
                  <option value="container">Container</option>
                  <option value="deprecated">Deprecated</option>
                </Select>
              </Field>
              <Field label="Role" htmlFor="ep-role" optional>
                <Select id="ep-role" value={form.role_id} onChange={(e) => set("role_id", e.target.value)}>
                  <option value="">None</option>
                  {roles.data?.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            {otherScope ? (
              <p className="text-[13px] text-muted">
                Scope: {otherScope.name} ({prefix.scope_type?.replace("dcim.", "")}). Change it in {nbName}.
              </p>
            ) : (
              <Field label="Site" htmlFor="ep-site" optional error={fieldError(save.error, "site_id", "scope_id")}>
                <Select id="ep-site" value={form.site_id} onChange={(e) => set("site_id", e.target.value)}>
                  <option value="">No site</option>
                  {sites.data?.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.label}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
            <Field label="VLAN" htmlFor="ep-vlan" optional error={fieldError(save.error, "vlan_id", "vlan")}>
              <Select id="ep-vlan" value={form.vlan_id} onChange={(e) => set("vlan_id", e.target.value)}>
                <option value="">No VLAN</option>
                {vlans.data?.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </Field>
            {form.status !== "container" && (
              <Field label="Gateway" htmlFor="ep-gw" optional error={fieldError(save.error, "gateway")} hint="Leave empty to remove the gateway marker. The IP address record is kept.">
                <Input id="ep-gw" className="addr" value={form.gateway} onChange={(e) => set("gateway", e.target.value)} spellCheck={false} />
              </Field>
            )}
            <Field label="Comments" htmlFor="ep-comments" optional>
              <Textarea id="ep-comments" value={form.comments} onChange={(e) => set("comments", e.target.value)} />
            </Field>
            {save.error && !fieldError(save.error, "name", "description", "vlan_id", "vlan", "gateway") && (
              <Notice tone="danger">{errorMessage(save.error)}</Notice>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function initial(p: Prefix) {
  return {
    name: p.name,
    status: p.status.value as PrefixStatus,
    vlan_id: p.vlan ? String(p.vlan.id) : "",
    role_id: p.role ? String(p.role.id) : "",
    site_id: p.scope_type === "dcim.site" && p.scope ? String(p.scope.id) : "",
    gateway: p.gateway?.address ?? "",
    comments: p.comments,
  };
}

function DeletePrefixDialog({
  prefix,
  open,
  onOpenChange,
  onDeleted,
}: {
  prefix: Prefix;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onDeleted: () => void;
}) {
  const nbName = useNetBoxLabel();
  const [confirm, setConfirm] = React.useState("");
  React.useEffect(() => setConfirm(""), [open]);
  const impact = useQuery({
    queryKey: ["prefixes", "impact", prefix.id],
    queryFn: () => api<{ prefix: string; child_prefixes: number; ip_addresses: number }>(`/prefixes/${prefix.id}/delete-impact`),
    enabled: open,
  });
  const del = useNetBoxWrite({
    fn: () => api<WriteResult>(`/prefixes/${prefix.id}`, { method: "DELETE", query: { confirm } }),
    success: () => `Deleted ${prefix.prefix}`,
    invalidate: [["prefixes"]],
    onSuccess: () => {
      onOpenChange(false);
      onDeleted();
    },
  });
  const matches = confirm.trim() === prefix.prefix;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={`Delete ${prefix.prefix}?`} description={`This removes the prefix record from ${nbName}.`} role="alertdialog">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (matches) del.mutate(undefined);
          }}
        >
          <DialogBody>
            {impact.isPending ? (
              <Skeleton className="h-12 w-full" />
            ) : impact.data ? (
              <Notice tone="warn" title="What happens">
                {impact.data.child_prefixes > 0 && (
                  <>
                    <strong>{impact.data.child_prefixes}</strong> child prefix{impact.data.child_prefixes === 1 ? "" : "es"} will move up to the next parent.{" "}
                  </>
                )}
                {impact.data.ip_addresses > 0 ? (
                  <>
                    <strong>{impact.data.ip_addresses}</strong> IP address record{impact.data.ip_addresses === 1 ? "" : "s"} inside it will stay in {nbName}.
                  </>
                ) : (
                  "No IP address records are inside it."
                )}{" "}
                Devices are not changed.
              </Notice>
            ) : null}
            <Field label={`Type ${prefix.prefix} to confirm`} htmlFor="del-confirm" error={fieldError(del.error, "confirm")}>
              <Input id="del-confirm" className="addr" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" spellCheck={false} autoFocus />
            </Field>
            {del.error && !fieldError(del.error, "confirm") && <Notice tone="danger">{errorMessage(del.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" disabled={!matches} loading={del.isPending}>
              Delete prefix
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
