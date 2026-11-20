"""What monitoring finds on a server or switch, from its latest read and the documentation. Pure
functions."""

import fnmatch
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lanvex.core.vendors import vendor
from lanvex.integrations.linux.facts import HostFacts
from lanvex.integrations.snmp.switch import SwitchFacts
from lanvex.monitoring.models import Severity
from lanvex.monitoring.schemas import MonitoringSettings
from lanvex.networks.schemas import NetworksView
from lanvex.servers.models import ConnectionStatus
from lanvex.servers.schemas import Comparison, SettingRow

# Alerts about events rather than conditions: they end when acknowledged (or, for unknown devices,
# when the device is documented), and each is reported once per server or switch.
EVENT_KINDS = {"unknown_device", "change_failed", "device_moved"}

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
    # A server that does not answer at all is the probe's alert (unreachable), not a second one here.
    if status in (ConnectionStatus.OK, ConnectionStatus.UNREACHABLE):
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


SWITCH_READ_FAILED = {
    "auth_failed": "SNMP sign-in rejected",
    "not_allowed": "address not allowed",
    "error": "read failed",
}


def switch_read_failed(switch: str, status: str | None, message: str | None) -> list[Finding]:
    # A switch that does not answer at all is the probe's alert (unreachable).
    if status in (None, "ok", "unreachable"):
        return []
    return [
        Finding(
            "read_failed",
            "",
            Severity.WARNING,
            f"{switch}: {SWITCH_READ_FAILED.get(status, 'read failed')}",
            [message] if message else [],
        )
    ]


def switch_unreachable(switch: str, failures: int, after: int, error: str | None) -> list[Finding]:
    if failures < after:
        return []
    return [
        Finding(
            "unreachable",
            "",
            Severity.CRITICAL,
            f"{switch} is not reachable",
            [f"SNMP not answering ({failures} checks in a row)."] + ([error] if error else []),
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
        # Only what contradicts the documentation: a setting nothing documents is no alert, but a route
        # missing from a documented list of routes is.
        routes_listed = isinstance((comparison.documented_settings or {}).get("routes"), list)
        for st in comparison.settings:
            contradicts = st.status in ("differs", "only_documented") or (
                st.kind == "route" and st.status == "only_on_server" and routes_listed
            )
            if not contradicts or _ignored(st.interface, cfg.drift_ignore_interfaces):
                continue
            lines.append(_setting_line(st))
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


def _setting_line(st: SettingRow) -> str:
    if st.kind == "gateway":
        where = f" ({st.interface})" if st.interface else ""
        return f"Default gateway {st.server}{where}: documented gateway of {st.prefix} is {st.documented}"
    if st.kind == "route":
        if st.status == "only_documented":
            return f"Route to {st.key} via {st.documented}: documented, not on the server"
        if st.status == "only_on_server":
            return f"Route to {st.key} via {st.server}: on the server, not documented"
        return f"Route to {st.key}: via {st.server}, documented via {st.documented}"
    name = "DNS servers" if st.kind == "dns_servers" else "Search domains"
    return f"{name}: {st.server or 'none'}, documented {st.documented or 'none'}"


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
        maker = vendor(mac)
        label = f"{lease.hostname} ({mac})" if lease.hostname else f"{mac} ({maker})" if maker else mac
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


# ---- switches ------------------------------------------------------------------------------------------


def ignored_vlans(cfg: MonitoringSettings) -> set[int]:
    return {int(p.split(":", 1)[1]) for p in cfg.unknown_device_ignore if p.lower().startswith("vlan:")}


def watch(
    old: list[dict[str, Any]], facts: SwitchFacts, peers: dict[int, tuple[str, bool]]
) -> list[dict[str, Any]]:
    """The ports to watch: those that led to another device when last up. `peers` names, for each port up
    now, what it leads to (an LLDP neighbour or another switch) and whether it is an uplink. A watched
    port stays watched while it is down; one that is up with nothing on it any more is dropped."""
    ports = {p.index: p for p in facts.ports}
    out: dict[int, dict[str, Any]] = {}
    for w in old:
        p = ports.get(w.get("index", -1))
        if p is not None and not (p.oper_up and p.index not in peers):
            known = {"peer": peers[p.index][0], "uplink": peers[p.index][1]} if p.index in peers else {}
            out[p.index] = {**w, "name": p.name, **known}
    for idx, (peer, uplink) in peers.items():
        p = ports.get(idx)
        if p is not None and p.oper_up:
            out[idx] = {"index": idx, "name": p.name, "peer": peer, "uplink": uplink}
    return [out[i] for i in sorted(out)]


def ports_down(switch: str, facts: SwitchFacts, watched: list[dict[str, Any]]) -> list[Finding]:
    """Watched ports that are down now. A port turned off on purpose (admin down) is not an alert."""
    ports = {p.index: p for p in facts.ports}
    out = []
    for w in watched:
        p = ports.get(w.get("index", -1))
        if p is None or p.admin_up is False or p.oper_up is not False:
            continue
        out.append(
            Finding(
                "port_down",
                p.name,
                Severity.CRITICAL if w.get("uplink") else Severity.WARNING,
                f"{switch} {p.name} is down",
                [f"It led to {w['peer']}." if w.get("peer") else "It led to another device."],
            )
        )
    return out


@dataclass
class SeenDevice:
    mac: str
    port: str
    vlan: int | None
    addresses: list[str]
    vendor: str | None


def switch_unknown_devices(switch: str, devices: list[SeenDevice], cfg: MonitoringSettings) -> list[Finding]:
    """Devices that appeared on the switch's ports after its first read and are documented nowhere."""
    vlans = ignored_vlans(cfg)
    prefixes = [ipaddress.ip_network(p, strict=False) for p in cfg.unknown_device_ignore if "/" in p]
    out = []
    for d in devices:
        if d.vlan in vlans:
            continue
        addresses = []
        for a in d.addresses:
            try:
                addresses.append(ipaddress.ip_address(a))
            except ValueError:
                continue
        if any(a in p for a in addresses for p in prefixes if a.version == p.version):
            continue
        label = f"{d.mac} ({d.vendor})" if d.vendor else d.mac
        detail = [f"On {switch} {d.port}" + (f", VLAN {d.vlan}" if d.vlan else "") + "."]
        if d.addresses:
            detail.append(f"Seen with {', '.join(d.addresses)}.")
        out.append(
            Finding(
                "unknown_device", d.mac, Severity.INFO, f"Unknown device {label} on {switch} {d.port}", detail
            )
        )
    return out


def device_moves(moves: list[tuple[str, str, str, str]]) -> list[Finding]:
    """(who, key, now on, was on) for documented devices now plugged into another port."""
    return [
        Finding("device_moved", key, Severity.INFO, f"{who} moved to {now}", [f"Was on {was}."])
        for who, key, now, was in moves
    ]
