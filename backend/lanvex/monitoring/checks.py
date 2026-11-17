"""What monitoring finds on a server, from its latest read and the documentation. Pure functions."""

import fnmatch
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime

from lanvex.integrations.linux.facts import HostFacts
from lanvex.monitoring.models import Severity
from lanvex.monitoring.schemas import MonitoringSettings
from lanvex.networks.schemas import NetworksView
from lanvex.servers.models import ConnectionStatus
from lanvex.servers.schemas import Comparison

# Alerts about events rather than conditions: they end when acknowledged (or, for unknown devices,
# when the device is documented), and a device is reported once per server.
EVENT_KINDS = {"unknown_device", "change_failed"}

READ_FAILED = {
    ConnectionStatus.AUTH_FAILED: "sign-in rejected",
    ConnectionStatus.HOST_KEY_MISMATCH: "host key changed",
    ConnectionStatus.NOT_ALLOWED: "address not allowed",
    ConnectionStatus.REFUSED: "connections refused",
    ConnectionStatus.UNREACHABLE: "not reachable over SSH",
    ConnectionStatus.ERROR: "read failed",
}

NETWORK_DRIFT = {
    "missing": "documented, not on the server",
    "differs": "differs from the documentation",
    "removed": "on the server, no longer documented",
}

ADDRESS_DRIFT = {
    "mask_differs": "prefix length differs",
    "interface_differs": "on another interface than documented",
    "only_on_server": "not documented",
    "only_in_netbox": "documented, not on the server",
}


@dataclass
class Finding:
    kind: str
    key: str
    severity: Severity
    title: str
    detail: list[str] = field(default_factory=list)


def read_failed(server: str, status: ConnectionStatus, message: str | None) -> list[Finding]:
    if status == ConnectionStatus.OK:
        return []
    return [
        Finding(
            "read_failed",
            "",
            Severity.WARNING,
            f"{server}: {READ_FAILED.get(status, 'read failed')}",
            [message] if message else [],
        )
    ]


def unreachable(server: str, failures: int, after: int, error: str | None) -> list[Finding]:
    if failures < after:
        return []
    return [
        Finding(
            "unreachable",
            "",
            Severity.CRITICAL,
            f"{server} is not reachable",
            [f"SSH port not answering ({failures} checks in a row)."] + ([error] if error else []),
        )
    ]


def _ignored(iface: str | None, patterns: list[str]) -> bool:
    return bool(iface) and any(fnmatch.fnmatchcase(iface or "", p) for p in patterns)


def drift(
    server: str, comparison: Comparison | None, view: NetworksView | None, cfg: MonitoringSettings
) -> list[Finding]:
    lines: list[str] = []
    if comparison is not None and comparison.linked:
        for r in comparison.rows:
            if r.status == "match":
                continue
            iface = r.server_interface or r.netbox_interface
            if _ignored(iface, cfg.drift_ignore_interfaces):
                continue
            where = f" ({iface})" if iface else ""
            lines.append(f"{r.address}{where}: {ADDRESS_DRIFT.get(r.status, r.status)}")
    if view is not None and view.linked and view.supported:
        for n in view.networks:
            if n.status in NETWORK_DRIFT:
                lines.append(f"Network {n.name}: {NETWORK_DRIFT[n.status]}")
            elif n.status == "in_sync":
                if n.dhcp in ("missing", "differs", "removed"):
                    lines.append(f"Network {n.name}: DHCP {NETWORK_DRIFT[n.dhcp]}")
                if n.access_status == "differs":
                    lines.append(f"Network {n.name}: access rules differ from the documentation")
    if not lines:
        return []
    lines = sorted(dict.fromkeys(lines))
    count = f"{len(lines)} difference" + ("s" if len(lines) != 1 else "")
    return [
        Finding("drift", "", Severity.WARNING, f"{server} differs from its documentation ({count})", lines)
    ]


def pool_usage(server: str, facts: HostFacts, threshold: int, now: datetime) -> list[Finding]:
    out: list[Finding] = []
    if facts.dhcp is None:
        return out
    active = [lease for lease in facts.dhcp.leases if lease.expires_at is None or lease.expires_at > now]
    for p in facts.dhcp.pools:
        try:
            lo, hi = ipaddress.ip_address(p.start), ipaddress.ip_address(p.end)
        except ValueError:
            continue
        size = int(hi) - int(lo) + 1
        if size <= 0:
            continue
        used = sum(1 for lease in active if _in(lease.ip, lo, hi))
        if used * 100 >= size * threshold:
            out.append(
                Finding(
                    "pool_full",
                    p.interface,
                    Severity.CRITICAL if used >= size else Severity.WARNING,
                    f"DHCP pool on {p.interface} ({server}) {used * 100 // size}% in use",
                    [f"{used} of {size} addresses leased ({p.start} to {p.end})."],
                )
            )
    return out


def _in(
    ip: str,
    lo: ipaddress.IPv4Address | ipaddress.IPv6Address,
    hi: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == lo.version and lo <= a <= hi


def network_of(facts: HostFacts, ip: str) -> str:
    """The server interface whose network holds `ip`, for naming where a device is."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for p in facts.dhcp.pools if facts.dhcp else []:
        if p.router and p.netmask:
            try:
                if address in ipaddress.ip_network(f"{p.router}/{p.netmask}", strict=False):
                    return p.interface
            except ValueError:
                pass
    for iface in facts.interfaces:
        for a in iface.addresses:
            try:
                if address in ipaddress.ip_interface(a.address).network and a.scope not in ("host", "link"):
                    return iface.name
            except ValueError:
                continue
    return ""


def unknown_devices(
    server: str, facts: HostFacts, known: set[str], cfg: MonitoringSettings, now: datetime
) -> list[Finding]:
    """Devices holding a lease whose MAC address the documentation does not have."""
    names = {p for p in cfg.unknown_device_ignore if "/" not in p}
    prefixes = [ipaddress.ip_network(p, strict=False) for p in cfg.unknown_device_ignore if "/" in p]
    out: list[Finding] = []
    seen: set[str] = set()
    for lease in facts.dhcp.leases if facts.dhcp else []:
        mac = lease.mac.lower()
        if mac in known or mac in seen or (lease.expires_at is not None and lease.expires_at <= now):
            continue
        network = network_of(facts, lease.ip)
        try:
            address = ipaddress.ip_address(lease.ip)
        except ValueError:
            continue
        if network in names or any(address in p for p in prefixes):
            continue
        seen.add(mac)
        label = f"{lease.hostname} ({mac})" if lease.hostname else mac
        out.append(
            Finding(
                "unknown_device",
                mac,
                Severity.INFO,
                f"Unknown device {label} on {network or server}",
                [f"{lease.ip} leased from {server}" + (f" on {network}" if network else "") + "."],
            )
        )
    return out
