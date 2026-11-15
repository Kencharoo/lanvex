"""Turn raw command results into HostFacts: which stack is in charge and who owns each interface."""

from dataclasses import dataclass
from datetime import UTC, datetime

from lanvex.integrations.linux import parsers as P
from lanvex.integrations.linux.commands import Cmd
from lanvex.integrations.linux.facts import (
    DNSInfo,
    HostFacts,
    Interface,
    LldpNeighbor,
    NetworkStack,
    SystemInfo,
)

RESOLVED_STUB = "127.0.0.53"


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int | None  # None when the command could not be run at all (timeout, channel error)

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


Results = dict[Cmd, CommandResult]


def _out(results: Results, cmd: Cmd) -> str:
    r = results.get(cmd)
    return r.stdout if r else ""


def detect_stack(results: Results) -> NetworkStack:
    units = P.parse_unit_states(_out(results, Cmd.UNIT_STATES))
    netplan = P.parse_netplan_files(_out(results, Cmd.NETPLAN_FILES))
    ifaces_main, _ = P.parse_ifupdown(_out(results, Cmd.IFUPDOWN_CONFIG))
    ifaces_inc, _ = P.parse_ifupdown(_out(results, Cmd.IFUPDOWN_INCLUDES))
    ifupdown = ifaces_main + [n for n in ifaces_inc if n not in ifaces_main]

    nm = units.get("NetworkManager") == "active"
    networkd = units.get("systemd-networkd") == "active"
    networking = units.get("networking") == "active"
    evidence: list[str] = [f"{u}: {s}" for u, s in units.items()]
    if netplan:
        evidence.append(f"netplan configuration present ({', '.join(netplan)})")
    if ifupdown:
        evidence.append(f"/etc/network/interfaces configures {', '.join(ifupdown)}")
    if all(s == "unknown" for s in units.values()):
        evidence.append("systemd unit states could not be read (systemctl unavailable)")

    if netplan and networkd and not nm:
        key, label = "netplan-networkd", "netplan → systemd-networkd"
    elif netplan and nm:
        key, label = (
            "netplan-networkmanager",
            "netplan → NetworkManager"
            if not networkd
            else "netplan with NetworkManager and systemd-networkd",
        )
    elif nm:
        key, label = "networkmanager", "NetworkManager"
    elif networkd:
        key, label = "networkd", "systemd-networkd"
    elif ifupdown and (networking or units.get("networking") == "unknown"):
        key, label = "ifupdown", "ifupdown (/etc/network/interfaces)"
    else:
        key, label = "unknown", "Not determined"
    return NetworkStack(
        primary=label,
        key=key,
        units=units,
        netplan_files=netplan,
        ifupdown_interfaces=ifupdown,
        evidence=evidence,
    )


def _assign_ownership(interfaces: list[Interface], results: Results, stack: NetworkStack) -> None:
    nm = (
        P.parse_nmcli_devices(_out(results, Cmd.NMCLI_DEVICES))
        if stack.units.get("NetworkManager") == "active"
        else {}
    )
    nd = (
        P.parse_networkctl(_out(results, Cmd.NETWORKCTL_LIST))
        if stack.units.get("systemd-networkd") == "active"
        else {}
    )
    ifupdown = set(stack.ifupdown_interfaces) if stack.key == "ifupdown" else set()
    for iface in interfaces:
        if iface.name in nm and nm[iface.name]["state"] not in ("unmanaged",):
            iface.managed_by = "NetworkManager"
            iface.manager_state = nm[iface.name]["state"]
            iface.manager_profile = nm[iface.name]["connection"]
        elif iface.name in nd and nd[iface.name]["setup"] not in ("unmanaged", "-"):
            iface.managed_by = "systemd-networkd"
            iface.manager_state = nd[iface.name]["setup"]
        elif iface.name in ifupdown:
            iface.managed_by = "ifupdown"
            iface.manager_state = "configured"
        elif iface.name in nm:
            iface.manager_state = nm[iface.name]["state"]
        elif iface.name in nd:
            iface.manager_state = nd[iface.name]["setup"]


def _dns(results: Results, stack: NetworkStack) -> DNSInfo:
    servers, search, options = P.parse_resolv_conf(_out(results, Cmd.RESOLV_CONF))
    stub = servers == [RESOLVED_STUB] or RESOLVED_STUB in servers
    resolved = None
    if stack.units.get("systemd-resolved") == "active":
        resolved = P.parse_resolvectl(_out(results, Cmd.RESOLVECTL_DNS), _out(results, Cmd.RESOLVECTL_DOMAIN))
    if stub and resolved:
        effective: list[str] = []
        for scope in resolved:
            effective.extend(s for s in scope.servers if s not in effective)
        source = "systemd-resolved (resolv.conf points at its local stub)"
    else:
        effective, source = servers, "/etc/resolv.conf"
    return DNSInfo(
        resolv_conf_servers=servers,
        resolv_conf_search=search,
        resolv_conf_options=options,
        uses_resolved_stub=stub,
        resolved=resolved,
        effective_servers=effective,
        source=source,
    )


def assemble(results: Results) -> HostFacts:
    warnings: list[str] = []

    def must(cmd: Cmd, what: str) -> str:
        r = results.get(cmd)
        if r is None or r.exit_status is None:
            warnings.append(f"Could not read {what}: the command did not complete.")
            return ""
        if not r.ok:
            detail = (r.stderr.strip().splitlines() or [f"exit status {r.exit_status}"])[-1]
            warnings.append(f"Could not read {what}: {detail}")
            return ""
        return r.stdout

    os_info = P.classify_os(P.parse_os_release(must(Cmd.OS_RELEASE, "the OS release")))
    try:
        interfaces = P.parse_addresses(
            must(Cmd.ADDRESSES, "interfaces and addresses (is iproute2 installed?)")
        )
    except ValueError:
        interfaces = []
        warnings.append("Interface data was not valid JSON. iproute2 4.13 or newer is required.")
    routes = []
    for cmd, fam in ((Cmd.ROUTES_V4, 4), (Cmd.ROUTES_V6, 6)):
        try:
            routes += P.parse_routes(must(cmd, f"IPv{fam} routes"), fam)
        except ValueError:
            warnings.append(f"IPv{fam} route data was not valid JSON.")

    stack = detect_stack(results)
    _assign_ownership(interfaces, results, stack)
    dns = _dns(results, stack)

    defaults = [r for r in routes if r.is_default and r.type in (None, "unicast")]
    for fam in (4, 6):
        gws = {(r.gateway, r.interface, r.metric) for r in defaults if r.family == fam}
        if len(gws) > 1 and len({r.metric for r in defaults if r.family == fam}) < len(gws):
            warnings.append(
                f"Several IPv{fam} default routes with the same metric; traffic may not take the path you expect."
            )
    try:
        neighbors = P.parse_neighbors(_out(results, Cmd.NEIGHBORS))
    except ValueError:
        neighbors = []
        warnings.append("Neighbour data was not valid JSON.")
    lldp = _lldp(results, warnings)

    if not os_info.supported:
        warnings.append(os_info.support_note)
    if stack.key == "unknown":
        warnings.append("No supported network manager was detected; interface ownership is unknown.")

    return HostFacts(
        collected_at=datetime.now(UTC),
        system=SystemInfo(
            hostname=must(Cmd.HOSTNAME, "the hostname").strip() or None,
            kernel=must(Cmd.KERNEL, "the kernel version").strip() or None,
            iproute2_version=P.parse_ip_version(_out(results, Cmd.IP_VERSION)),
            os=os_info,
        ),
        interfaces=interfaces,
        routes=routes,
        dns=dns,
        stack=stack,
        neighbors=neighbors,
        lldp=lldp,
        warnings=warnings,
    )


def _lldp(results: Results, warnings: list[str]) -> list[LldpNeighbor]:
    """LLDP neighbours from lldpd and systemd-networkd, whichever run. lldpd's are more detailed, so
    when both saw the same neighbour on the same interface, lldpd's entry is kept."""
    found: list[LldpNeighbor] = []
    r = results.get(Cmd.LLDPCLI_NEIGHBORS)
    if r is not None:
        if r.ok:
            found += P.parse_lldpcli(r.stdout)
        elif r.exit_status == 126 or "denied" in r.stderr.lower():
            warnings.append(
                "lldpd runs, but this account may not ask it for LLDP neighbours. To include them, add it "
                "to the group that may run lldpcli (adm on Debian and Ubuntu: stat -c %G /usr/sbin/lldpcli)."
            )
    r = results.get(Cmd.NETWORKCTL_LLDP)
    if r is not None and r.ok:
        seen = {(n.interface, n.chassis_id, n.port_id) for n in found}
        found += [
            n for n in P.parse_networkctl_lldp(r.stdout) if (n.interface, n.chassis_id, n.port_id) not in seen
        ]
    return sorted(found, key=lambda n: (n.interface, n.system_name or "", n.port_id or ""))
