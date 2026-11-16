"""Normalised facts about a Linux host, as read over SSH. This is *actual* state, never documented state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OSInfo(BaseModel):
    id: str | None = None
    id_like: list[str] = Field(default_factory=list)
    name: str | None = None
    pretty_name: str | None = None
    version_id: str | None = None
    variant_id: str | None = None
    supported: bool = False
    support_note: str


class SystemInfo(BaseModel):
    hostname: str | None
    kernel: str | None
    iproute2_version: str | None
    os: OSInfo


class Address(BaseModel):
    address: str  # with prefix length, e.g. 172.30.0.11/24
    family: Literal[4, 6]
    scope: str | None = None
    dynamic: bool = False  # assigned by DHCP / SLAAC (has a finite lifetime)
    secondary: bool = False
    deprecated: bool = False
    tentative: bool = False
    label: str | None = None


class Interface(BaseModel):
    name: str
    index: int
    kind: str  # ethernet, loopback, bridge, vlan, bond, veth, wireguard, …
    mac: str | None = None
    mtu: int | None = None
    oper_state: str  # up, down, unknown, dormant, …
    admin_up: bool
    carrier: bool
    parent: str | None = None  # lower device of a VLAN, macvlan, …
    master: str | None = None  # bridge or bond this interface belongs to
    vlan_id: int | None = None
    addresses: list[Address] = Field(default_factory=list)
    managed_by: str | None = None  # NetworkManager, systemd-networkd, ifupdown
    manager_state: str | None = (
        None  # the manager's own word: configured, connected (externally), unmanaged …
    )
    manager_profile: str | None = None  # NetworkManager connection name


class Route(BaseModel):
    destination: str  # CIDR; default routes are 0.0.0.0/0 or ::/0
    family: Literal[4, 6]
    gateway: str | None = None
    interface: str | None = None
    metric: int | None = None
    protocol: str | None = None  # kernel, static, dhcp, boot, ra …
    scope: str | None = None
    source: str | None = None  # preferred source address
    type: str | None = None  # unicast unless stated (blackhole, unreachable, …)
    flags: list[str] = Field(default_factory=list)

    @property
    def is_default(self) -> bool:
        return self.destination in ("0.0.0.0/0", "::/0")


class ResolverScope(BaseModel):
    scope: str  # "global" or an interface name
    servers: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class DNSInfo(BaseModel):
    resolv_conf_servers: list[str] = Field(default_factory=list)
    resolv_conf_search: list[str] = Field(default_factory=list)
    resolv_conf_options: list[str] = Field(default_factory=list)
    uses_resolved_stub: bool = False  # resolv.conf points at systemd-resolved's 127.0.0.53
    resolved: list[ResolverScope] | None = None  # None when systemd-resolved is not running
    effective_servers: list[str] = Field(default_factory=list)
    source: str  # where effective_servers came from


class NetworkStack(BaseModel):
    primary: str  # human label, e.g. "netplan → systemd-networkd"
    key: Literal[
        "netplan-networkd", "netplan-networkmanager", "networkmanager", "networkd", "ifupdown", "unknown"
    ]
    units: dict[str, str]  # unit -> is-active state
    netplan_files: list[str] = Field(default_factory=list)
    ifupdown_interfaces: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Neighbor(BaseModel):
    """A host this server exchanged packets with recently (ARP for IPv4, neighbour discovery for IPv6)."""

    address: str
    family: Literal[4, 6]
    interface: str
    mac: str | None = None
    state: str  # REACHABLE, STALE, DELAY, PROBE, PERMANENT, …
    router: bool = False


class LldpNeighbor(BaseModel):
    """What the device on the other end of an interface announces about itself with LLDP."""

    interface: str  # this server's interface
    source: Literal["lldpd", "networkd"]
    chassis_id: str | None = None
    system_name: str | None = None
    system_description: str | None = None
    port_id: str | None = None
    port_description: str | None = None
    management_addresses: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)  # enabled ones: Bridge, Router, …


class DhcpReservation(BaseModel):
    mac: str
    ip: str
    name: str | None = None


class DhcpPool(BaseModel):
    """A pool in Lanvex's own dnsmasq file (/etc/dnsmasq.d/lanvex.conf)."""

    interface: str
    start: str
    end: str
    netmask: str | None = None
    lease: str | None = None
    router: str | None = None
    dns: list[str] = Field(default_factory=list)
    reservations: list[DhcpReservation] = Field(default_factory=list)


class DhcpLease(BaseModel):
    """An address dnsmasq gave out (its leases file)."""

    ip: str
    mac: str
    hostname: str | None = None
    expires_at: datetime | None = None  # None: never (infinite lease)


class DhcpFacts(BaseModel):
    pools: list[DhcpPool] = Field(default_factory=list)
    leases: list[DhcpLease] = Field(default_factory=list)


class HostFacts(BaseModel):
    collected_at: datetime
    system: SystemInfo
    interfaces: list[Interface]
    routes: list[Route]
    dns: DNSInfo
    stack: NetworkStack
    # Discovery. Older snapshots have neither.
    neighbors: list[Neighbor] = Field(default_factory=list)
    lldp: list[LldpNeighbor] = Field(default_factory=list)
    # DHCP Lanvex serves on this server and its leases; None where not read (no NetworkManager, older reads).
    dhcp: DhcpFacts | None = None
    # VLAN interfaces Lanvex's helper lists as its own (/etc/lanvex/networks.json); NetworkManager
    # profiles named lanvex-* count too, see lanvex_owned().
    lanvex_vlans: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def lanvex_owned(self, iface: "Interface") -> bool:
        """Whether Lanvex created this VLAN interface (as far as the last read shows)."""
        return iface.kind == "vlan" and (
            (iface.manager_profile or "").startswith("lanvex-") or iface.name in self.lanvex_vlans
        )
