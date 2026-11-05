/** Mirrors the Lanvex API response schemas in backend/lanvex. Not NetBox raw payloads. */

export interface Ref {
  id: number;
  name: string;
  slug: string | null;
}
export interface Choice {
  value: string;
  label: string;
}
export interface Tag {
  id: number;
  name: string;
  slug: string;
  color: string | null;
}
export interface Meta {
  created: string | null;
  last_updated: string | null;
  netbox_url: string | null;
}
export interface Page<T> {
  count: number;
  limit: number;
  offset: number;
  results: T[];
}

export type Role = "admin" | "readonly";
export interface User {
  id: string;
  username: string;
  display_name: string | null;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}
export interface SessionInfo {
  user: User;
  csrf_token: string;
  expires_at: string;
}

export interface VlanRef {
  id: number;
  vid: number;
  name: string;
}
export interface Utilization {
  percent: number;
  used: number;
  total: number;
  basis: "child_prefixes" | "ip_addresses" | "marked_utilized";
}
export interface Prefix {
  id: number;
  prefix: string;
  family: 4 | 6;
  name: string;
  status: Choice;
  vrf: Ref | null;
  vlan: VlanRef | null;
  role: Ref | null;
  tenant: Ref | null;
  scope_type: string | null;
  scope: Ref | null;
  is_pool: boolean;
  mark_utilized: boolean;
  depth: number;
  children: number;
  comments: string;
  tags: Tag[];
  utilization: Utilization | null;
  gateway: { ip_address_id: number; address: string } | null;
  meta: Meta;
}
export type PrefixStatus = "container" | "active" | "reserved" | "deprecated";

export interface Finding {
  level: "error" | "warning" | "info";
  code: string;
  message: string;
  field: string | null;
}
export interface PrefixValidation {
  valid: boolean;
  normalized: string | null;
  family: number | null;
  size: number | null;
  usable: number | null;
  suggested_gateway: string | null;
  findings: Finding[];
  related: { id: number; prefix: string; name: string; status: string; relation: "duplicate" | "parent" | "child" }[];
}
export interface WriteResult {
  state: "documented";
  warnings: string[];
}
export interface AvailablePrefix {
  prefix: string;
  family: number;
  size: number;
}
export interface AvailableIP {
  address: string;
  family: number;
}

export type IPStatus = "active" | "reserved" | "deprecated" | "dhcp" | "slaac";
export interface IPAddress {
  id: number;
  address: string;
  family: 4 | 6;
  status: Choice;
  role: Choice | null;
  vrf: Ref | null;
  tenant: Ref | null;
  dns_name: string;
  description: string;
  assigned: { interface_id: number; interface_name: string; device: Ref | null; object_type: string } | null;
  is_gateway: boolean;
  tags: Tag[];
  meta: Meta;
}

export interface VLAN {
  id: number;
  vid: number;
  name: string;
  status: Choice;
  site: Ref | null;
  group: Ref | null;
  role: Ref | null;
  tenant: Ref | null;
  description: string;
  prefix_count: number;
  tags: Tag[];
  meta: Meta;
}

export interface Device {
  id: number;
  name: string;
  status: Choice;
  role: Ref | null;
  device_type: { id: number; model: string; manufacturer: Ref | null } | null;
  platform: Ref | null;
  site: Ref | null;
  location: Ref | null;
  rack: Ref | null;
  tenant: Ref | null;
  serial: string;
  asset_tag: string | null;
  primary_ip: string | null;
  oob_ip: string | null;
  interface_count: number;
  description: string;
  meta: Meta;
}
export interface Interface {
  id: number;
  name: string;
  type: Choice | null;
  enabled: boolean;
  mgmt_only: boolean;
  mtu: number | null;
  mac_address: string | null;
  description: string;
  mode: Choice | null;
  untagged_vlan: VlanRef | null;
  tagged_vlans: VlanRef[];
  connected: boolean;
  lag: Ref | null;
}
export interface DeviceDetail extends Device {
  comments: string;
  interfaces: Interface[];
  ip_addresses: IPAddress[];
  vlans: VlanRef[];
}

export interface Option {
  id: number;
  label: string;
  hint: string | null;
}

export interface NetBoxStatus {
  configured: boolean;
  url: string | null;
  reachable: boolean;
  authenticated: boolean;
  version: string | null;
  supported: boolean | null;
  minimum_version: string;
  latency_ms: number | null;
  message: string | null;
}

export interface Overview {
  counts: {
    prefixes: number;
    prefixes_v4: number;
    prefixes_v6: number;
    ip_addresses: number;
    ip_assigned: number;
    ip_unassigned: number;
    vlans: number;
    devices: number;
    vrfs: number;
    sites: number;
  };
  ip_status: { status: string; count: number }[];
  top_utilized: { id: number; prefix: string; name: string; vrf: string | null; percent: number; used: number; total: number }[];
  utilization_sampled: number;
  recent_changes: { time: string; user: string | null; action: string; object_type: string; object_repr: string; source: "netbox" | "lanvex" }[];
  recent_changes_available: boolean;
  recent_changes_message: string | null;
  generated_at: string;
}

export interface AuditEvent {
  id: string;
  occurred_at: string;
  actor_username: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  target_repr: string | null;
  outcome: "success" | "failure" | "denied";
  detail: Record<string, unknown> | null;
  ip_address: string | null;
  request_id: string | null;
}
