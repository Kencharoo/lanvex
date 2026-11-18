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


def check_ipv6_addresses(v: list[str]) -> list[str]:
    out: list[str] = []
    for text in v:
        text = text.strip()
        try:
            a = ipaddress.ip_interface(text)
        except ValueError:
            raise ValueError(f"{text} is not an address with prefix length, e.g. 2001:db8::10/64.") from None
        if not isinstance(a, ipaddress.IPv6Interface):
            raise ValueError(f"{text} is not an IPv6 address.")
        if "/" not in text or a.network.prefixlen == 0:
            raise ValueError(f"{text} needs a prefix length, e.g. /64.")
        if a.ip.is_loopback or a.ip.is_multicast or a.ip.is_link_local or a.ip.is_unspecified:
            raise ValueError(f"{a.ip} cannot be assigned to an interface.")
        out.append(str(a))
    if len({a.split("/")[0] for a in out}) != len(out):
        raise ValueError("The same address is listed twice.")
    return out


def check_gateway4(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    try:
        ip = ipaddress.ip_address(v.strip())
    except ValueError:
        raise ValueError(f"{v} is not an IPv4 address.") from None
    if not isinstance(ip, ipaddress.IPv4Address) or ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"{v} is not a usable IPv4 gateway.")
    return str(ip)


class Ipv6Settings(ApiModel):
    """auto: router advertisements and DHCPv6; static: these addresses; link-local: no other address."""

    method: Literal["auto", "static", "link-local"]
    addresses: list[str] = Field(default_factory=list, max_length=32)
    gateway: str | None = None

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        return check_ipv6_addresses(v)

    @field_validator("gateway")
    @classmethod
    def _gateway(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"{v} is not an IPv6 address.") from None
        if not isinstance(ip, ipaddress.IPv6Address) or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"{v} is not a usable IPv6 gateway.")
        return str(ip)

    @model_validator(mode="after")
    def _consistent(self) -> "Ipv6Settings":
        if self.method == "static":
            if not self.addresses:
                raise ValueError("A static IPv6 configuration needs at least one address.")
            if self.gateway:
                gw = ipaddress.ip_address(self.gateway)
                nets = [ipaddress.ip_interface(a) for a in self.addresses]
                if not gw.is_link_local and not any(gw in n.network for n in nets):
                    raise ValueError(
                        f"The IPv6 gateway {gw} is neither link-local nor inside one of the interface's networks."
                    )
                if any(gw == n.ip for n in nets):
                    raise ValueError("The IPv6 gateway cannot be one of the interface's own addresses.")
        elif self.addresses or self.gateway:
            raise ValueError("IPv6 addresses and a gateway need the static method.")
        return self


class StaticRoute(ApiModel):
    """A route to a network through a next hop on this interface's link."""

    to: str
    via: str
    metric: int | None = Field(default=None, ge=0, le=4294967295)

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        try:
            net = ipaddress.ip_network(v.strip(), strict=False)
        except ValueError:
            raise ValueError(f"{v} is not a network, e.g. 10.20.0.0/16.") from None
        if str(net) != v.strip() and "/" in v and net.network_address != ipaddress.ip_interface(v.strip()).ip:
            raise ValueError(f"{v} is not a network address; did you mean {net}?")
        if net.prefixlen == 0:
            raise ValueError("The default route is the gateway, not a static route.")
        if net.is_multicast or net.is_loopback or (net.version == 6 and net.is_link_local):
            raise ValueError(f"{net} cannot be routed.")
        return str(net)

    @field_validator("via")
    @classmethod
    def _via(cls, v: str) -> str:
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"{v} is not an IP address.") from None
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
            raise ValueError(f"{v} cannot be a next hop.")
        return str(ip)

    @model_validator(mode="after")
    def _family(self) -> "StaticRoute":
        if ipaddress.ip_network(self.to).version != ipaddress.ip_address(self.via).version:
            raise ValueError(f"{self.to} and its next hop {self.via} are not the same family.")
        return self


class ChangeRequest(ApiModel):
    """The IP settings an interface should have. Everything else on the interface stays as it is."""

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
    # None keeps the interface's IPv6 settings, or its static routes, exactly as they are.
    ipv6: Ipv6Settings | None = None
    routes: list[StaticRoute] | None = Field(default=None, max_length=64)

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
        return check_gateway4(v)

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
        if self.routes is not None:
            self._check_routes(self.routes)
        return self

    def _check_routes(self, routes: list["StaticRoute"]) -> None:
        """A next hop must be on the link: inside a static network of the interface (or link-local for
        IPv6). With DHCP or router advertisements the networks are not known in advance."""
        if len({r.to for r in routes}) != len(routes):
            raise ValueError("A network is listed twice in the routes.")
        on_link: dict[int, list[Any] | None] = {
            4: [ipaddress.ip_interface(a).network for a in self.addresses]
            if self.method == "static"
            else None,
            6: None,
        }
        if self.ipv6 is not None and self.ipv6.method == "static":
            on_link[6] = [ipaddress.ip_interface(a).network for a in self.ipv6.addresses]
        elif self.ipv6 is not None and self.ipv6.method == "link-local":
            on_link[6] = []
        for r in routes:
            via = ipaddress.ip_address(r.via)
            nets = on_link[via.version]
            if nets is None or (via.version == 6 and via.is_link_local):
                continue
            if not any(via in n for n in nets):
                raise ValueError(f"The next hop {via} for {r.to} is not on any of the interface's networks.")

    @property
    def changes_dns(self) -> bool:
        return self.dns_servers is not None

    def helper_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "interface": self.interface,
            "ipv4": {"method": self.method, "addresses": self.addresses, "gateway": self.gateway},
            "dns": {"servers": self.dns_servers, "search": self.dns_search} if self.changes_dns else None,
        }
        # Sent only when set, so a helper older than 6 still takes a change that sets neither.
        if self.ipv6 is not None:
            out["ipv6"] = self.ipv6.model_dump()
        if self.routes is not None:
            out["routes"] = [r.model_dump() for r in self.routes]
        return out


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


BOND_MODES = (
    "active-backup",
    "802.3ad",
    "balance-rr",
    "balance-xor",
    "balance-tlb",
    "balance-alb",
    "broadcast",
)


class LinkRequest(ApiModel):
    """Create one bridge or bond from interfaces without addresses, or remove one Lanvex created."""

    kind: Literal["link"] = "link"
    action: Literal["create", "remove"] = "create"
    name: str
    type: Literal["bridge", "bond"] | None = None
    members: list[str] = Field(default_factory=list, max_length=8)
    mode: Literal[BOND_MODES] | None = None  # type: ignore[valid-type]
    stp: bool = False
    method: Literal["none", "static", "dhcp"] = "none"
    addresses: list[str] = Field(default_factory=list, max_length=32)
    gateway: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not IFACE_RE.match(v) or v == "lo" or v.startswith(("lanvex", "lxlink-")) or "." in v or ":" in v:
            raise ValueError("Not a name Lanvex can give a bridge or bond.")
        return v

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[str]) -> list[str]:
        for m in v:
            if not IFACE_RE.match(m) or m == "lo":
                raise ValueError(f"{m} is not an interface name.")
        if len(set(v)) != len(v):
            raise ValueError("An interface is listed twice.")
        return v

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        return check_addresses(v)

    @field_validator("gateway")
    @classmethod
    def _gateway(cls, v: str | None) -> str | None:
        return check_gateway4(v)

    @model_validator(mode="after")
    def _consistent(self) -> "LinkRequest":
        if self.action == "remove":
            return self
        if self.type is None:
            raise ValueError("Choose bridge or bond.")
        if not self.members:
            raise ValueError("Choose at least one member interface.")
        if self.name in self.members:
            raise ValueError("A bridge or bond cannot be its own member.")
        if self.type == "bond":
            self.mode = self.mode or "active-backup"
        else:
            self.mode = None
        if self.method == "static":
            if not self.addresses:
                raise ValueError("A static configuration needs at least one address.")
            if self.gateway:
                gw = ipaddress.ip_address(self.gateway)
                if not any(gw in ipaddress.ip_interface(a).network for a in self.addresses):
                    raise ValueError(f"The gateway {gw} is not inside any of the networks.")
        elif self.addresses or self.gateway:
            raise ValueError("Addresses and a gateway need the static method.")
        return self

    @property
    def interface(self) -> str:
        return self.name

    @property
    def verify_dns_name(self) -> None:
        return None

    def helper_payload(self) -> dict[str, Any]:
        if self.action == "remove":
            return {"kind": "link", "action": "remove", "name": self.name}
        return {"kind": "link", **self.model_dump(exclude={"kind"})}


def _kind(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("kind") or "interface_ipv4"
    return getattr(v, "kind", "interface_ipv4")


AnyChangeRequest = Annotated[
    Annotated[ChangeRequest, Tag("interface_ipv4")]
    | Annotated[NetworksRequest, Tag("networks")]
    | Annotated[LinkRequest, Tag("link")],
    Discriminator(_kind),
]


def parse_request(kind: str, data: dict[str, Any]) -> "ChangeRequest | NetworksRequest | LinkRequest":
    """A stored request; requests from before kinds existed are interface changes."""
    if kind == "networks":
        return NetworksRequest.model_validate(data)
    if kind == "link":
        return LinkRequest.model_validate(data)
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
    # Bridges and bonds Lanvex created here (by its changes that succeeded) and that are still there.
    links: list[dict[str, Any]] = []


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
