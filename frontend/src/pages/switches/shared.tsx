import { useQuery } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router";
import { Field, Input, Select } from "@/components/ui/form";
import { Badge, type Tone } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useNetBoxLabel } from "@/lib/system";
import type { NetBoxRef, PortEndpoint, PortStatus, SnmpAuthProtocol, SnmpCredentials, SnmpPrivProtocol, SwitchStatus } from "@/lib/types";
import { relativeTime } from "@/lib/utils";

const STATUS: Record<SwitchStatus, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "Reachable" },
  unreachable: { tone: "danger", label: "No answer" },
  auth_failed: { tone: "danger", label: "Sign-in failed" },
  not_allowed: { tone: "warn", label: "Address not allowed" },
  error: { tone: "danger", label: "Error" },
};

export function SwitchStatusBadge({ status }: { status: SwitchStatus | null }) {
  if (!status) return <Badge>Not read</Badge>;
  const s = STATUS[status] ?? STATUS.error;
  return (
    <Badge tone={s.tone}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {s.label}
    </Badge>
  );
}

export const AUTH: [SnmpAuthProtocol, string][] = [
  ["sha", "SHA-1"],
  ["sha256", "SHA-256"],
  ["sha512", "SHA-512"],
  ["sha224", "SHA-224"],
  ["sha384", "SHA-384"],
  ["md5", "MD5"],
  ["none", "None"],
];
export const PRIV: [SnmpPrivProtocol, string][] = [
  ["aes", "AES-128"],
  ["aes256", "AES-256"],
  ["aes192", "AES-192"],
  ["aes256c", "AES-256 (Cisco)"],
  ["aes192c", "AES-192 (Cisco)"],
  ["des", "DES"],
  ["none", "None"],
];

export const EMPTY_SNMP: Required<SnmpCredentials> = {
  version: "3",
  community: "",
  username: "",
  auth_protocol: "sha",
  auth_password: "",
  priv_protocol: "aes",
  priv_password: "",
};

export function snmpPayload(c: Required<SnmpCredentials>): SnmpCredentials {
  if (c.version === "2c") return { version: "2c", community: c.community };
  const auth = c.auth_protocol !== "none";
  const priv = auth && c.priv_protocol !== "none";
  return {
    version: "3",
    username: c.username.trim(),
    auth_protocol: c.auth_protocol,
    auth_password: auth ? c.auth_password : undefined,
    priv_protocol: priv ? c.priv_protocol : "none",
    priv_password: priv ? c.priv_password : undefined,
  };
}

export function snmpComplete(c: Required<SnmpCredentials>): boolean {
  if (c.version === "2c") return c.community.length > 0;
  if (!c.username.trim()) return false;
  if (c.auth_protocol === "none") return true;
  if (c.auth_password.length < 8) return false;
  return c.priv_protocol === "none" || c.priv_password.length >= 8;
}

export function SnmpForm({ value: c, onChange, idPrefix = "snmp" }: { value: Required<SnmpCredentials>; onChange: (c: Required<SnmpCredentials>) => void; idPrefix?: string }) {
  const id = (name: string) => `${idPrefix}-${name}`;
  const set = (patch: Partial<SnmpCredentials>) => onChange({ ...c, ...patch } as Required<SnmpCredentials>);
  const short = (v: string) => (v.length > 0 && v.length < 8 ? "At least 8 characters" : undefined);
  return (
    <div className="grid gap-4">
      <Field label="SNMP version" htmlFor={id("version")}>
        <Select id={id("version")} value={c.version} onChange={(e) => set({ version: e.target.value as "2c" | "3" })}>
          <option value="3">SNMPv3</option>
          <option value="2c">SNMPv2c (community sent unencrypted)</option>
        </Select>
      </Field>
      {c.version === "2c" ? (
        <Field label="Community" htmlFor={id("community")}>
          <Input id={id("community")} type="password" value={c.community} onChange={(e) => set({ community: e.target.value })} autoComplete="off" spellCheck={false} />
        </Field>
      ) : (
        <>
          <Field label="User" htmlFor={id("user")}>
            <Input id={id("user")} className="addr" value={c.username} onChange={(e) => set({ username: e.target.value })} autoComplete="off" spellCheck={false} />
          </Field>
          <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
            <Field label="Authentication" htmlFor={id("auth")}>
              <Select id={id("auth")} value={c.auth_protocol} onChange={(e) => set({ auth_protocol: e.target.value as SnmpAuthProtocol })}>
                {AUTH.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </Select>
            </Field>
            {c.auth_protocol !== "none" && (
              <Field label="Authentication password" htmlFor={id("auth-pw")} error={short(c.auth_password)}>
                <Input id={id("auth-pw")} type="password" value={c.auth_password} onChange={(e) => set({ auth_password: e.target.value })} autoComplete="off" />
              </Field>
            )}
          </div>
          {c.auth_protocol !== "none" && (
            <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
              <Field label="Privacy" htmlFor={id("priv")}>
                <Select id={id("priv")} value={c.priv_protocol} onChange={(e) => set({ priv_protocol: e.target.value as SnmpPrivProtocol })}>
                  {PRIV.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </Select>
              </Field>
              {c.priv_protocol !== "none" && (
                <Field label="Privacy password" htmlFor={id("priv-pw")} error={short(c.priv_password)}>
                  <Input id={id("priv-pw")} type="password" value={c.priv_password} onChange={(e) => set({ priv_password: e.target.value })} autoComplete="off" />
                </Field>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function snmpLabel(s: { version: "2c" | "3"; username: string | null; auth_protocol: string | null; priv_protocol: string | null }): string {
  if (s.version === "2c") return "SNMPv2c";
  const auth = AUTH.find(([v]) => v === s.auth_protocol)?.[1];
  const priv = PRIV.find(([v]) => v === s.priv_protocol)?.[1];
  const level = !s.auth_protocol || s.auth_protocol === "none" ? "no authentication" : !s.priv_protocol || s.priv_protocol === "none" ? auth : `${auth}, ${priv}`;
  return `SNMPv3 as ${s.username ?? "?"} (${level})`;
}

export const PORT_STATUS: Record<PortStatus, { tone: Tone; label: string }> = {
  documented: { tone: "ok", label: "Documented" },
  different: { tone: "danger", label: "Different cable" },
  undocumented: { tone: "warn", label: "No cable recorded" },
  unidentified: { tone: "neutral", label: "Unknown device" },
  shared: { tone: "neutral", label: "Several devices" },
  virtual: { tone: "neutral", label: "Virtual machines" },
  not_linked: { tone: "neutral", label: "Switch not linked" },
  no_port: { tone: "warn", label: "Port not documented" },
  not_checked: { tone: "neutral", label: "Not compared" },
};

export function PortStatusBadge({ status }: { status: PortStatus }) {
  const s = PORT_STATUS[status];
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

const KIND: Record<PortEndpoint["kind"], string> = { server: "Server", device: "Device", vm: "VM", switch: "Switch", unknown: "" };

/** One thing seen on a port: what it is, and how it was seen. */
export function EndpointLine({ e }: { e: PortEndpoint }) {
  const name = e.name ?? e.mac ?? "Unknown";
  const to = e.server_id ? `/servers/${e.server_id}` : e.switch_id ? `/switches/${e.switch_id}` : null;
  return (
    <span className="grid text-[13px]">
      <span className="flex flex-wrap items-center gap-1.5">
        {KIND[e.kind] && <span className="text-xs text-muted">{KIND[e.kind]}</span>}
        {to ? (
          <Link to={to} className="font-medium hover:underline">
            {name}
          </Link>
        ) : e.netbox_url ? (
          <a href={e.netbox_url} target="_blank" rel="noreferrer" className="font-medium hover:underline">
            {name}
          </a>
        ) : (
          <span className={e.name ? "font-medium" : "addr"}>{name}</span>
        )}
        {e.interface && <span className="text-muted">/ {e.interface}</span>}
        {!e.current && (
          <span className="text-xs text-faint" title={e.last_seen}>
            seen {relativeTime(e.last_seen)}
          </span>
        )}
      </span>
      <span className="addr text-xs text-muted">
        {[e.via === "lldp" ? "LLDP" : null, e.name ? e.mac : null, e.name ? null : e.vendor, e.vlan != null ? `VLAN ${e.vlan}` : null, ...e.addresses.slice(0, 2)]
          .filter(Boolean)
          .join(" · ")}
      </span>
    </span>
  );
}

export function speed(mbps: number | null): string | null {
  if (!mbps) return null;
  return mbps >= 1000 ? `${mbps % 1000 === 0 ? mbps / 1000 : (mbps / 1000).toFixed(1)} Gbit/s` : `${mbps} Mbit/s`;
}

export function uptime(seconds: number | null): string | null {
  if (seconds == null) return null;
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d > 0 ? `${d} d ${h} h` : h > 0 ? `${h} h ${m} min` : `${m} min`;
}

/** Pick the switch's device in NetBox: search by name, then choose. */
export function DevicePicker({ value, onChange }: { value: { id: number; name: string | null } | null; onChange: (v: { id: number; name: string | null } | null) => void }) {
  const nbName = useNetBoxLabel();
  const [q, setQ] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const results = useQuery({
    queryKey: ["servers", "link-candidates", debounced],
    queryFn: () => api<{ results: NetBoxRef[] }>("/servers/link-candidates", { query: { q: debounced } }),
    enabled: debounced.length > 0,
  });
  const options = (results.data?.results ?? []).filter((o) => o.object_type === "dcim.device");
  return (
    <div className="grid gap-2 sm:grid-cols-2 sm:items-end">
      <Field label={`Device in ${nbName}`} htmlFor="sw-device-q" optional>
        <Input id="sw-device-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder={value?.name ?? "Search by name"} />
      </Field>
      <Field label="Device" htmlFor="sw-device">
        <Select
          id="sw-device"
          value={value ? String(value.id) : ""}
          onChange={(e) => {
            const id = Number(e.target.value);
            const o = options.find((x) => x.object_id === id);
            onChange(id ? { id, name: o?.name ?? value?.name ?? null } : null);
          }}
        >
          <option value="">{debounced && results.isFetching ? "Searching…" : "None"}</option>
          {value && !options.some((o) => o.object_id === value.id) && <option value={value.id}>{value.name ?? `Device ${value.id}`}</option>}
          {options.map((o) => (
            <option key={o.object_id} value={o.object_id}>
              {o.name}
            </option>
          ))}
        </Select>
      </Field>
    </div>
  );
}
