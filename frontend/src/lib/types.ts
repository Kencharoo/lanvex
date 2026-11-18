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

// ---- Servers (live state read over SSH; never NetBox's documented state) ----------------------

export type AuthMethod = "generated_key" | "private_key" | "password";
export type ConnectionStatus = "ok" | "unreachable" | "auth_failed" | "host_key_mismatch" | "not_allowed" | "refused" | "error";

export interface NetBoxRef {
  object_type: "dcim.device" | "virtualization.virtualmachine" | string;
  object_id: number;
  name: string | null;
  url: string | null;
}

export interface Server {
  id: string;
  name: string;
  description: string;
  address: string;
  port: number;
  username: string;
  adapter: string;
  capabilities: string[];
  auth_method: AuthMethod;
  public_key: string | null;
  authorized_keys_line: string | null;
  host_key_fingerprint: string;
  host_key_algorithm: string | null;
  host_key_pinned_at: string;
  netbox: NetBoxRef | null;
  last_status: ConnectionStatus | null;
  last_status_message: string | null;
  last_checked_at: string | null;
  last_collected_at: string | null;
  os_pretty_name: string | null;
  os_supported: boolean | null;
  network_stack: string | null;
  changes_enabled: boolean;
  read_interval_minutes: number | null;
  read_interval: number;
  next_read_at: string | null;
  reads_paused: string | null;
  verify_address: string | null;
  protected_interfaces: string[];
  /** Changes are kept only when an administrator confirms them in the browser (Lanvex runs on this server). */
  confirm_in_browser: boolean;
  created_at: string;
}

export interface HostKeyScan {
  algorithm: string;
  fingerprint_sha256: string;
  public_key: string;
  resolved_addresses: string[];
}

export interface HostAddress {
  address: string;
  family: 4 | 6;
  scope: string | null;
  dynamic: boolean;
  secondary: boolean;
  deprecated: boolean;
  tentative: boolean;
}

export interface HostInterface {
  name: string;
  index: number;
  kind: string;
  mac: string | null;
  mtu: number | null;
  oper_state: string;
  admin_up: boolean;
  carrier: boolean;
  parent: string | null;
  master: string | null;
  vlan_id: number | null;
  addresses: HostAddress[];
  managed_by: string | null;
  manager_state: string | null;
  manager_profile: string | null;
}

export interface HostRoute {
  destination: string;
  family: 4 | 6;
  gateway: string | null;
  interface: string | null;
  metric: number | null;
  protocol: string | null;
  scope: string | null;
  source: string | null;
  type: string | null;
  flags: string[];
}

export interface HostFacts {
  collected_at: string;
  system: {
    hostname: string | null;
    kernel: string | null;
    iproute2_version: string | null;
    os: { id: string | null; pretty_name: string | null; version_id: string | null; supported: boolean; support_note: string };
  };
  interfaces: HostInterface[];
  routes: HostRoute[];
  neighbors?: { address: string; family: 4 | 6; interface: string; mac: string | null; state: string; router: boolean }[];
  lldp?: LldpNeighbor[];
  dns: {
    resolv_conf_servers: string[];
    resolv_conf_search: string[];
    uses_resolved_stub: boolean;
    resolved: { scope: string; servers: string[]; domains: string[] }[] | null;
    effective_servers: string[];
    source: string;
  };
  stack: { primary: string; key: string; units: Record<string, string>; netplan_files: string[]; ifupdown_interfaces: string[]; evidence: string[] };
  warnings: string[];
}

export interface SnapshotMeta {
  id: string;
  taken_at: string;
  status: ConnectionStatus;
  message: string | null;
  duration_ms: number | null;
  requested_by: string | null;
  confirmed_at: string | null;
  reads: number;
}

/** One state of a server and what differed from the readable state before it. */
export interface HistoryEntry {
  snapshot_id: string;
  taken_at: string;
  confirmed_at: string | null;
  reads: number;
  status: ConnectionStatus;
  message: string | null;
  requested_by: string | null;
  first: boolean;
  changes: { kind: "added" | "removed" | "changed"; text: string }[];
}

export interface Snapshot extends SnapshotMeta {
  facts: HostFacts | null;
}

export interface ComparisonRow {
  address: string;
  status: "match" | "mask_differs" | "interface_differs" | "only_on_server" | "only_in_netbox";
  server_address: string | null;
  server_interface: string | null;
  netbox_address: string | null;
  netbox_interface: string | null;
  netbox_ip_id: number | null;
  recorded_elsewhere: string | null;
}

export interface RecordAction {
  address: string | null;
  kind: "create_interface" | "create" | "assign" | "update_mask" | "move" | "unassign";
  text: string;
  problem: string | null;
}

/** NetBox writes that make the documentation match the server's last read (preview or result). */
export interface RecordPlan {
  target: NetBoxRef;
  actions: RecordAction[];
  token: string;
  applied: boolean;
  done: RecordAction[];
  failed: RecordAction | null;
  error: string | null;
}

/** Hosts in servers' neighbour tables, and LLDP links (discovery). */
export interface DiscoveredHost {
  address: string;
  family: 4 | 6;
  mac: string | null;
  vendor: string | null;
  router: boolean;
  first_seen: string;
  last_seen: string;
  seen_by: { server_id: string; server_name: string; interface: string; mac: string | null; last_seen: string }[];
  server: string | null;
  server_id: string | null;
  netbox: { id: number; address: string; assigned_to: string | null; url: string | null } | null;
  record_as: string | null;
  switch_port: { switch_id: string; switch_name: string; port: string; vlan: number | null; last_seen: string; current: boolean } | null;
}

export interface DiscoveredHosts {
  hosts: DiscoveredHost[];
  total: number;
  undocumented: number;
  netbox_checked: boolean;
}

export interface LldpNeighbor {
  interface: string;
  source: "lldpd" | "networkd";
  chassis_id: string | null;
  system_name: string | null;
  system_description: string | null;
  port_id: string | null;
  port_description: string | null;
  management_addresses: string[];
  capabilities: string[];
}

export interface DiscoveredLink {
  key: string;
  server_id: string;
  server_name: string;
  interface: string;
  neighbor: LldpNeighbor;
  seen_at: string;
  status: "documented" | "different" | "undocumented" | "several" | "not_linked" | "virtual";
  documented: string | null;
  peer: string | null;
  problem: string | null;
}

export interface DiscoveredLinks {
  links: DiscoveredLink[];
  servers_read: number;
  servers_without_lldp: string[];
}

/** A preview of NetBox writes, or their result (discovery). */
export interface WritePlan {
  actions: RecordAction[];
  token: string;
  applied: boolean;
  done: RecordAction[];
  failed: RecordAction | null;
  error: string | null;
}

export interface Comparison {
  linked: boolean;
  netbox: NetBoxRef | null;
  suggestions: NetBoxRef[];
  snapshot_taken_at: string | null;
  rows: ComparisonRow[];
  counts: Record<string, number>;
}

export interface SystemInfo {
  version: string;
  environment: string;
  automation_enabled: boolean;
  /** Lanvex Core (the bundled NetBox-based application) is served under /core/. */
  core_enabled: boolean;
  netbox_label: string;
  credential_storage: boolean;
  server_allowed_networks: string[];
}

// ---- configuration changes ----------------------------------------------------------------------------

export type ChangeStatus =
  | "pending_approval"
  | "approved"
  | "running"
  | "verifying"
  | "succeeded"
  | "rolled_back"
  | "failed"
  | "cancelled"
  | "needs_attention";

export interface ChangeRequestBody {
  interface: string;
  method: "static" | "dhcp";
  addresses: string[];
  gateway: string | null;
  /** null keeps the interface's DNS settings exactly as they are. */
  dns_servers: string[] | null;
  dns_search: string[] | null;
  verify_dns_name?: string | null;
  /** Left out (or null): the interface's IPv6 settings stay as they are. */
  ipv6?: Ipv6Settings | null;
  /** Left out (or null): its static routes stay as they are; [] removes them. */
  routes?: StaticRoute[] | null;
}

export interface Ipv6Settings {
  method: "auto" | "static" | "link-local";
  addresses: string[];
  gateway: string | null;
}

export interface StaticRoute {
  to: string;
  via: string;
  metric?: number | null;
}

export interface VlanSpec {
  name: string;
  parent: string;
  id: number;
  addresses: string[];
}

export interface DhcpPoolSpec {
  interface: string;
  start: string;
  end: string;
  lease: string;
  router: string | null;
  dns: string[] | null;
  reservations: { mac: string; ip: string; name: string }[];
}

/** Every VLAN interface Lanvex manages on a server, and the DHCP pools on them. */
export interface AccessDestination {
  network: string;
  ports: string[];
}

/** What devices on one Lanvex network may reach. Default: DHCP and DNS on the server, nothing else. */
export interface AccessSettings {
  internet: boolean;
  destinations: AccessDestination[];
  services: string[];
  /** Names devices cannot look up (each with every name below it), except the exceptions. null: off. */
  dns_filter?: { block: string[]; allow: string[] } | null;
}

export interface AccessRule extends AccessSettings {
  interface: string;
}

export interface NetworksRequestBody {
  kind: "networks";
  vlans: VlanSpec[];
  dhcp: { pools: DhcpPoolSpec[] } | null;
  access?: { rules: AccessRule[] } | null;
}

export interface InterfaceBefore {
  method: "static" | "dhcp";
  addresses: string[];
  dynamic_addresses: string[];
  gateway: string | null;
  dns_servers: string[];
  dns_search: string[];
  dns_known: boolean;
  managed_by: string | null;
  /** Absent in changes prepared before 1.3. */
  ipv6?: Ipv6Settings & { dynamic_addresses: string[] };
  routes?: StaticRoute[];
}

export interface ChangeOut {
  id: string;
  number: number;
  server_id: string;
  server_name: string;
  interface: string;
  summary: string;
  status: ChangeStatus;
  requested_by: string;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  outcome: string | null;
  /** Applied and checked; kept only if an administrator keeps it in the browser in time. */
  awaiting_browser: boolean;
}

export interface ChangeCheck {
  id: string;
  label: string;
  ok: boolean | null;
  detail: string;
}

export type ChangeDetail = ChangeDetailBase &
  (
    | { kind: "interface_ipv4"; request: ChangeRequestBody; before: InterfaceBefore | null }
    | { kind: "networks"; request: NetworksRequestBody; before: { vlans: VlanSpec[]; dhcp: null; access?: AccessRule[] } | null }
    | { kind: "link"; request: LinkRequestBody; before: { members?: string[]; type?: "bridge" | "bond"; name?: string } | null }
  );

export interface LinkRequestBody {
  kind: "link";
  action: "create" | "remove";
  name: string;
  type?: "bridge" | "bond" | null;
  members?: string[];
  mode?: string | null;
  stp?: boolean;
  method?: "none" | "static" | "dhcp";
  addresses?: string[];
  gateway?: string | null;
}

/** A bridge or bond Lanvex created on a server. */
export interface LanvexLink {
  name: string;
  type: "bridge" | "bond";
  members: string[];
  mode: string | null;
  method: "none" | "static" | "dhcp";
  addresses: string[];
}

interface ChangeDetailBase extends ChangeOut {
  preview: {
    backend: string;
    files: { path: string; label: string; diff: string; before: string; after: string }[];
    commands: string[];
    warnings: string[];
  } | null;
  rollback_seconds: number;
  deadline_at: string | null;
  checks: ChangeCheck[];
  log: { at: string; level: "info" | "warning" | "error"; message: string }[];
  can_approve: boolean;
  approve_blocker: string | null;
  can_cancel: boolean;
  can_recheck: boolean;
  confirm_in_browser: boolean;
  browser: {
    waiting_since: string | null;
    decision: "keep" | "undo" | null;
    by: string | null;
    at: string | null;
    host: string | null;
    on_server: boolean | null;
  } | null;
  can_decide: boolean;
  /** How this browser reaches Lanvex, and whether that is one of the changed server's own addresses. */
  your_address: { host: string; on_server: boolean; interface: string | null; vpn: boolean } | null;
}

export type HelperState = "unknown" | "ok" | "outdated" | "missing" | "no_sudo" | "no_python" | "timeout" | "error";

export interface ChangeReadiness {
  automation_enabled: boolean;
  changes_enabled: boolean;
  stack_key: string | null;
  stack_label: string | null;
  stack_supported: boolean | null;
  /** Interface settings can be changed here (netplan, NetworkManager); on ifupdown only networks. */
  interface_changes: boolean | null;
  helper: { state: HelperState; version: number | null; expected_version: number; message: string | null; checked_at: string | null };
  helper_path: string;
  helper_sha256: string;
  ready: boolean;
  blockers: string[];
  rollback_seconds: number;
  second_admin_required: boolean;
  open_change: ChangeOut | null;
  /** Which of the server's interfaces carry Lanvex's connection, as of its last read. */
  connection: { address: string; interfaces: string[]; verify_address: string | null; verify_interfaces: string[] } | null;
  protected_interfaces: string[];
  confirm_in_browser: boolean;
  links: LanvexLink[];
}

export interface HelperInstall {
  path: string;
  sha256: string;
  commands: string[];
}

/** Network topology: documented devices and circuits as nodes, documented cables as links. */
export type TopologyNodeKind = "device" | "circuit";
export type TopologyLinkKind = "data" | "patch" | "console" | "power" | "circuit";
export interface TopologyNode {
  id: string;
  kind: TopologyNodeKind;
  object_id: number;
  name: string;
  role: Ref | null;
  color: string | null;
  site: Ref | null;
  status: Choice | null;
  device_type: string | null;
  primary_ip: string | null;
  provider: string | null;
  external: boolean;
  server_id: string | null;
  switch_id: string | null;
  netbox_url: string | null;
}
/** A connection a switch sees on a port that the documented cables do not show. */
export interface SeenLink {
  id: string;
  a: string;
  b: string;
  a_port: string;
  b_port: string | null;
  status: "undocumented" | "different";
}
export interface TopologyLink {
  id: string;
  cable_id: number;
  a: string;
  b: string;
  a_ports: string[];
  b_ports: string[];
  kind: TopologyLinkKind;
  status: Choice | null;
  type: string | null;
  label: string;
  color: string | null;
  netbox_url: string | null;
}
export interface Topology {
  nodes: TopologyNode[];
  links: TopologyLink[];
  seen: SeenLink[];
  sites: Ref[];
  site_id: number | null;
  truncated: boolean;
  skipped_cables: number;
}

// ---- networks on a server ----------------------------------------------------------------------------

export type NetworkStatus = "in_sync" | "missing" | "differs" | "removed" | "protected" | "foreign" | "incomplete" | "unknown";
export type DhcpStatus = "in_sync" | "missing" | "differs" | "removed" | "none";

export interface NetworkLease {
  ip: string;
  mac: string;
  hostname: string | null;
  expires_at: string | null;
  fixed: boolean;
}

export interface ServerNetwork {
  name: string;
  parent: string | null;
  vlan_id: number | null;
  vlan_name: string | null;
  addresses: string[];
  network: string | null;
  netbox_interface_id: number | null;
  status: NetworkStatus;
  note: string | null;
  on_server: { parent: string | null; id: number | null; addresses: string[] } | null;
  pool: { start: string; end: string; lease: string; ip_range_id: number | null } | null;
  /** device: the device interface the address is assigned to in Lanvex Core ("nas eth0"), if any. */
  fixed: { ip: string; mac: string; name: string | null; ip_id: number | null; device: string | null }[];
  dhcp: DhcpStatus | null;
  leases: NetworkLease[];
  access: AccessSettings | null;
  access_on_server: AccessSettings | null;
  access_status: "in_sync" | "differs" | "invalid" | null;
  access_problem: string | null;
}

export interface ImportReport {
  found: boolean;
  source: string;
  /** completed: documented before; what it lacked was added (the notes say what). */
  networks: { name: string; result: "documented" | "completed" | "already documented" | "failed"; notes: string[] }[];
  view: NetworksView;
}

export interface NetworksView {
  linked: boolean;
  supported: boolean | null;
  stack_label: string | null;
  read_at: string | null;
  networks: ServerNetwork[];
  in_sync: boolean;
  change: NetworksRequestBody | null;
  missing_fields: string[];
  other_leases: NetworkLease[];
}

// ---- monitoring ----------------------------------------------------------------------------------------

export type AlertKind = "unreachable" | "read_failed" | "drift" | "pool_full" | "unknown_device" | "change_failed" | "port_down" | "device_moved";
export type Severity = "critical" | "warning" | "info";
export type WebhookFormat = "generic" | "ntfy" | "gotify" | "slack" | "discord" | "teams";

export interface Alert {
  id: string;
  kind: AlertKind;
  server_id: string | null;
  switch_id: string | null;
  /** The server's or the switch's name. */
  server_name: string;
  key: string;
  severity: Severity;
  title: string;
  detail: string[];
  state: "open" | "resolved";
  opened_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
}

export interface AlertList {
  alerts: Alert[];
  open: number;
  unacknowledged: number;
}

export interface AlertCount {
  open: number;
  unacknowledged: number;
  critical: number;
}

export interface MonitoringSettings {
  unreachable: boolean;
  read_failed: boolean;
  drift: boolean;
  pool_full: boolean;
  unknown_device: boolean;
  change_failed: boolean;
  port_down: boolean;
  device_moved: boolean;
  unreachable_after: number;
  pool_threshold: number;
  drift_ignore_interfaces: string[];
  unknown_device_ignore: string[];
  email_enabled: boolean;
  email_to: string[];
  email_min_severity: Severity;
  notify_resolved: boolean;
}

export interface MonitoringOut {
  settings: MonitoringSettings;
  smtp_configured: boolean;
  smtp_from: string | null;
  probe_seconds: number;
  enabled: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface Webhook {
  id: string;
  name: string;
  format: WebhookFormat;
  url_hint: string;
  enabled: boolean;
  min_severity: Severity;
  created_at: string;
}

export interface DeliveryResult {
  channel: string;
  ok: boolean;
  error: string | null;
}

// ---- switches (read over SNMP) ---------------------------------------------------------------------------

export type SnmpAuthProtocol = "none" | "md5" | "sha" | "sha224" | "sha256" | "sha384" | "sha512";
export type SnmpPrivProtocol = "none" | "des" | "aes" | "aes192" | "aes256" | "aes192c" | "aes256c";
export interface SnmpCredentials {
  version: "2c" | "3";
  community?: string;
  username?: string;
  auth_protocol?: SnmpAuthProtocol;
  auth_password?: string;
  priv_protocol?: SnmpPrivProtocol;
  priv_password?: string;
}
export interface SnmpSummary {
  version: "2c" | "3";
  username: string | null;
  auth_protocol: SnmpAuthProtocol | null;
  priv_protocol: SnmpPrivProtocol | null;
}
export type SwitchStatus = "ok" | "unreachable" | "auth_failed" | "not_allowed" | "error";
export interface Switch {
  id: string;
  name: string;
  description: string;
  address: string;
  port: number;
  snmp: SnmpSummary;
  netbox: { id: number; name: string | null; url: string | null } | null;
  read_interval_minutes: number | null;
  read_interval: number;
  next_read_at: string | null;
  last_status: SwitchStatus | null;
  last_status_message: string | null;
  last_checked_at: string | null;
  last_read_at: string | null;
  sys_name: string | null;
  sys_description: string | null;
  ports_total: number | null;
  ports_up: number | null;
  mac_count: number | null;
  /** VLAN changes may be made on this switch (LANVEX_AUTOMATION_ENABLED must be on too). */
  changes_enabled: boolean;
  /** Credentials of their own for changes; null: changes use the reading credentials. */
  write_snmp: SnmpSummary | null;
  created_at: string;
}
export interface SwitchChangeOut {
  id: string;
  number: number;
  switch_id: string;
  switch_name: string;
  if_index: number;
  port: string;
  summary: string;
  status: ChangeStatus;
  requested_by: string;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  outcome: string | null;
}
export interface SwitchChangeDetail extends SwitchChangeOut {
  request: { untagged: number; tagged: number[] };
  before: { pvid: number | null; untagged: number[]; tagged: number[] };
  writes: { text: string; oids: string[] }[];
  warnings: string[];
  log: { at: string; level: "info" | "warning" | "error"; message: string }[];
  approve_blocker: string | null;
}
export interface SwitchChanges {
  automation_enabled: boolean;
  changes_enabled: boolean;
  open: SwitchChangeOut | null;
  recent: SwitchChangeOut[];
}
export interface SwitchSystem {
  name: string | null;
  description: string | null;
  object_id: string | null;
  uptime_seconds: number | null;
  location: string | null;
  bridge_mac: string | null;
  lldp_chassis_id: string | null;
  lldp_system_name: string | null;
}
export interface SwitchPort {
  index: number;
  name: string;
  description: string | null;
  alias: string | null;
  type: number | null;
  kind: "ethernet" | "lag" | "vlan" | "loopback" | "wireless" | "other";
  mac: string | null;
  admin_up: boolean | null;
  oper_up: boolean | null;
  speed_mbps: number | null;
  bridge_port: number | null;
  lag: number | null;
  pvid: number | null;
  untagged: number[];
  tagged: number[];
  trunk: boolean | null;
}
export interface SwitchNeighbor {
  port: number | null;
  port_name: string | null;
  chassis_id: string | null;
  port_id: string | null;
  port_description: string | null;
  system_name: string | null;
  system_description: string | null;
  capabilities: string[];
  management_addresses: string[];
}
export interface SwitchFacts {
  collected_at: string;
  system: SwitchSystem;
  ports: SwitchPort[];
  vlans: { id: number; name: string | null }[];
  fdb: { mac: string; vlan: number | null; port: number }[];
  neighbors: SwitchNeighbor[];
  arp: { address: string; mac: string; interface: number | null }[];
  notes: string[];
}
export interface PortEndpoint {
  via: "lldp" | "mac";
  kind: "server" | "device" | "vm" | "switch" | "unknown";
  name: string | null;
  interface: string | null;
  mac: string | null;
  vendor: string | null;
  vlan: number | null;
  addresses: string[];
  server_id: string | null;
  switch_id: string | null;
  netbox_url: string | null;
  last_seen: string;
  current: boolean;
}
export type PortStatus = "documented" | "different" | "undocumented" | "unidentified" | "shared" | "virtual" | "not_linked" | "no_port" | "not_checked";
export interface PortFinding {
  key: string;
  switch_id: string;
  switch_name: string;
  if_index: number;
  port: string;
  alias: string | null;
  uplink: boolean;
  endpoints: PortEndpoint[];
  status: PortStatus;
  documented: string | null;
  peer: string | null;
  problem: string | null;
}
export interface PortFindings {
  ports: PortFinding[];
  switches_read: number;
  netbox_checked: boolean;
}
export interface SwitchDetail extends Switch {
  facts: SwitchFacts | null;
  findings: PortFinding[];
  findings_error: string | null;
}
export interface SwitchTestResult {
  status: SwitchStatus;
  message: string;
  system: SwitchSystem | null;
}
