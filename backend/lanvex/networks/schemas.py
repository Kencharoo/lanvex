import ipaddress
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from lanvex.changes.schemas import HOSTNAME_RE, IFACE_RE, LEASE_RE, MAC_RE, NetworksRequest
from lanvex.schemas import ApiModel

NetworkStatus = Literal[
    "in_sync",  # on the server exactly as documented
    "missing",  # documented, not on the server yet: the change creates it
    "differs",  # on the server, but not as documented: the change brings it in line
    "removed",  # a Lanvex VLAN on the server that is no longer documented: the change removes it
    "protected",  # documented, but another tool manages interfaces with this name: left alone
    "foreign",  # documented, but the server has it from another tool: left alone
    "incomplete",  # documented without an IPv4 address: nothing to create
    "unknown",  # the server has not been read yet
]
DhcpStatus = Literal["in_sync", "missing", "differs", "removed", "none"]


class Pool(ApiModel):
    start: str
    end: str
    lease: str
    ip_range_id: int | None = None


class FixedAddress(ApiModel):
    ip: str
    mac: str
    name: str | None = None
    ip_id: int | None = None


class Lease(ApiModel):
    ip: str
    mac: str
    hostname: str | None
    expires_at: datetime | None
    fixed: bool  # Lanvex Core documents this MAC address for this IP


class OnServer(ApiModel):
    parent: str | None
    id: int | None
    addresses: list[str]


class ServerNetwork(ApiModel):
    name: str
    parent: str | None
    vlan_id: int | None
    vlan_name: str | None = None
    addresses: list[str]
    network: str | None  # the network of the first IPv4 address
    netbox_interface_id: int | None
    status: NetworkStatus
    note: str | None = None
    on_server: OnServer | None
    pool: Pool | None
    fixed: list[FixedAddress]
    dhcp: DhcpStatus | None  # None: the server's DHCP has not been read
    leases: list[Lease]


class NetworksView(ApiModel):
    linked: bool
    supported: bool | None  # None: not read yet
    stack_label: str | None
    read_at: datetime | None
    networks: list[ServerNetwork]
    in_sync: bool
    change: NetworksRequest | None  # what "Prepare change" asks the server for; None: nothing to change
    missing_fields: list[str]  # custom fields Lanvex needs that this NetBox does not have
    other_leases: list[Lease]  # leases outside every Lanvex network


class NewNetwork(ApiModel):
    name: str
    parent: str
    vlan_id: int = Field(ge=1, le=4094)
    vlan_name: str | None = Field(default=None, max_length=64)
    address: str = Field(description="The server's address in the new network, e.g. 192.168.50.1/24.")
    pool_start: str | None = None
    pool_end: str | None = None
    lease: str = "12h"

    @field_validator("name", "parent")
    @classmethod
    def _iface(cls, v: str) -> str:
        v = v.strip()
        if not IFACE_RE.match(v) or v == "lo":
            raise ValueError("Letters, digits and . _ - @ :, at most 15 characters.")
        return v

    @field_validator("address")
    @classmethod
    def _address(cls, v: str) -> str:
        try:
            a = ipaddress.ip_interface(v.strip())
        except ValueError:
            raise ValueError("An IPv4 address with prefix length, e.g. 192.168.50.1/24.") from None
        if not isinstance(a, ipaddress.IPv4Interface) or "/" not in v or not 8 <= a.network.prefixlen <= 30:
            raise ValueError("An IPv4 address with prefix length between /8 and /30, e.g. 192.168.50.1/24.")
        if a.ip in (a.network.network_address, a.network.broadcast_address):
            raise ValueError(f"{a.ip} is the network or broadcast address of {a.network}.")
        return str(a)

    @field_validator("lease")
    @classmethod
    def _lease(cls, v: str) -> str:
        if not LEASE_RE.match(v):
            raise ValueError("A lease time like 12h, 30m, 2d or infinite.")
        return v

    @model_validator(mode="after")
    def _pool(self) -> "NewNetwork":
        if (self.pool_start is None) != (self.pool_end is None):
            raise ValueError("Give the DHCP pool's first and last address, or neither.")
        if self.pool_start and self.pool_end:
            check_pool(self.address, self.pool_start, self.pool_end)
        return self


def check_pool(address: str, start_text: str, end_text: str) -> tuple[str, str]:
    """A pool inside the network of `address`, not containing it. Raises ValueError with the reason."""
    iface = ipaddress.ip_interface(address)
    net = iface.network
    try:
        start, end = ipaddress.ip_address(start_text.strip()), ipaddress.ip_address(end_text.strip())
    except ValueError:
        raise ValueError("The pool's first and last address must be IPv4 addresses.") from None
    if start not in net or end not in net:
        raise ValueError(f"The pool must be inside {net}.")
    if start > end:
        raise ValueError("The pool's first address comes after its last.")
    if start in (net.network_address, net.broadcast_address) or end in (
        net.network_address,
        net.broadcast_address,
    ):
        raise ValueError("The pool cannot include the network or broadcast address.")
    if start <= iface.ip <= end:
        raise ValueError(
            f"The server's own address {iface.ip} is inside the pool and could be given to a device."
        )
    return str(start), str(end)


class FixedIn(ApiModel):
    ip: str
    mac: str
    name: str | None = Field(default=None, max_length=63)

    @field_validator("mac")
    @classmethod
    def _mac(cls, v: str) -> str:
        v = v.strip().lower().replace("-", ":")
        if not MAC_RE.match(v):
            raise ValueError("A MAC address like aa:bb:cc:dd:ee:ff.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and not HOSTNAME_RE.match(v):
            raise ValueError("Letters, digits and hyphens, like drucker-1.")
        return v

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError("An IPv4 address.") from None
        if not isinstance(ip, ipaddress.IPv4Address):
            raise ValueError("An IPv4 address.")
        return str(ip)
