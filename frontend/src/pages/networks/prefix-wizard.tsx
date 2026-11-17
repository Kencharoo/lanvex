import { useQuery } from "@tanstack/react-query";
import { Check, CircleAlert, Info, TriangleAlert } from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Notice } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatBig, parseCidr, suggestGateway, usableHosts } from "@/lib/cidr";
import { fieldError, useLookup, useNetBoxWrite } from "@/lib/hooks";
import type { Prefix, PrefixStatus, PrefixValidation, WriteResult } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNetBoxLabel } from "@/lib/system";

const STEPS = ["Network", "Placement", "Gateway", "Review"] as const;

interface Draft {
  name: string;
  prefix: string;
  vrf_id: string;
  vlan_id: string;
  status: PrefixStatus;
  role_id: string;
  site_id: string;
  is_pool: boolean;
  gateway: string;
  gatewayEnabled: boolean;
}

const EMPTY: Draft = {
  name: "",
  prefix: "",
  vrf_id: "",
  vlan_id: "",
  status: "active",
  role_id: "",
  site_id: "",
  is_pool: false,
  gateway: "",
  gatewayEnabled: true,
};

const num = (v: string) => (v ? Number(v) : null);

function useServerValidation(draft: Draft, enabled: boolean) {
  const [debounced, setDebounced] = React.useState({ prefix: draft.prefix, vrf: draft.vrf_id, gw: draft.gateway, gwOn: draft.gatewayEnabled });
  React.useEffect(() => {
    const t = setTimeout(() => setDebounced({ prefix: draft.prefix, vrf: draft.vrf_id, gw: draft.gateway, gwOn: draft.gatewayEnabled }), 300);
    return () => clearTimeout(t);
  }, [draft.prefix, draft.vrf_id, draft.gateway, draft.gatewayEnabled]);
  const local = parseCidr(debounced.prefix);
  return useQuery({
    queryKey: ["prefix-validate", debounced],
    queryFn: () =>
      api<PrefixValidation>("/prefixes/validate", {
        method: "POST",
        body: { prefix: debounced.prefix.trim(), vrf_id: num(debounced.vrf), gateway: debounced.gwOn && debounced.gw.trim() ? debounced.gw.trim() : null },
      }),
    enabled: enabled && local.ok,
    staleTime: 10_000,
  });
}

export function PrefixWizard({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (p: Prefix) => void }) {
  const nbName = useNetBoxLabel();
  const [step, setStep] = React.useState(0);
  const [draft, setDraft] = React.useState<Draft>(EMPTY);
  const [touched, setTouched] = React.useState(false);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  React.useEffect(() => {
    if (open) {
      setStep(0);
      setDraft(EMPTY);
      setTouched(false);
    }
  }, [open]);

  const vrfs = useLookup("vrfs", undefined, open);
  const vlans = useLookup("vlans", undefined, open);
  const roles = useLookup("prefix-roles", undefined, open);
  const sites = useLookup("sites", undefined, open);
  const local = parseCidr(draft.prefix);
  const validation = useServerValidation(draft, open);
  const v = validation.data;
  const serverErrors = v?.findings.filter((f) => f.level === "error") ?? [];
  const prefixError = !local.ok ? (touched || draft.prefix ? local.error : undefined) : serverErrors.find((f) => f.field === "prefix")?.message;
  const gatewayError = serverErrors.find((f) => f.field === "gateway")?.message;

  // Pre-fill the conventional gateway once a valid prefix is entered.
  const suggested = local.ok ? suggestGateway(local.value) : null;
  const lastSuggested = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (suggested && (draft.gateway === "" || draft.gateway === lastSuggested.current)) set("gateway", suggested);
    lastSuggested.current = suggested;
  }, [suggested]); // eslint-disable-line react-hooks/exhaustive-deps

  const create = useNetBoxWrite({
    fn: () =>
      api<WriteResult & { prefix: Prefix }>("/prefixes", {
        method: "POST",
        body: {
          prefix: local.ok ? local.value.canonical : draft.prefix.trim(),
          name: draft.name.trim() || null,
          status: draft.status,
          vrf_id: num(draft.vrf_id),
          vlan_id: num(draft.vlan_id),
          role_id: num(draft.role_id),
          site_id: num(draft.site_id),
          is_pool: draft.is_pool,
          gateway: draft.gatewayEnabled && draft.status !== "container" && draft.gateway.trim() ? draft.gateway.trim() : null,
        },
      }),
    success: (r) => `Created ${r.prefix.prefix}`,
    invalidate: [["prefixes"]],
    onSuccess: (r) => {
      onOpenChange(false);
      onCreated(r.prefix);
    },
  });

  const canAdvance = [
    local.ok && !validation.isFetching && serverErrors.filter((f) => f.field === "prefix").length === 0,
    true,
    !draft.gatewayEnabled || draft.status === "container" || !draft.gateway.trim() || !gatewayError,
    v?.valid === true && !validation.isFetching,
  ];

  const next = () => {
    setTouched(true);
    if (canAdvance[step]) setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };

  const selectedVrf = vrfs.data?.find((x) => String(x.id) === draft.vrf_id);
  const selectedVlan = vlans.data?.find((x) => String(x.id) === draft.vlan_id);
  const selectedRole = roles.data?.find((x) => String(x.id) === draft.role_id);
  const selectedSite = sites.data?.find((x) => String(x.id) === draft.site_id);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="New network" width="max-w-[720px]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (step < STEPS.length - 1) next();
            else if (canAdvance[3]) create.mutate(undefined);
          }}
        >
          <div className="grid md:grid-cols-[168px_1fr]">
            <ol className="hidden border-r border-line px-3 py-4 md:block" aria-label="Steps">
              {STEPS.map((label, i) => (
                <li key={label}>
                  <button
                    type="button"
                    onClick={() => i < step && setStep(i)}
                    disabled={i > step}
                    aria-current={i === step ? "step" : undefined}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2 py-1.5 text-left text-[13px]",
                      i === step ? "bg-accent-soft font-medium text-accent" : i < step ? "text-ink hover:bg-surface-2" : "text-faint",
                    )}
                  >
                    <span
                      className={cn(
                        "grid size-5 shrink-0 place-items-center rounded-full border text-2xs tabular-nums",
                        i < step ? "border-accent bg-accent text-accent-ink" : i === step ? "border-accent" : "border-line",
                      )}
                    >
                      {i < step ? <Check className="size-3" /> : i + 1}
                    </span>
                    {label}
                  </button>
                </li>
              ))}
            </ol>

            <DialogBody className="min-h-[340px] content-start">
              <p className="text-xs text-muted md:hidden">
                Step {step + 1} of {STEPS.length}: {STEPS[step]}
              </p>

              {step === 0 && (
                <>
                  <Field label="Name" htmlFor="pf-name" optional >
                    <Input id="pf-name" value={draft.name} maxLength={200} placeholder="e.g. Office LAN, 3rd floor" onChange={(e) => set("name", e.target.value)} autoFocus />
                  </Field>
                  <Field
                    label="Prefix"
                    htmlFor="pf-prefix"
                    error={prefixError}
                    hint={local.ok ? `${local.value.canonical}: ${formatBig(local.value.size)} addresses, ${formatBig(usableHosts(local.value, draft.is_pool))} usable` : "CIDR notation, IPv4 or IPv6. For example 10.20.0.0/24 or 2001:db8:20::/48."}
                  >
                    <Input
                      id="pf-prefix"
                      className="addr"
                      value={draft.prefix}
                      onChange={(e) => set("prefix", e.target.value)}
                      onBlur={() => setTouched(true)}
                      placeholder="10.20.0.0/24"
                      spellCheck={false}
                      autoComplete="off"
                      aria-invalid={!!prefixError}
                      aria-describedby={prefixError ? "pf-prefix-error" : undefined}
                    />
                  </Field>
                  {!local.ok && local.suggestion && (
                    <Button type="button" size="sm" variant="secondary" className="justify-self-start" onClick={() => set("prefix", local.suggestion!)}>
                      Use {local.suggestion}
                    </Button>
                  )}
                  <ValidationFindings v={v} loading={validation.isFetching} error={validation.error} onlyLevels={["error", "warning", "info"]} />
                </>
              )}

              {step === 1 && (
                <>
                  <Field label="VRF" htmlFor="pf-vrf" >
                    <Select id="pf-vrf" value={draft.vrf_id} onChange={(e) => set("vrf_id", e.target.value)}>
                      <option value="">Global table</option>
                      {vrfs.data?.map((o) => (
                        <option key={o.id} value={o.id}>
                          {o.label}
                          {o.hint ? ` (${o.hint})` : ""}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="Status" htmlFor="pf-status" hint={draft.status === "container" ? "Containers organise child prefixes." : undefined}>
                      <Select id="pf-status" value={draft.status} onChange={(e) => set("status", e.target.value as PrefixStatus)}>
                        <option value="active">Active</option>
                        <option value="reserved">Reserved</option>
                        <option value="container">Container</option>
                        <option value="deprecated">Deprecated</option>
                      </Select>
                    </Field>
                    <Field label="Role" htmlFor="pf-role" optional>
                      <Select id="pf-role" value={draft.role_id} onChange={(e) => set("role_id", e.target.value)}>
                        <option value="">None</option>
                        {roles.data?.map((o) => (
                          <option key={o.id} value={o.id}>
                            {o.label}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  </div>
                  <Field label="Site" htmlFor="pf-site" optional hint="Where this network is used.">
                    <Select id="pf-site" value={draft.site_id} onChange={(e) => set("site_id", e.target.value)} disabled={sites.isPending}>
                      <option value="">No site</option>
                      {sites.data?.map((o) => (
                        <option key={o.id} value={o.id}>
                          {o.label}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="VLAN" htmlFor="pf-vlan" optional>
                    <Select id="pf-vlan" value={draft.vlan_id} onChange={(e) => set("vlan_id", e.target.value)} disabled={vlans.isPending}>
                      <option value="">No VLAN</option>
                      {vlans.data?.map((o) => (
                        <option key={o.id} value={o.id}>
                          {o.label}
                          {o.hint ? `, ${o.hint}` : ""}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Checkbox id="pf-pool" label="Treat as a pool (every address is usable, including network and broadcast)" checked={draft.is_pool} onChange={(e) => set("is_pool", e.target.checked)} />
                </>
              )}

              {step === 2 &&
                (draft.status === "container" ? (
                  <Notice title="No gateway for containers">Container prefixes organise child networks and are not assigned a gateway.</Notice>
                ) : (
                  <>
                    <Checkbox id="pf-gw-on" label="Record a gateway address" checked={draft.gatewayEnabled} onChange={(e) => set("gatewayEnabled", e.target.checked)} />
                    {draft.gatewayEnabled && (
                      <Field
                        label="Gateway"
                        htmlFor="pf-gateway"
                        error={gatewayError}
                        hint="With prefix length"
                      >
                        <Input
                          id="pf-gateway"
                          className="addr"
                          value={draft.gateway}
                          onChange={(e) => set("gateway", e.target.value)}
                          placeholder={suggested ?? "10.20.0.1/24"}
                          spellCheck={false}
                          aria-invalid={!!gatewayError}
                        />
                      </Field>
                    )}
                  </>
                ))}

              {step === 3 && (
                <>
                  <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 rounded-[var(--radius-control)] border border-line p-3 text-[13px]">
                    <dt className="text-muted">Prefix</dt>
                    <dd className="addr">{v?.normalized ?? draft.prefix}</dd>
                    <dt className="text-muted">Name</dt>
                    <dd>{draft.name.trim() || <span className="text-faint">None</span>}</dd>
                    <dt className="text-muted">Size</dt>
                    <dd>
                      {v?.size != null ? `${formatBig(BigInt(v.size))} addresses, ${formatBig(BigInt(v.usable ?? 0))} usable` : "—"}
                    </dd>
                    <dt className="text-muted">VRF</dt>
                    <dd>{selectedVrf?.label ?? "Global table"}</dd>
                    <dt className="text-muted">Status</dt>
                    <dd className="capitalize">{draft.status}</dd>
                    <dt className="text-muted">Site</dt>
                    <dd>{selectedSite?.label ?? <span className="text-faint">None</span>}</dd>
                    <dt className="text-muted">VLAN</dt>
                    <dd>{selectedVlan?.label ?? <span className="text-faint">None</span>}</dd>
                    <dt className="text-muted">Role</dt>
                    <dd>{selectedRole?.label ?? <span className="text-faint">None</span>}</dd>
                    <dt className="text-muted">Gateway</dt>
                    <dd className="addr">
                      {draft.gatewayEnabled && draft.status !== "container" && draft.gateway.trim() ? draft.gateway.trim() : <span className="font-sans text-faint">None</span>}
                    </dd>
                  </dl>
                  <ValidationFindings v={v} loading={validation.isFetching} error={validation.error} onlyLevels={["error", "warning", "info"]} />
                  {create.error && fieldError(create.error, "__all__", "prefix", "vlan", "vrf", "role", "gateway") && (
                    <Notice tone="danger" title={`${nbName} rejected the prefix`}>
                      {fieldError(create.error, "__all__", "prefix", "vlan", "vrf", "role", "gateway")}
                    </Notice>
                  )}
                  {create.error && !fieldError(create.error, "__all__", "prefix", "vlan", "vrf", "role", "gateway") && (
                    <Notice tone="danger">{(create.error as Error).message}</Notice>
                  )}
                </>
              )}
            </DialogBody>
          </div>
          <DialogFooter>
            {step > 0 && (
              <Button type="button" variant="ghost" onClick={() => setStep((s) => s - 1)} className="mr-auto">
                Back
              </Button>
            )}
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            {step < STEPS.length - 1 ? (
              <Button type="submit" variant="primary" disabled={!canAdvance[step]} loading={step === 0 && validation.isFetching}>
                Continue
              </Button>
            ) : (
              <Button type="submit" variant="primary" disabled={!canAdvance[3]} loading={create.isPending}>
                Create network
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ValidationFindings({
  v,
  loading,
  error,
  onlyLevels,
}: {
  v: PrefixValidation | undefined;
  loading: boolean;
  error: unknown;
  onlyLevels: ("error" | "warning" | "info")[];
}) {
  const nbName = useNetBoxLabel();
  if (error) return <Notice tone="danger" title={`Could not check the prefix against ${nbName}`}>{(error as Error).message}</Notice>;
  if (!v && loading) return <p className="text-xs text-muted">Checking against {nbName}…</p>;
  if (!v) return null;
  const items = v.findings.filter((f) => onlyLevels.includes(f.level) && f.field !== "prefix" && f.field !== "gateway");
  if (!items.length) return null;
  return (
    <ul className={cn("grid gap-1.5 text-[13px] transition-opacity", loading && "opacity-60")} aria-live="polite">
      {items.map((f) => {
        const Icon = f.level === "error" ? CircleAlert : f.level === "warning" ? TriangleAlert : Info;
        return (
          <li key={f.code} className="flex items-start gap-2">
            <Icon className={cn("mt-0.5 size-4 shrink-0", f.level === "error" ? "text-danger" : f.level === "warning" ? "text-warn" : "text-accent")} aria-hidden />
            <span>{f.message}</span>
          </li>
        );
      })}
    </ul>
  );
}
