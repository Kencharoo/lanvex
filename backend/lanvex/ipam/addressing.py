"""Pure address arithmetic. No I/O, so it is exhaustively unit-tested.

Semantics deliberately match NetBox 4.7 (ipam/models/ip.py) so numbers shown in Lanvex agree
with what NetBox itself would report.
"""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPInterface = ipaddress.IPv4Interface | ipaddress.IPv6Interface


class AddressError(ValueError):
    def __init__(self, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion


def parse_network(value: str) -> IPNetwork:
    """Parse a CIDR network strictly. Host bits must be zero; a prefix length is required."""
    text = (value or "").strip()
    if not text:
        raise AddressError("Enter a prefix in CIDR notation, e.g. 10.20.0.0/24.")
    if "/" not in text:
        raise AddressError("Include the prefix length, e.g. 10.20.0.0/24 or 2001:db8::/48.")
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError as e:
        try:
            corrected = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise AddressError(f"'{text}' is not a valid IPv4 or IPv6 prefix.") from e
        raise AddressError(
            f"{text} has host bits set. The network address for this prefix is {corrected}.",
            suggestion=str(corrected),
        ) from e


def parse_interface(value: str) -> IPInterface:
    """Parse an address with mask (NetBox stores IP addresses as address/len)."""
    text = (value or "").strip()
    if "/" not in text:
        raise AddressError("Include the mask length, e.g. 10.20.0.1/24.")
    try:
        return ipaddress.ip_interface(text)
    except ValueError as e:
        raise AddressError(f"'{text}' is not a valid IPv4 or IPv6 address with mask.") from e


def usable_bounds(net: IPNetwork, is_pool: bool = False) -> tuple[int, int]:
    """First and last usable host addresses as integers (NetBox Prefix.usable_ip_bounds)."""
    first, last = int(net.network_address), int(net.broadcast_address)
    if is_pool or (net.version == 4 and net.prefixlen >= 31) or (net.version == 6 and net.prefixlen >= 127):
        return first, last
    if net.version == 4:
        return first + 1, last - 1
    return first + 1, last  # IPv6 excludes only the subnet-router anycast address


def utilization_denominator(net: IPNetwork, is_pool: bool = False) -> int:
    """NetBox Prefix._get_utilization_denominator."""
    if net.version == 4 and net.prefixlen < 31 and not is_pool:
        return net.num_addresses - 2
    return net.num_addresses


def covered_size(children: Iterable[IPNetwork]) -> int:
    """Number of addresses covered by a set of networks, without double counting overlaps."""
    by_family: dict[int, list[IPNetwork]] = {4: [], 6: []}
    for c in children:
        by_family[c.version].append(c)
    total = 0
    for nets in by_family.values():
        if nets:
            total += sum(n.num_addresses for n in ipaddress.collapse_addresses(nets))  # type: ignore[arg-type]
    return total


@dataclass(slots=True)
class Utilization:
    percent: float
    used: int
    total: int
    basis: str  # "child_prefixes" | "ip_addresses" | "marked_utilized"


def container_utilization(net: IPNetwork, children: Iterable[IPNetwork]) -> Utilization:
    inside = [c for c in children if c.version == net.version and c != net and c.subnet_of(net)]  # type: ignore[arg-type]
    used = covered_size(inside)
    return Utilization(
        percent=min(used / net.num_addresses * 100, 100.0),
        used=used,
        total=net.num_addresses,
        basis="child_prefixes",
    )


def address_utilization(
    net: IPNetwork, is_pool: bool, ip_count: int, utilized_range_size: int = 0
) -> Utilization:
    total = utilization_denominator(net, is_pool)
    used = min(ip_count + utilized_range_size, total)
    percent = 100.0 if total <= 0 else min(used / total * 100, 100.0)
    return Utilization(percent=percent, used=used, total=total, basis="ip_addresses")


def marked_utilized(net: IPNetwork, is_pool: bool) -> Utilization:
    total = utilization_denominator(net, is_pool)
    return Utilization(percent=100.0, used=total, total=total, basis="marked_utilized")


def classify_overlap(candidate: IPNetwork, existing: IPNetwork) -> str | None:
    """Relationship of an existing prefix to a candidate: 'duplicate', 'parent', 'child', or None."""
    if candidate.version != existing.version:
        return None
    if candidate == existing:
        return "duplicate"
    if candidate.subnet_of(existing):  # type: ignore[arg-type]
        return "parent"
    if existing.subnet_of(candidate):  # type: ignore[arg-type]
        return "child"
    return None


def suggest_gateway(net: IPNetwork) -> str | None:
    """Conventional gateway suggestion: the first usable address, with the prefix's mask."""
    if (net.version == 4 and net.prefixlen >= 31) or (net.version == 6 and net.prefixlen >= 127):
        return None
    first, _ = usable_bounds(net)
    return f"{ipaddress.ip_address(first)}/{net.prefixlen}"


def address_in_prefix(address: IPInterface, net: IPNetwork, is_pool: bool = False) -> str | None:
    """Return an error message if the address is not a usable host in the prefix, else None."""
    if address.version != net.version:
        return f"{address.ip} is IPv{address.version}, but the prefix is IPv{net.version}."
    if address.ip not in net:
        return f"{address.ip} is outside {net}."
    if address.network.prefixlen != net.prefixlen:
        return f"Use the prefix's mask length (/{net.prefixlen}) for addresses in {net}."
    first, last = usable_bounds(net, is_pool)
    value = int(address.ip)
    if value < first or value > last:
        if net.version == 4 and value == int(net.broadcast_address):
            return f"{address.ip} is the broadcast address of {net}."
        return f"{address.ip} is the network address of {net}."
    return None
