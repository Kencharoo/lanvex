"""What an infrastructure adapter can do. Core code asks for a capability, never for a vendor.

Every adapter declares the capabilities it actually implements. The UI and API only offer an
operation when the adapter for that target declares it. Write capabilities additionally need
LANVEX_AUTOMATION_ENABLED, a per-target opt-in, and whatever the target itself must provide.
"""

from enum import StrEnum


class Capability(StrEnum):
    READ_SYSTEM = "read.system"
    READ_INTERFACES = "read.interfaces"
    READ_ADDRESSES = "read.addresses"
    READ_ROUTES = "read.routes"
    READ_DNS = "read.dns"
    READ_NETWORK_STACK = "read.network_stack"
    CHANGE_INTERFACE_IPV4 = "change.interface_ipv4"  # one interface's IPv4 addresses, gateway and DNS


ADAPTERS: dict[str, frozenset[Capability]] = {}


def register(name: str, capabilities: set[Capability]) -> None:
    ADAPTERS[name] = frozenset(capabilities)


def capabilities(name: str) -> frozenset[Capability]:
    return ADAPTERS.get(name, frozenset())
