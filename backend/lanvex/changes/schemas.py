import ipaddress
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Discriminator, Field, Tag, field_validator, model_validator

from lanvex.changes.models import ChangeStatus
from lanvex.schemas import ApiModel

IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?$"
)

HelperStateName = Literal["unknown", "ok", "outdated", "missing", "no_sudo", "no_python", "timeout", "error"]


def check_addresses(v: list[str]) -> list[str]:
    """IPv4 addresses with prefix length an interface can have."""
    out: list[str] = []
    for text in v:
        text = text.strip()
        try:
            a = ipaddress.ip_interface(text)
        except ValueError:
            raise ValueError(f"{text} is not an address with prefix length, e.g. 192.0.2.10/24.") from None
        if not isinstance(a, ipaddress.IPv4Interface):
            raise ValueError("Only IPv4 addresses can be changed so far.")
        if "/" not in text or a.network.prefixlen == 0:
            raise ValueError(f"{text} needs a prefix length, e.g. /24.")
        if (
            a.ip.is_loopback
            or a.ip.is_multicast
            or a.ip.is_link_local
            or a.ip.is_unspecified
            or a.ip.is_reserved
        ):
            raise ValueError(f"{a.ip} cannot be assigned to an interface.")
        if a.network.prefixlen < 31 and a.ip in (a.network.network_address, a.network.broadcast_address):
            raise ValueError(f"{a.ip} is the network or broadcast address of {a.network}.")
        out.append(str(a))
    if len({a.split("/")[0] for a in out}) != len(out):
        raise ValueError("The same address is listed twice.")
    return out


class ChangeRequest(ApiModel):
    """The IPv4 settings an interface should have. Everything else on the interface stays as it is."""

    kind: Literal["interface_ipv4"] = "interface_ipv4"
    interface: str
    method: Literal["static", "dhcp"]
    addresses: list[str] = Field(default_factory=list, max_length=32)
    gateway: str | None = None
    # None for both keeps the interface's DNS settings exactly as they are.
    dns_servers: list[str] | None = Field(default=None, max_length=8)
    dns_search: list[str] | None = Field(default=None, max_length=16)
    verify_dns_name: str | None = Field(
        default=None, description="A name that must resolve after the change."
    )

    @field_validator("interface")
    @classmethod
    def _iface(cls, v: str) -> str:
        if not IFACE_RE.match(v) or v == "lo":
            raise ValueError("Not an interface Lanvex can change.")
        return v

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        return check_addresses(v)

    @field_validator("gateway")
    @classmethod
    def _gateway(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"{v} is not an IPv4 address.") from None
        if not isinstance(ip, ipaddress.IPv4Address) or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"{v} is not a usable IPv4 gateway.")
        return str(ip)

    @field_validator("dns_servers")
    @classmethod
    def _dns(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = []
        for text in v:
            try:
                ip = ipaddress.ip_address(text.strip())
            except ValueError:
                raise ValueError(f"{text} is not an IPv4 address.") from None
            if not isinstance(ip, ipaddress.IPv4Address):
                raise ValueError("Only IPv4 DNS servers can be set so far.")
            out.append(str(ip))
        return out

    @field_validator("dns_search")
    @classmethod
    def _search(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for d in v:
            if not DOMAIN_RE.match(d):
                raise ValueError(f"{d} is not a valid domain name.")
        return [d.rstrip(".") for d in v]

    @field_validator("verify_dns_name")
    @classmethod
    def _verify_name(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not DOMAIN_RE.match(v.strip()):
            raise ValueError(f"{v} is not a valid host name.")
        return v.strip().rstrip(".")

    @model_validator(mode="after")
    def _consistent(self) -> "ChangeRequest":
        if self.method == "static":
            if not self.addresses:
                raise ValueError("A static configuration needs at least one address.")
            if self.gateway:
                gw = ipaddress.ip_address(self.gateway)
                nets = [ipaddress.ip_interface(a) for a in self.addresses]
                if not any(gw in n.network for n in nets):
                    raise ValueError(f"The gateway {gw} is not inside any of the interface's networks.")
                if any(gw == n.ip for n in nets):
                    raise ValueError("The gateway cannot be one of the interface's own addresses.")
        elif self.addresses or self.gateway:
            raise ValueError("With DHCP, addresses and the gateway come from the DHCP server.")
        if (self.dns_servers is None) != (self.dns_search is None):
            # Changing DNS means stating both; send [] for "none".
            self.dns_servers, self.dns_search = self.dns_servers or [], self.dns_search or []
        return self

    @property
    def changes_dns(self) -> bool:
        return self.dns_servers is not None

    def helper_payload(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "ipv4": {"method": self.method, "addresses": self.addresses, "gateway": self.gateway},
            "dns": {"servers": self.dns_servers, "search": self.dns_search} if self.changes_dns else None,
        }


MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
LEASE_RE = re.compile(r"^([1-9][0-9]{0,4}[mhd]|infinite)$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class VlanSpec(ApiModel):
    """A VLAN interface Lanvex manages on the server."""

    name: str
    parent: str
    id: int = Field(ge=1, le=4094)
    addresses: list[str] = Field(min_length=1, max_length=8)

    @field_validator("name", "parent")
    @classmethod
    def _iface(cls, v: str) -> str:
        if not IFACE_RE.match(v) or v == "lo":
            raise ValueError("Not an interface name.")
        return v

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        return check_addresses(v)


class Reservation(ApiModel):
    mac: str
    ip: str
    name: str = ""

    @field_validator("mac")
    @classmethod
    def _mac(cls, v: str) -> str:
        v = v.strip().lower().replace("-", ":")
        if not MAC_RE.match(v):
            raise ValueError(f"{v} is not a MAC address like aa:bb:cc:dd:ee:ff.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if v and not HOSTNAME_RE.match(v):
            raise ValueError(f"{v} is not a host name (letters, digits, hyphens).")
        return v


class DhcpPool(ApiModel):
    interface: str
    start: str
    end: str
    lease: str = "12h"
    router: str | None = None  # default: the server's address on the interface
    dns: list[str] | None = Field(default=None, max_length=4)  # default: the server's address
    reservations: list[Reservation] = Field(default_factory=list, max_length=1000)

    @field_validator("lease")
    @classmethod
    def _lease(cls, v: str) -> str:
        if not LEASE_RE.match(v):
            raise ValueError("A lease time like 12h, 30m, 2d or infinite.")
        return v


class DhcpSpec(ApiModel):
    pools: list[DhcpPool] = Field(default_factory=list, max_length=64)


PORT_RE = re.compile(r"^([0-9]{1,5})(?:-([0-9]{1,5}))?/(tcp|udp)$")


def check_port(v: str) -> str:
    """'22/tcp' or '8000-8100/udp', normalised."""
    m = PORT_RE.match(v.strip().lower())
    if not m:
        raise ValueError(f"{v} is not a port like 22/tcp or 8000-8100/udp.")
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if not 1 <= first <= last <= 65535:
        raise ValueError(f"{v}: ports are between 1 and 65535.")
    return f"{first}{f'-{last}' if last != first else ''}/{m.group(3)}"


def _port_key(p: str) -> tuple[int, str, str]:
    number, proto = p.split("/")
    return (int(number.split("-")[0]), number, proto)


def check_ports(v: list[str]) -> list[str]:
    return sorted({check_port(x) for x in v}, key=_port_key)


class Destination(ApiModel):
    """Somewhere devices on a Lanvex network may reach through the server: a network or one address,
    on every port or only on these."""

    network: str
    ports: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("network")
    @classmethod
    def _network(cls, v: str) -> str:
        try:
            net = ipaddress.IPv4Network(v.strip(), strict=False)
        except ValueError:
            raise ValueError(f"{v} is not an IPv4 network or address.") from None
        if net.prefixlen == 0:
            raise ValueError("Allow the Internet with its own switch, not 0.0.0.0/0.")
        return str(net)

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[str]) -> list[str]:
        return check_ports(v)


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)


def check_domains(v: list[str]) -> list[str]:
    out = set()
    for d in v:
        name = d.strip().lower().lstrip("*").strip(".")
        if not DOMAIN_RE.match(name):
            raise ValueError(f"{d} is not a domain like example.com.")
        out.add(name)
    return sorted(out)


class DnsFilter(ApiModel):
    """Names devices on the network cannot look up: each domain with every name below it, except the
    exceptions."""

    block: list[str] = Field(default_factory=list, max_length=5000)
    allow: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("block", "allow")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return check_domains(v)


class AccessSettings(ApiModel):
    """What devices on one Lanvex network may reach. The default: DHCP and DNS on the server, nothing else."""

    internet: bool = False
    destinations: list[Destination] = Field(default_factory=list, max_length=64)
    services: list[str] = Field(default_factory=list, max_length=32)  # ports on the server itself
    dns_filter: DnsFilter | None = None  # None: no DNS filter

    @field_validator("services")
    @classmethod
    def _services(cls, v: list[str]) -> list[str]:
        return check_ports(v)

    @field_validator("destinations")
    @classmethod
    def _destinations(cls, v: list[Destination]) -> list[Destination]:
        seen: dict[str, Destination] = {}
        for d in v:
            if d.network in seen:  # the same network twice: one entry with both sets of ports
                old = seen[d.network]
                ports = [] if not old.ports or not d.ports else check_ports(old.ports + d.ports)
                seen[d.network] = Destination(network=d.network, ports=ports)
            else:
                seen[d.network] = d
        return sorted(seen.values(), key=lambda d: (ipaddress.ip_network(d.network), d.ports))

    @property
    def is_default(self) -> bool:
        return not self.internet and not self.destinations and not self.services and self.dns_filter is None


class AccessRule(AccessSettings):
    interface: str


class AccessSpec(ApiModel):
    rules: list[AccessRule] = Field(default_factory=list, max_length=64)


class NetworksRequest(ApiModel):
    """Every VLAN interface Lanvex manages on the server, the DHCP pools on them, and what devices on
    them may reach. A Lanvex VLAN interface missing here is removed. dhcp=None leaves DHCP as it is;
    access=None leaves the access rules as last applied."""

    kind: Literal["networks"]
    vlans: list[VlanSpec] = Field(default_factory=list, max_length=64)
    dhcp: DhcpSpec | None = None
    access: AccessSpec | None = None
    # Replace another tool's networks (all of them) with Lanvex's in this change: its files go, backed up.
    takeover: Literal["netzverwaltung"] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "NetworksRequest":
        names = [v.name for v in self.vlans]
        if len(set(names)) != len(names):
            raise ValueError("A VLAN interface is listed twice.")
        if len({(v.parent, v.id) for v in self.vlans}) != len(self.vlans):
            raise ValueError("The same VLAN on the same parent is listed twice.")
        by_name = {v.name: v for v in self.vlans}
        for p in self.dhcp.pools if self.dhcp else []:
            vlan = by_name.get(p.interface)
            if vlan is None:
                raise ValueError(
                    f"DHCP on {p.interface}: Lanvex serves DHCP only on VLAN interfaces it manages."
                )
            nets = [ipaddress.ip_interface(a).network for a in vlan.addresses]
            for text in (p.start, p.end, *(r.ip for r in p.reservations)):
                try:
                    ip = ipaddress.ip_address(text)
                except ValueError:
                    raise ValueError(f"{text} is not an IPv4 address.") from None
                if not any(ip in n for n in nets):
                    raise ValueError(f"{ip} is not in the network of {p.interface}.")
        seen: set[str] = set()
        for r in self.access.rules if self.access else []:
            vlan = by_name.get(r.interface)
            if vlan is None:
                raise ValueError(
                    f"Access rules for {r.interface}: Lanvex sets them only on its own VLAN interfaces."
                )
            if r.interface in seen:
                raise ValueError(f"{r.interface} has more than one access rule.")
            seen.add(r.interface)
            own = ipaddress.ip_interface(vlan.addresses[0]).network
            for d in r.destinations:
                if ipaddress.ip_network(d.network).overlaps(own):
                    raise ValueError(f"{r.interface}: {d.network} is that network itself.")
            if (
                r.dns_filter is not None
                and self.dhcp is not None
                and r.interface not in {p.interface for p in self.dhcp.pools}
            ):
                raise ValueError(
                    f"A DNS filter on {r.interface} needs a DHCP pool there: devices learn the filter as their DNS server from it."
                )
        return self

    @property
    def interface(self) -> str:
        return "networks"

    @property
    def verify_dns_name(self) -> None:
        return None

    def helper_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _kind(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("kind") or "interface_ipv4"
    return getattr(v, "kind", "interface_ipv4")


AnyChangeRequest = Annotated[
    Annotated[ChangeRequest, Tag("interface_ipv4")] | Annotated[NetworksRequest, Tag("networks")],
    Discriminator(_kind),
]


def parse_request(kind: str, data: dict[str, Any]) -> "ChangeRequest | NetworksRequest":
    """A stored request; requests from before kinds existed are interface changes."""
    if kind == "networks":
        return NetworksRequest.model_validate(data)
    return ChangeRequest.model_validate(data)


class EnabledUpdate(ApiModel):
    enabled: bool


class ApproveRequest(ApiModel):
    confirm_name: str = Field(min_length=1, max_length=100)


class BrowserDecision(ApiModel):
    decision: Literal["keep", "undo"]
    # Keep even though this browser reaches Lanvex through an address that is not the server's own.
    acknowledge_indirect: bool = False


class BrowserState(ApiModel):
    waiting_since: datetime | None = None
    decision: Literal["keep", "undo"] | None = None
    by: str | None = None
    at: datetime | None = None
    host: str | None = None
    on_server: bool | None = None


class BrowserAddress(ApiModel):
    """How this browser reaches Lanvex, and whether that goes through the changed server's network."""

    host: str
    on_server: bool
    # The server's interface with that address, and whether it is a VPN (Tailscale, WireGuard, a tunnel):
    # reaching Lanvex through a VPN shows only that the VPN works, not the server's LAN and VLANs.
    interface: str | None = None
    vpn: bool = False


class HelperStatus(ApiModel):
    state: HelperStateName
    version: int | None
    expected_version: int
    message: str | None
    checked_at: datetime | None


class ChangeOut(ApiModel):
    id: uuid.UUID
    number: int
    server_id: uuid.UUID
    server_name: str
    interface: str
    summary: str
    status: ChangeStatus
    requested_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    outcome: str | None
    # Applied and checked; kept only if an administrator confirms it in the browser in time.
    awaiting_browser: bool = False


class ConnectionPath(ApiModel):
    """Which of the server's interfaces carry Lanvex's connection, as of its last read."""

    address: str  # where Lanvex connects
    interfaces: list[str]  # the server's interfaces with that address; empty = not one of its own
    verify_address: str | None
    verify_interfaces: list[str]


class Readiness(ApiModel):
    automation_enabled: bool
    changes_enabled: bool
    stack_key: str | None
    stack_label: str | None
    stack_supported: bool | None
    # Whether interface settings can be changed here (netplan, NetworkManager), not only networks.
    interface_changes: bool | None = None
    helper: HelperStatus
    helper_path: str
    helper_sha256: str
    ready: bool
    blockers: list[str]
    rollback_seconds: int
    second_admin_required: bool
    open_change: ChangeOut | None
    connection: ConnectionPath | None = None
    protected_interfaces: list[str] = []
    confirm_in_browser: bool = False


class HelperInstall(ApiModel):
    path: str
    sha256: str
    commands: list[str]


class PreviewFile(ApiModel):
    path: str
    label: str
    diff: str
    before: str
    after: str


class Preview(ApiModel):
    backend: str
    files: list[PreviewFile]
    commands: list[str]
    warnings: list[str]


class Check(ApiModel):
    id: str
    label: str
    ok: bool | None
    detail: str


class LogEntry(ApiModel):
    at: datetime
    level: Literal["info", "warning", "error"]
    message: str


class ChangeDetail(ChangeOut):
    kind: str = "interface_ipv4"
    request: AnyChangeRequest
    before: dict[str, Any] | None
    preview: Preview | None
    rollback_seconds: int
    deadline_at: datetime | None
    checks: list[Check]
    log: list[LogEntry]
    can_approve: bool
    approve_blocker: str | None
    can_cancel: bool
    can_recheck: bool
    confirm_in_browser: bool = False
    browser: BrowserState | None = None
    can_decide: bool = False
    your_address: BrowserAddress | None = None
