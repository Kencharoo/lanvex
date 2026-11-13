import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { MoreHorizontal, Pencil, Plus, Tags, Trash2 } from "lucide-react";
import * as React from "react";
import { DataTable, Pagination, SearchInput, Toolbar, type Column } from "@/components/data-table";
import { PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { EmptyState, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Notice, StatusBadge } from "@/components/ui/misc";
import { useSession } from "@/app/session";
import { api, errorMessage } from "@/lib/api";
import { fieldError, useLookup, useNetBoxWrite, useUrlState } from "@/lib/hooks";
import type { Page, VLAN, WriteResult } from "@/lib/types";
import { NotConfigured, isNotConfigured } from "./networks";
import { useNetBoxLabel } from "@/lib/system";

const LIMIT = 50;
type VLANWrite = WriteResult & { vlan: VLAN };

export function VLANsPage() {
  const nbName = useNetBoxLabel();
  const { isAdmin } = useSession();
  const { values, set, offset } = useUrlState(["q", "site", "group", "status"] as const);
  const [editing, setEditing] = React.useState<VLAN | "new" | null>(null);
  const [deleting, setDeleting] = React.useState<VLAN | null>(null);
  const sites = useLookup("sites");
  const groups = useLookup("vlan-groups");

  const query = { q: values.q, site_id: values.site, group_id: values.group, status: values.status, limit: LIMIT, offset };
  const list = useQuery({
    queryKey: ["vlans", "list", query],
    queryFn: ({ signal }) => api<Page<VLAN>>("/vlans", { query, signal }),
    placeholderData: keepPreviousData,
  });
  const filtered = !!(values.q || values.site || values.group || values.status);

  const columns: Column<VLAN>[] = [
    { key: "vid", header: "VID", cell: (v) => <span className="addr font-medium">{v.vid}</span>, headerClassName: "w-20" },
    { key: "name", header: "Name", cell: (v) => <span className="font-medium">{v.name}</span> },
    { key: "status", header: "Status", cell: (v) => <StatusBadge value={v.status.value} label={v.status.label} /> },
    { key: "group", header: "Group", cell: (v) => v.group?.name ?? <span className="text-faint">—</span> },
    { key: "site", header: "Site", cell: (v) => v.site?.name ?? <span className="text-faint">—</span> },
    { key: "prefixes", header: "Prefixes", cell: (v) => <span className="tabular-nums">{v.prefix_count}</span> },
    { key: "desc", header: "Description", cell: (v) => v.description || <span className="text-faint">—</span>, className: "max-w-64 truncate text-muted" },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      headerClassName: "w-10",
      cell: (v) =>
        isAdmin ? (
          <Menu>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for VLAN ${v.vid}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              <MenuItem onSelect={() => setEditing(v)}>
                <Pencil /> Edit VLAN
              </MenuItem>
              <MenuSeparator />
              <MenuItem danger onSelect={() => setDeleting(v)}>
                <Trash2 /> Delete VLAN
              </MenuItem>
            </MenuContent>
          </Menu>
        ) : null,
    },
  ];

  return (
    <>
      <PageHeader
        title="VLANs"
        description={`802.1Q VLANs documented in ${nbName}. Assign them to networks and interfaces from their detail views.`}
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus /> New VLAN
            </Button>
          )
        }
      />
      {isNotConfigured(list.error) ? (
        <NotConfigured />
      ) : (
        <>
          <Toolbar>
            <SearchInput label="Search VLANs" placeholder="Search VID or name" value={values.q} onChange={(q) => set({ q })} />
            <Select aria-label="Site" className="w-auto" value={values.site} onChange={(e) => set({ site: e.target.value })}>
              <option value="">All sites</option>
              {sites.data?.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </Select>
            <Select aria-label="VLAN group" className="w-auto" value={values.group} onChange={(e) => set({ group: e.target.value })}>
              <option value="">All groups</option>
              {groups.data?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.label}
                </option>
              ))}
            </Select>
            <Select aria-label="Status" className="w-auto" value={values.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="">Any status</option>
              <option value="active">Active</option>
              <option value="reserved">Reserved</option>
              <option value="deprecated">Deprecated</option>
            </Select>
            {filtered && (
              <Button variant="ghost" size="sm" onClick={() => set({ q: null, site: null, group: null, status: null })}>
                Clear filters
              </Button>
            )}
          </Toolbar>
          <DataTable
            columns={columns}
            rows={list.data?.results}
            rowKey={(v) => v.id}
            loading={list.isPending}
            error={list.error}
            onRetry={() => void list.refetch()}
            empty={
              <EmptyState
                icon={Tags}
                title={filtered ? "No VLANs match these filters" : "No VLANs yet"}
                action={
                  !filtered &&
                  isAdmin && (
                    <Button variant="primary" onClick={() => setEditing("new")}>
                      <Plus /> New VLAN
                    </Button>
                  )
                }
              >
                {filtered ? "Try a broader search." : "Create a VLAN record to associate with networks and interfaces."}
              </EmptyState>
            }
            footer={list.data && <Pagination count={list.data.count} limit={LIMIT} offset={offset} onChange={(o) => set({ offset: o })} noun="VLANs" />}
          />
        </>
      )}
      <VLANDialog vlan={editing} onClose={() => setEditing(null)} />
      <DeleteVLANDialog vlan={deleting} onClose={() => setDeleting(null)} />
    </>
  );
}

function VLANDialog({ vlan, onClose }: { vlan: VLAN | "new" | null; onClose: () => void }) {
  const nbName = useNetBoxLabel();
  const open = vlan !== null;
  const isNew = vlan === "new";
  const sites = useLookup("sites", undefined, open);
  const groups = useLookup("vlan-groups", undefined, open);
  const [f, setF] = React.useState({ vid: "", name: "", status: "active", site_id: "", group_id: "", description: "" });
  React.useEffect(() => {
    if (vlan === "new") setF({ vid: "", name: "", status: "active", site_id: "", group_id: "", description: "" });
    else if (vlan)
      setF({
        vid: String(vlan.vid),
        name: vlan.name,
        status: vlan.status.value,
        site_id: vlan.site ? String(vlan.site.id) : "",
        group_id: vlan.group ? String(vlan.group.id) : "",
        description: vlan.description,
      });
  }, [vlan]);
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const vidNum = Number(f.vid);
  const vidError = f.vid && (!Number.isInteger(vidNum) || vidNum < 1 || vidNum > 4094) ? "VLAN IDs range from 1 to 4094." : undefined;

  const save = useNetBoxWrite({
    fn: () => {
      const body = {
        vid: vidNum,
        name: f.name.trim(),
        status: f.status,
        site_id: f.site_id ? Number(f.site_id) : null,
        group_id: f.group_id ? Number(f.group_id) : null,
        description: f.description,
      };
      return isNew
        ? api<VLANWrite>("/vlans", { method: "POST", body })
        : api<VLANWrite>(`/vlans/${(vlan as VLAN).id}`, { method: "PATCH", body });
    },
    success: (r) => `${isNew ? "Created" : "Saved"} VLAN ${r.vlan.vid} ${r.vlan.name}`,
    invalidate: [["vlans"], ["lookup"]],
    onSuccess: onClose,
  });
  const err = save.error;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      {open && (
        <DialogContent title={isNew ? "New VLAN" : `Edit VLAN ${(vlan as VLAN).vid}`} description={`Saved as a VLAN record in ${nbName}. Switches are not configured.`}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!vidError && f.vid && f.name.trim()) save.mutate(undefined);
            }}
          >
            <DialogBody>
              <div className="grid gap-4 sm:grid-cols-[8rem_1fr]">
                <Field label="VLAN ID" htmlFor="vl-vid" error={vidError ?? fieldError(err, "vid")}>
                  <Input id="vl-vid" inputMode="numeric" className="addr" value={f.vid} onChange={(e) => set("vid", e.target.value.replace(/\D/g, ""))} required aria-invalid={!!vidError} autoFocus />
                </Field>
                <Field label="Name" htmlFor="vl-name" error={fieldError(err, "name")}>
                  <Input id="vl-name" value={f.name} onChange={(e) => set("name", e.target.value)} maxLength={64} required />
                </Field>
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label="Status" htmlFor="vl-status">
                  <Select id="vl-status" value={f.status} onChange={(e) => set("status", e.target.value)}>
                    <option value="active">Active</option>
                    <option value="reserved">Reserved</option>
                    <option value="deprecated">Deprecated</option>
                  </Select>
                </Field>
                <Field label="Group" htmlFor="vl-group" optional error={fieldError(err, "group", "group_id")}>
                  <Select id="vl-group" value={f.group_id} onChange={(e) => set("group_id", e.target.value)}>
                    <option value="">None</option>
                    {groups.data?.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Site" htmlFor="vl-site" optional error={fieldError(err, "site", "site_id")}>
                  <Select id="vl-site" value={f.site_id} onChange={(e) => set("site_id", e.target.value)}>
                    <option value="">None</option>
                    {sites.data?.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
              <Field label="Description" htmlFor="vl-desc" optional>
                <Input id="vl-desc" value={f.description} onChange={(e) => set("description", e.target.value)} maxLength={200} />
              </Field>
              {err && !fieldError(err, "vid", "name", "group", "group_id", "site", "site_id") && <Notice tone="danger">{errorMessage(err)}</Notice>}
            </DialogBody>
            <DialogFooter>
              <Button type="button" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" loading={save.isPending} disabled={!!vidError || !f.vid || !f.name.trim()}>
                {isNew ? "Create VLAN" : "Save changes"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}

function DeleteVLANDialog({ vlan, onClose }: { vlan: VLAN | null; onClose: () => void }) {
  const nbName = useNetBoxLabel();
  const del = useNetBoxWrite({
    fn: () => api<WriteResult>(`/vlans/${vlan!.id}`, { method: "DELETE" }),
    success: () => `Deleted VLAN ${vlan?.vid}`,
    invalidate: [["vlans"], ["lookup"], ["prefixes"]],
    onSuccess: onClose,
  });
  return (
    <Dialog open={!!vlan} onOpenChange={(o) => !o && onClose()}>
      {vlan && (
        <DialogContent title={`Delete VLAN ${vlan.vid} ${vlan.name}?`} description={`The VLAN record is removed from ${nbName}.`} role="alertdialog">
          <DialogBody>
            {vlan.prefix_count > 0 && (
              <Notice tone="warn">
                {vlan.prefix_count} prefix{vlan.prefix_count === 1 ? " references" : "es reference"} this VLAN. {nbName} will clear that link.
              </Notice>
            )}
            <p className="text-[13px] text-muted">Interfaces that carry this VLAN in {nbName} lose the association. No switch is reconfigured.</p>
            {del.error && <Notice tone="danger">{errorMessage(del.error)}</Notice>}
          </DialogBody>
          <DialogFooter>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="danger" onClick={() => del.mutate(undefined)} loading={del.isPending}>
              Delete VLAN
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}
