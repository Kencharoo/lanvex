import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Download, Network, Plus, Settings2, Trash2, Upload, X, XCircle } from "lucide-react";
import * as React from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { CodeBlock } from "@/components/copy";
import { Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { ChangeDetail, ChangeOut, ChangeReadiness, ChangeRequestBody, HelperInstall, HelperState, HostFacts, LanvexLink, LinkRequestBody, Server } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";
import { ChangeStatusBadge } from "../changes/shared";
import type { ChangeSeed } from "./record";

const HELPER_LABEL: Record<HelperState, string> = {
  unknown: "Not checked yet",
  ok: "Installed",
  outdated: "Outdated version",
  missing: "Not installed",
  no_sudo: "Not allowed through sudo",
  no_python: "Python 3 missing",
  timeout: "No answer",
  error: "Not usable",
};

export function ChangesSection({
  server: s,
  facts,
  isAdmin,
  seed = null,
  onSeedUsed,
}: {
  server: Server;
  facts: HostFacts | null;
  isAdmin: boolean;
  /** Start the change form from this (a change prepared from the comparison with NetBox). */
  seed?: ChangeSeed | null;
  onSeedUsed?: () => void;
}) {
  const qc = useQueryClient();
  const [dialog, setDialog] = React.useState<null | "install" | "change" | "link">(null);
  const [removing, setRemoving] = React.useState<LanvexLink | null>(null);
  React.useEffect(() => {
    if (seed) setDialog("change");
  }, [seed]);
  const readiness = useQuery({
    queryKey: ["servers", s.id, "changes", "readiness"],
    queryFn: () => api<ChangeReadiness>(`/servers/${s.id}/changes/readiness`),
  });
  const recent = useQuery({
    queryKey: ["changes", { server_id: s.id }],
    queryFn: () => api<ChangeOut[]>("/changes", { query: { server_id: s.id, limit: 5 } }),
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["servers", s.id] });
    void qc.invalidateQueries({ queryKey: ["changes"] });
  };
  const check = useMutation({
    mutationFn: () => api<ChangeReadiness>(`/servers/${s.id}/changes/helper/check`, { method: "POST" }),
    onSuccess: (r) => {
      (r.helper.state === "ok" ? toast.success : toast.error)(r.helper.message ?? HELPER_LABEL[r.helper.state]);
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api<ChangeReadiness>(`/servers/${s.id}/changes/enabled`, { method: "PUT", body: { enabled } }),
    onSuccess: (_, enabled) => {
      toast.success(enabled ? `Changes enabled for ${s.name}.` : `Changes disabled for ${s.name}.`);
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const r = readiness.data;
  return (
    <Section
      title="Configuration changes"
      actions={
        isAdmin &&
        r?.ready &&
        r.interface_changes !== false &&
        !r.open_change && (
          <>
            <Button variant="secondary" onClick={() => setDialog("link")} disabled={!facts}>
              <Network /> Add bridge or bond
            </Button>
            <Button variant="primary" onClick={() => setDialog("change")} disabled={!facts}>
              <Settings2 /> Change IP settings
            </Button>
          </>
        )
      }
    >
      {!r ? (
        <Skeleton className="h-32 w-full" />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="grid content-start gap-3 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
            <p className="text-[13px] font-medium">Requirements</p>
            <ul className="grid gap-2.5">
              <Requirement ok={r.automation_enabled} label="Changes enabled">
                {r.automation_enabled ? (
                  <>Rollback after {r.rollback_seconds} s{r.second_admin_required ? "; second approval required" : ""}.</>
                ) : (
                  <>
                    Set <span className="addr">LANVEX_AUTOMATION_ENABLED=true</span>.
                  </>
                )}
              </Requirement>
              <Requirement ok={r.stack_supported} label="Supported network stack">
                {r.stack_label ?? "Not read yet."}
                {r.interface_changes === false && r.stack_supported && " (VLAN networks only)"}
              </Requirement>
              <Requirement ok={r.helper.state === "ok" ? true : r.helper.state === "unknown" ? null : false} label={`Change helper: ${HELPER_LABEL[r.helper.state]}`}>
                <span className="grid gap-1.5">
                  <span>
                    {r.helper.message}
                    {r.helper.checked_at && <> Checked {relativeTime(r.helper.checked_at)}.</>}
                  </span>
                  {isAdmin && (
                    <span className="flex flex-wrap gap-2">
                      <Button size="sm" variant="secondary" onClick={() => setDialog("install")}>
                        {r.helper.state === "ok" ? "Install steps" : "Install the helper"}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => check.mutate()} loading={check.isPending}>
                        Check again
                      </Button>
                    </span>
                  )}
                </span>
              </Requirement>
              <Requirement ok={r.changes_enabled} label="Changes enabled for this server">
                <span className="grid gap-1.5">
                  <span>{r.changes_enabled ? "Administrators can prepare changes for this server." : "Off until an administrator turns it on."}</span>
                  {isAdmin && (
                    <span>
                      <Button
                        size="sm"
                        variant={r.changes_enabled ? "ghost" : "secondary"}
                        onClick={() => toggle.mutate(!r.changes_enabled)}
                        loading={toggle.isPending}
                        disabled={!r.changes_enabled && !r.automation_enabled}
                      >
                        {r.changes_enabled ? "Turn off" : "Turn on"}
                      </Button>
                    </span>
                  )}
                </span>
              </Requirement>
              {r.connection && <ConnectionRequirement c={r.connection} name={s.name} browser={r.confirm_in_browser} />}
              {r.protected_interfaces.length > 0 && (
                <li className="pl-6 text-xs text-muted">
                  Protected, never changed by Lanvex: <span className="addr">{r.protected_interfaces.join(", ")}</span>
                </li>
              )}
            </ul>
          </div>

          <div className="grid content-start gap-3">
            {r.open_change && (
              <Notice
                tone="info"
                title={`Change #${r.open_change.number} is open`}
                action={
                  <Button asChild size="sm" variant="secondary">
                    <Link to={`/changes/${r.open_change.id}`}>Open</Link>
                  </Button>
                }
              >
                {r.open_change.summary}
              </Notice>
            )}
            {r.links.length > 0 && (
              <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
                <p className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Bridges and bonds by Lanvex</p>
                <ul>
                  {r.links.map((l) => (
                    <li key={l.name} className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5 last:border-0">
                      <span className="grid">
                        <span className="addr text-[13px] font-medium">{l.name}</span>
                        <span className="text-xs text-muted">
                          {l.type === "bond" ? `Bond (${l.mode})` : "Bridge"} of {l.members.join(", ")}
                          {l.method === "static" ? ` · ${l.addresses.join(", ")}` : l.method === "dhcp" ? " · DHCP" : ""}
                        </span>
                      </span>
                      {isAdmin && r.ready && !r.open_change && (
                        <Button size="sm" variant="ghost" onClick={() => setRemoving(l)}>
                          <Trash2 /> Remove…
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="rounded-[var(--radius-panel)] border border-line bg-surface">
              <p className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Recent changes</p>
              {recent.data && recent.data.length > 0 ? (
                <ul>
                  {recent.data.map((c) => (
                    <li key={c.id} className="border-b border-line last:border-0">
                      <Link to={`/changes/${c.id}`} className="grid gap-1 px-4 py-2.5 hover:bg-surface-2/60">
                        <span className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-[13px] font-medium">#{c.number} · {c.interface}</span>
                          <ChangeStatusBadge status={c.status} />
                        </span>
                        <span className="text-xs text-muted">
                          {c.summary.replace(`${c.interface}: `, "")} · {relativeTime(c.created_at)}
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-4 py-6 text-center text-[13px] text-muted">No changes yet.</p>
              )}
            </div>
          </div>
        </div>
      )}
      {r && <InstallHelperDialog server={s} readiness={r} open={dialog === "install"} onClose={() => setDialog(null)} onChecked={refresh} />}
      {r && facts && <LinkDialog server={s} facts={facts} readiness={r} open={dialog === "link"} onClose={() => setDialog(null)} />}
      <RemoveLinkDialog server={s} link={removing} onClose={() => setRemoving(null)} />
      {r && facts && (
        <ChangeDialog
          server={s}
          facts={facts}
          readiness={r}
          seed={seed}
          open={dialog === "change"}
          onClose={() => {
            setDialog(null);
            onSeedUsed?.();
          }}
        />
      )}
    </Section>
  );
}

/** Interfaces that can become members: Ethernet, in no bridge or bond, no addresses, no VLANs on them. */
function freeInterfaces(facts: HostFacts, protectedPatterns: string[]): string[] {
  const parents = new Set(facts.interfaces.filter((i) => i.kind === "vlan").map((i) => i.parent));
  return facts.interfaces
    .filter(
      (i) =>
        (i.kind === "ethernet" || i.kind === "veth") &&
        !i.master &&
        !parents.has(i.name) &&
        !i.addresses.some((a) => a.scope !== "link") &&
        !isProtected(i.name, protectedPatterns),
    )
    .map((i) => i.name);
}

const BOND_MODES: [string, string][] = [
  ["active-backup", "Active-backup (any switch)"],
  ["802.3ad", "802.3ad / LACP (switch must be set up for it)"],
  ["balance-alb", "Adaptive load balancing"],
  ["balance-tlb", "Transmit load balancing"],
  ["balance-xor", "XOR"],
  ["balance-rr", "Round-robin"],
  ["broadcast", "Broadcast"],
];

function LinkDialog({ server: s, facts, readiness: r, open, onClose }: { server: Server; facts: HostFacts; readiness: ChangeReadiness; open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const free = freeInterfaces(facts, r.protected_interfaces);
  const [d, setD] = React.useState({ type: "bridge" as "bridge" | "bond", name: "br1", members: [] as string[], mode: "active-backup", stp: false, method: "none" as "none" | "static" | "dhcp", addresses: "", gateway: "" });
  const prepare = useMutation({
    mutationFn: (body: LinkRequestBody) => api<ChangeDetail>(`/servers/${s.id}/changes`, { method: "POST", body }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["changes"] });
      void qc.invalidateQueries({ queryKey: ["servers", s.id] });
      onClose();
      navigate(`/changes/${c.id}`);
    },
  });
  React.useEffect(() => {
    if (!open) return;
    const taken = new Set(facts.interfaces.map((i) => i.name));
    const next = (prefix: string) => `${prefix}${[...Array(20).keys()].find((n) => !taken.has(`${prefix}${n}`)) ?? 0}`;
    setD({ type: "bridge", name: next("br"), members: [], mode: "active-backup", stp: false, method: "none", addresses: "", gateway: "" });
    prepare.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const set = (patch: Partial<typeof d>) => setD((prev) => ({ ...prev, ...patch }));
  const err = (...names: string[]) => fieldError(prepare.error, ...names);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`Add a bridge or bond on ${s.name}`} width="max-w-xl">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            prepare.mutate({
              kind: "link",
              action: "create",
              name: d.name.trim(),
              type: d.type,
              members: d.members,
              mode: d.type === "bond" ? d.mode : null,
              stp: d.type === "bridge" && d.stp,
              method: d.method,
              addresses: d.method === "static" ? split(d.addresses) : [],
              gateway: d.method === "static" && d.gateway.trim() ? d.gateway.trim() : null,
            });
          }}
        >
          <DialogBody>
            {prepare.error && !err("name", "members", "addresses", "gateway") && <Notice tone="danger">{errorMessage(prepare.error)}</Notice>}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Type" htmlFor="lk-type">
                <Select id="lk-type" value={d.type} onChange={(e) => set({ type: e.target.value as "bridge" | "bond", name: d.name.replace(/^(br|bond)(\d+)$/, (_, __, n) => `${e.target.value === "bond" ? "bond" : "br"}${n}`) })}>
                  <option value="bridge">Bridge</option>
                  <option value="bond">Bond</option>
                </Select>
              </Field>
              <Field label="Name" htmlFor="lk-name" error={err("name")}>
                <Input id="lk-name" className="addr" value={d.name} maxLength={15} onChange={(e) => set({ name: e.target.value })} spellCheck={false} />
              </Field>
            </div>
            <fieldset className="grid gap-1.5">
              <legend className="mb-1.5 text-[13px] font-medium">Members</legend>
              {free.length === 0 ? (
                <p className="text-[13px] text-muted">No free interfaces: members need to be Ethernet interfaces without addresses.</p>
              ) : (
                <div className="flex flex-wrap gap-x-5 gap-y-1.5">
                  {free.map((m) => (
                    <Checkbox
                      key={m}
                      id={`lk-m-${m}`}
                      label={m}
                      checked={d.members.includes(m)}
                      onChange={(e) => set({ members: e.target.checked ? [...d.members, m] : d.members.filter((x) => x !== m) })}
                    />
                  ))}
                </div>
              )}
              {err("members") && (
                <p role="alert" className="text-xs text-danger">
                  {err("members")}
                </p>
              )}
            </fieldset>
            {d.type === "bond" ? (
              <Field label="Mode" htmlFor="lk-mode">
                <Select id="lk-mode" value={d.mode} onChange={(e) => set({ mode: e.target.value })}>
                  {BOND_MODES.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : (
              <Checkbox id="lk-stp" label="Spanning tree (STP)" checked={d.stp} onChange={(e) => set({ stp: e.target.checked })} />
            )}
            <Field label="IPv4" htmlFor="lk-method">
              <Select id="lk-method" value={d.method} onChange={(e) => set({ method: e.target.value as "none" | "static" | "dhcp" })}>
                <option value="none">No address</option>
                <option value="static">Static</option>
                <option value="dhcp">From DHCP</option>
              </Select>
            </Field>
            {d.method === "static" && (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Addresses" htmlFor="lk-addresses" error={err("addresses")} hint="Comma-separated">
                  <Input id="lk-addresses" className="addr" value={d.addresses} placeholder="192.0.2.10/24" onChange={(e) => set({ addresses: e.target.value })} />
                </Field>
                <Field label="Default gateway" htmlFor="lk-gw" optional error={err("gateway")}>
                  <Input id="lk-gw" className="addr" value={d.gateway} placeholder="192.0.2.1" onChange={(e) => set({ gateway: e.target.value })} />
                </Field>
              </div>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={prepare.isPending} disabled={!d.name.trim() || d.members.length === 0}>
              Prepare change
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RemoveLinkDialog({ server: s, link, onClose }: { server: Server; link: LanvexLink | null; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const prepare = useMutation({
    mutationFn: (name: string) => api<ChangeDetail>(`/servers/${s.id}/changes`, { method: "POST", body: { kind: "link", action: "remove", name } }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["changes"] });
      void qc.invalidateQueries({ queryKey: ["servers", s.id] });
      onClose();
      navigate(`/changes/${c.id}`);
    },
  });
  React.useEffect(() => {
    if (link) prepare.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link]);
  return (
    <Dialog open={link !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={link ? `Remove ${link.name}?` : "Remove"} description={link ? `${link.members.join(", ")} become free again. Prepared as a change, applied after approval.` : undefined}>
        <DialogBody>{prepare.error && <Notice tone="danger">{errorMessage(prepare.error)}</Notice>}</DialogBody>
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="danger" loading={prepare.isPending} onClick={() => link && prepare.mutate(link.name)}>
            Prepare removal
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Requirement({ ok, label, children }: { ok: boolean | null; label: string; children: React.ReactNode }) {
  const Icon = ok === true ? CheckCircle2 : ok === false ? XCircle : CircleDashed;
  return (
    <li className="flex gap-2.5 text-[13px]">
      <Icon className={cn("mt-0.5 size-4 shrink-0", ok === true ? "text-ok" : ok === false ? "text-danger" : "text-faint")} aria-hidden />
      <span className="grid gap-0.5">
        <span className="font-medium">
          {label}
          <span className="sr-only">{ok === true ? " (done)" : ok === false ? " (missing)" : " (unknown)"}</span>
        </span>
        <span className="text-xs text-muted">{children}</span>
      </span>
    </li>
  );
}

function InstallHelperDialog({
  server: s,
  readiness: r,
  open,
  onClose,
  onChecked,
}: {
  server: Server;
  readiness: ChangeReadiness;
  open: boolean;
  onClose: () => void;
  onChecked: () => void;
}) {
  const upload = useMutation({
    mutationFn: () => api<HelperInstall>(`/servers/${s.id}/changes/helper/upload`, { method: "POST" }),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const check = useMutation({
    mutationFn: () => api<ChangeReadiness>(`/servers/${s.id}/changes/helper/check`, { method: "POST" }),
    onSuccess: (res) => {
      onChecked();
      if (res.helper.state === "ok") {
        toast.success("The helper is installed and allowed.");
        onClose();
      }
    },
  });
  React.useEffect(() => {
    if (open) {
      upload.reset();
      check.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const path = upload.data?.path ?? "./lanvex-netapply";
  const commands = upload.data?.commands ?? [
    `echo '${r.helper_sha256}  ${path}' | sha256sum -c -`,
    `sudo install -D -m 0755 -o root -g root ${path} ${r.helper_path}`,
    `echo '${s.username} ALL=(root) NOPASSWD: ${r.helper_path}' | sudo tee /etc/sudoers.d/lanvex >/dev/null`,
    "sudo chmod 0440 /etc/sudoers.d/lanvex && sudo visudo -cf /etc/sudoers.d/lanvex",
  ];
  const result = check.data?.helper;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title="Install the change helper"
        width="max-w-2xl"
      >
        <DialogBody>
          <ol className="grid gap-4 text-[13px]">
            <li className="grid gap-2">
              <p>
                <span className="font-medium">1. Copy the helper to the server</span>
              </p>
              <span className="flex flex-wrap gap-2">
                <Button size="sm" variant="secondary" onClick={() => upload.mutate()} loading={upload.isPending}>
                  <Upload /> Copy to the server
                </Button>
                <Button asChild size="sm" variant="ghost">
                  <a href="/api/v1/changes/helper" download="lanvex-netapply">
                    <Download /> Download
                  </a>
                </Button>
              </span>
              {upload.data && <Notice title={`Copied to ${upload.data.path}`}>The commands below now use that path.</Notice>}
            </li>
            <li className="grid gap-2">
              <p>
                <span className="font-medium">2. Install it as root</span>{" "}
                <span className="text-muted">Stop if the checksum line does not print OK.</span>
              </p>
              <CodeBlock text={commands.join("\n")} label="Copy commands" />
              <p className="text-xs text-muted">Requires Python 3.8+ and systemd.</p>
            </li>
            <li className="grid gap-2">
              <p>
                <span className="font-medium">3. Check it</span>
              </p>
              <span>
                <Button size="sm" variant="secondary" onClick={() => check.mutate()} loading={check.isPending}>
                  Check now
                </Button>
              </span>
              {result && result.state !== "ok" && (
                <Notice tone="danger" title={HELPER_LABEL[result.state]}>
                  {result.message}
                </Notice>
              )}
              {check.error && <Notice tone="danger">{errorMessage(check.error)}</Notice>}
            </li>
          </ol>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface Draft {
  iface: string;
  method: "static" | "dhcp";
  addresses: string[];
  gateway: string;
  changeDns: boolean;
  dns: string;
  search: string;
  verifyName: string;
  changeV6: boolean;
  v6method: "auto" | "static" | "link-local";
  v6addresses: string;
  v6gateway: string;
  changeRoutes: boolean;
  routes: { to: string; via: string; metric: string }[];
}

const isDefault = (dst: string) => dst === "0.0.0.0/0" || dst === "::/0" || dst === "default";

/** What the interface has now, as last read: the starting point of the form. */
function draftFor(facts: HostFacts, iface: string): Draft {
  const i = facts.interfaces.find((x) => x.name === iface);
  const v4 = (i?.addresses ?? []).filter((a) => a.family === 4);
  const statics = v4.filter((a) => !a.dynamic).map((a) => a.address);
  const gateway = facts.routes.find((rt) => rt.family === 4 && rt.interface === iface && (rt.destination === "0.0.0.0/0" || rt.destination === "default"))?.gateway ?? "";
  const scope = facts.dns.resolved?.find((x) => x.scope === iface);
  const v6 = (i?.addresses ?? []).filter((a) => a.family === 6 && a.scope !== "link");
  const v6static = v6.filter((a) => !a.dynamic).map((a) => a.address);
  const gw6 = facts.routes.find((rt) => rt.family === 6 && rt.interface === iface && isDefault(rt.destination))?.gateway ?? "";
  const routes = facts.routes
    .filter((rt) => rt.interface === iface && rt.gateway && !isDefault(rt.destination) && (rt.protocol === "static" || rt.protocol === "boot"))
    .map((rt) => ({ to: rt.destination, via: rt.gateway ?? "", metric: rt.metric == null ? "" : String(rt.metric) }));
  return {
    iface,
    method: statics.length === 0 && v4.some((a) => a.dynamic) ? "dhcp" : "static",
    addresses: statics.length ? statics : [""],
    gateway,
    changeDns: false,
    dns: (scope?.servers ?? []).filter((x) => !x.includes(":")).join(", "),
    search: (scope?.domains ?? []).filter((d) => d !== "~.").join(", "),
    verifyName: "",
    changeV6: false,
    v6method: v6static.length ? "static" : v6.some((a) => a.dynamic) ? "auto" : "link-local",
    v6addresses: v6static.join(", "),
    v6gateway: gw6,
    changeRoutes: false,
    routes,
  };
}

const split = (v: string) =>
  v
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean);

function ChangeDialog({
  server: s,
  facts,
  readiness: r,
  seed,
  open,
  onClose,
}: {
  server: Server;
  facts: HostFacts;
  readiness: ChangeReadiness;
  seed: ChangeSeed | null;
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const candidates = facts.interfaces.filter((i) => i.kind !== "loopback" && !i.master && !isProtected(i.name, r.protected_interfaces));
  const [d, setD] = React.useState<Draft>(() => draftFor(facts, candidates[0]?.name ?? ""));
  const dnsKnown = !!facts.dns.resolved?.some((x) => x.scope === d.iface);
  const prepare = useMutation({
    mutationFn: (body: ChangeRequestBody) => api<ChangeDetail>(`/servers/${s.id}/changes`, { method: "POST", body }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["changes"] });
      void qc.invalidateQueries({ queryKey: ["servers", s.id] });
      onClose();
      navigate(`/changes/${c.id}`);
    },
  });
  React.useEffect(() => {
    if (open) {
      setD(seed ? { ...draftFor(facts, seed.iface), method: "static", addresses: seed.addresses } : draftFor(facts, candidates[0]?.name ?? ""));
      prepare.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const set = (patch: Partial<Draft>) => setD((prev) => ({ ...prev, ...patch }));
  const err = (...names: string[]) => fieldError(prepare.error, ...names);
  const generalError = prepare.error && !err("addresses", "gateway", "interface", "dns_servers", "dns_search", "verify_dns_name", "ipv6", "routes") ? errorMessage(prepare.error) : null;

  const submit = () =>
    prepare.mutate({
      interface: d.iface,
      method: d.method,
      addresses: d.method === "static" ? d.addresses.map((a) => a.trim()).filter(Boolean) : [],
      gateway: d.method === "static" && d.gateway.trim() ? d.gateway.trim() : null,
      dns_servers: d.changeDns ? split(d.dns) : null,
      dns_search: d.changeDns ? split(d.search) : null,
      verify_dns_name: d.verifyName.trim() || null,
      ...(d.changeV6
        ? {
            ipv6: {
              method: d.v6method,
              addresses: d.v6method === "static" ? split(d.v6addresses) : [],
              gateway: d.v6method === "static" && d.v6gateway.trim() ? d.v6gateway.trim() : null,
            },
          }
        : {}),
      ...(d.changeRoutes
        ? {
            routes: d.routes
              .filter((x) => x.to.trim() || x.via.trim())
              .map((x) => ({ to: x.to.trim(), via: x.via.trim(), metric: x.metric.trim() ? Number(x.metric) : null })),
          }
        : {}),
    });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`Change IP settings on ${s.name}`}
        width="max-w-xl"
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <DialogBody>
            {generalError && <Notice tone="danger">{generalError}</Notice>}
            {seed && seed.iface === d.iface && (
              <Notice tone="info" title="Prepared from the comparison">
                {seed.note}
              </Notice>
            )}
            <Field label="Interface" htmlFor="chg-iface" error={err("interface")}>
              <Select id="chg-iface" value={d.iface} onChange={(e) => setD(draftFor(facts, e.target.value))}>
                {candidates.map((i) => (
                  <option key={i.name} value={i.name}>
                    {i.name}
                    {i.addresses.some((a) => a.family === 4) ? ` (${i.addresses.filter((a) => a.family === 4).map((a) => a.address).join(", ")})` : ""}
                  </option>
                ))}
              </Select>
            </Field>

            <fieldset className="grid gap-1.5">
              <legend className="mb-1.5 text-[13px] font-medium">IPv4 addresses</legend>
              <div className="flex gap-4 text-[13px]">
                {(["static", "dhcp"] as const).map((m) => (
                  <label key={m} className="flex items-center gap-2">
                    <input type="radio" name="chg-method" value={m} checked={d.method === m} onChange={() => set({ method: m })} className="accent-[var(--color-accent)]" />
                    {m === "static" ? "Static" : "From DHCP"}
                  </label>
                ))}
              </div>
            </fieldset>

            {d.method === "static" && (
              <>
                <div className="grid gap-1.5">
                  {d.addresses.map((a, idx) => (
                    <div key={idx} className="flex gap-2">
                      <Input
                        aria-label={`Address ${idx + 1}`}
                        className="addr"
                        value={a}
                        placeholder="192.0.2.10/24"
                        onChange={(e) => set({ addresses: d.addresses.map((x, j) => (j === idx ? e.target.value : x)) })}
                        aria-invalid={!!err("addresses")}
                      />
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        aria-label={`Remove address ${idx + 1}`}
                        disabled={d.addresses.length === 1}
                        onClick={() => set({ addresses: d.addresses.filter((_, j) => j !== idx) })}
                      >
                        <X />
                      </Button>
                    </div>
                  ))}
                  {err("addresses") && (
                    <p role="alert" className="text-xs text-danger">
                      {err("addresses")}
                    </p>
                  )}
                  <span>
                    <Button type="button" size="sm" variant="ghost" onClick={() => set({ addresses: [...d.addresses, ""] })}>
                      <Plus /> Add address
                    </Button>
                  </span>
                </div>
                <Field label="Default gateway" htmlFor="chg-gw" optional error={err("gateway")} >
                  <Input id="chg-gw" className="addr" value={d.gateway} placeholder="192.0.2.1" onChange={(e) => set({ gateway: e.target.value })} />
                </Field>
              </>
            )}

            <div className="grid gap-2 rounded-[var(--radius-control)] border border-line p-3">
              <Checkbox id="chg-dns" label="Also change DNS for this interface" checked={d.changeDns} onChange={(e) => set({ changeDns: e.target.checked })} />
              {!d.changeDns ? (
                <p className="text-xs text-muted">Unchanged</p>
              ) : (
                <>
                  {!dnsKnown && <Notice tone="warn">Current DNS settings unknown; these replace them.</Notice>}
                  <Field label="DNS servers" htmlFor="chg-dns-servers" error={err("dns_servers")} hint="Comma-separated">
                    <Input id="chg-dns-servers" className="addr" value={d.dns} placeholder="192.0.2.53, 192.0.2.54" onChange={(e) => set({ dns: e.target.value })} />
                  </Field>
                  <Field label="Search domains" htmlFor="chg-dns-search" optional error={err("dns_search")}>
                    <Input id="chg-dns-search" value={d.search} placeholder="example.net" onChange={(e) => set({ search: e.target.value })} />
                  </Field>
                </>
              )}
            </div>

            <div className="grid gap-2 rounded-[var(--radius-control)] border border-line p-3">
              <Checkbox id="chg-v6" label="Also change IPv6" checked={d.changeV6} onChange={(e) => set({ changeV6: e.target.checked })} />
              {!d.changeV6 ? (
                <p className="text-xs text-muted">Unchanged</p>
              ) : (
                <>
                  <div className="flex flex-wrap gap-4 text-[13px]" role="radiogroup" aria-label="IPv6 method">
                    {(
                      [
                        ["auto", "Automatic (router advertisements, DHCPv6)"],
                        ["static", "Static"],
                        ["link-local", "Link-local only"],
                      ] as const
                    ).map(([m, label]) => (
                      <label key={m} className="flex items-center gap-2">
                        <input type="radio" name="chg-v6-method" value={m} checked={d.v6method === m} onChange={() => set({ v6method: m })} className="accent-[var(--color-accent)]" />
                        {label}
                      </label>
                    ))}
                  </div>
                  {d.v6method === "static" && (
                    <>
                      <Field label="IPv6 addresses" htmlFor="chg-v6-addresses" hint="Comma-separated" error={err("ipv6")}>
                        <Input id="chg-v6-addresses" className="addr" value={d.v6addresses} placeholder="2001:db8::10/64" onChange={(e) => set({ v6addresses: e.target.value })} />
                      </Field>
                      <Field label="IPv6 gateway" htmlFor="chg-v6-gw" optional>
                        <Input id="chg-v6-gw" className="addr" value={d.v6gateway} placeholder="fe80::1" onChange={(e) => set({ v6gateway: e.target.value })} />
                      </Field>
                    </>
                  )}
                  {d.v6method !== "static" && err("ipv6") && (
                    <p role="alert" className="text-xs text-danger">
                      {err("ipv6")}
                    </p>
                  )}
                </>
              )}
            </div>

            <div className="grid gap-2 rounded-[var(--radius-control)] border border-line p-3">
              <Checkbox id="chg-routes" label="Also change static routes" checked={d.changeRoutes} onChange={(e) => set({ changeRoutes: e.target.checked })} />
              {!d.changeRoutes ? (
                <p className="text-xs text-muted">Unchanged</p>
              ) : (
                <div className="grid gap-1.5">
                  {d.routes.map((rt, idx) => (
                    <div key={idx} className="grid grid-cols-[1fr_1fr_5rem_auto] gap-2">
                      <Input aria-label={`Route ${idx + 1} network`} className="addr" value={rt.to} placeholder="10.20.0.0/16" onChange={(e) => set({ routes: d.routes.map((x, j) => (j === idx ? { ...x, to: e.target.value } : x)) })} />
                      <Input aria-label={`Route ${idx + 1} next hop`} className="addr" value={rt.via} placeholder="via 192.0.2.254" onChange={(e) => set({ routes: d.routes.map((x, j) => (j === idx ? { ...x, via: e.target.value } : x)) })} />
                      <Input aria-label={`Route ${idx + 1} metric`} className="addr" inputMode="numeric" value={rt.metric} placeholder="metric" onChange={(e) => set({ routes: d.routes.map((x, j) => (j === idx ? { ...x, metric: e.target.value.replace(/\D/g, "") } : x)) })} />
                      <Button type="button" size="icon" variant="ghost" aria-label={`Remove route ${idx + 1}`} onClick={() => set({ routes: d.routes.filter((_, j) => j !== idx) })}>
                        <X />
                      </Button>
                    </div>
                  ))}
                  {d.routes.length === 0 && <p className="text-xs text-muted">No static routes: those there now are removed.</p>}
                  {err("routes") && (
                    <p role="alert" className="text-xs text-danger">
                      {err("routes")}
                    </p>
                  )}
                  <span>
                    <Button type="button" size="sm" variant="ghost" onClick={() => set({ routes: [...d.routes, { to: "", via: "", metric: "" }] })}>
                      <Plus /> Add route
                    </Button>
                  </span>
                </div>
              )}
            </div>

            <Field
              label="Check that this name resolves afterwards"
              htmlFor="chg-verify"
              optional
              error={err("verify_dns_name")}
            >
              <Input id="chg-verify" value={d.verifyName} placeholder="netbox.example.net" onChange={(e) => set({ verifyName: e.target.value })} />
            </Field>
            <p className="text-xs text-muted">
              Other settings stay unchanged. Rolled back automatically after {r.rollback_seconds} s if it fails.
            </p>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={prepare.isPending} disabled={!d.iface}>
              Prepare change
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Shell-style pattern match (* ? [...]), as the server applies it to protected interfaces. */
export function isProtected(name: string, patterns: string[]): boolean {
  return patterns.some((p) => {
    const re = p.replace(/[.+^${}()|\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
    try {
      return new RegExp(`^${re}$`).test(name);
    } catch {
      return p === name;
    }
  });
}

function ConnectionRequirement({ c, name, browser }: { c: NonNullable<ChangeReadiness["connection"]>; name: string; browser: boolean }) {
  if (browser) {
    return (
      <Requirement ok label="Your browser confirms changes">
        {c.interfaces.length > 0 ? (
          <>
            Lanvex reaches {name} at <span className="addr">{c.address}</span> on {c.interfaces.join(", ")}.
          </>
        ) : (
          <>
            Lanvex reaches {name} through <span className="addr">{c.address}</span>, which is not one of its addresses, so it cannot check a change from outside.
          </>
        )}{" "}
        After every change you choose Keep in your browser. Use Lanvex through one of {name}'s own addresses, so that reaching Lanvex shows its network still works; without a decision in
        time, the server restores its previous configuration.
      </Requirement>
    );
  }
  if (c.interfaces.length > 0) {
    return (
      <Requirement ok label="Lanvex knows which interface it relies on">
        Lanvex reaches {name} at <span className="addr">{c.address}</span> on {c.interfaces.join(", ")}. A change must keep that address, and Lanvex signs in again before confirming.
      </Requirement>
    );
  }
  const verified = c.verify_address && c.verify_interfaces.length > 0;
  return (
    <Requirement ok={verified ? true : false} label="Lanvex can check the server on its own address">
      {verified ? (
        <>
          Lanvex connects through <span className="addr">{c.address}</span>, which is not one of the server's addresses, so it also checks <span className="addr">{c.verify_address}</span> on {c.verify_interfaces.join(", ")} after every change.
        </>
      ) : (
        <>
          Lanvex connects through <span className="addr">{c.address}</span>, which is not one of the server's addresses (port forwarding, NAT, or Lanvex running on this server). In Edit server, set a
          verification address, or, if Lanvex runs on this server, let your browser confirm changes.
        </>
      )}
    </Requirement>
  );
}
