"""Pure parsers for the read-only commands in commands.py.

Tested against output captured from real hosts (tests/fixtures/linux). Every parser tolerates empty
or missing output, because the tool may not be installed or the unit may not be running.
"""

import ipaddress
import json
import re
import shlex
from typing import Any

from lanvex.integrations.linux.commands import STACK_UNITS
from lanvex.integrations.linux.facts import (
    Address,
    Interface,
    LldpNeighbor,
    Neighbor,
    OSInfo,
    ResolverScope,
    Route,
)

FOREVER = 4294967295  # iproute2's "forever" lifetime

# Distributions Lanvex supports, with the oldest release it is tested against.
SUPPORTED: dict[str, tuple[str, tuple[int, ...]]] = {
    "ubuntu": ("Ubuntu Server", (20, 4)),
    "debian": ("Debian", (11,)),
    "fedora": ("Fedora Server", (38,)),
}


# ---- os-release -----------------------------------------------------------------------------


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [raw.strip("\"'")]
        out[key.strip()] = parts[0] if parts else ""
    return out


def _version_tuple(v: str | None) -> tuple[int, ...] | None:
    if not v:
        return None
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:2]) if nums else None


def classify_os(fields: dict[str, str]) -> OSInfo:
    os_id = (fields.get("ID") or "").lower() or None
    like = (fields.get("ID_LIKE") or "").lower().split()
    version = fields.get("VERSION_ID")
    info = dict(
        id=os_id,
        id_like=like,
        name=fields.get("NAME"),
        pretty_name=fields.get("PRETTY_NAME"),
        version_id=version,
        variant_id=fields.get("VARIANT_ID"),
    )
    if os_id in SUPPORTED:
        label, minimum = SUPPORTED[os_id]
        vt = _version_tuple(version)
        if vt is None:
            return OSInfo(
                **info, supported=False, support_note=f"{label}, but the version could not be determined."
            )
        if vt < minimum:
            need = (
                ".".join(f"{n:02d}" if i else str(n) for i, n in enumerate(minimum))
                if os_id == "ubuntu"
                else ".".join(map(str, minimum))
            )
            return OSInfo(
                **info,
                supported=False,
                support_note=f"{label} {version} is older than the supported minimum ({need}).",
            )
        note = f"Supported: {label} {version}."
        if os_id == "fedora" and fields.get("VARIANT_ID") not in (None, "", "server"):
            note = f"Supported distribution (Fedora {version}); variant '{fields.get('VARIANT_ID')}' rather than Server."
        return OSInfo(**info, supported=True, support_note=note)
    family = next((SUPPORTED[x][0] for x in like if x in SUPPORTED), None)
    if family:
        return OSInfo(
            **info,
            supported=False,
            support_note=f"Derived from {family} but not supported itself. Data is read best-effort.",
        )
    return OSInfo(
        **info, supported=False, support_note="Not a supported distribution. Data is read best-effort."
    )


def parse_ip_version(text: str) -> str | None:
    m = re.search(r"iproute2-([\w.\-]+)", text)
    return m.group(1) if m else (text.strip() or None)


# ---- ip -json -------------------------------------------------------------------------------


def _json_list(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else []


_KIND_FROM_LINK_TYPE = {
    "ether": "ethernet",
    "loopback": "loopback",
    "none": "tunnel",
    "ipip": "tunnel",
    "sit": "tunnel",
}


def parse_addresses(text: str) -> list[Interface]:
    """`ip -details -json address show`"""
    out: list[Interface] = []
    for raw in _json_list(text):
        flags = raw.get("flags") or []
        info = raw.get("linkinfo") or {}
        kind = info.get("info_kind") or _KIND_FROM_LINK_TYPE.get(
            raw.get("link_type", ""), raw.get("link_type") or "unknown"
        )
        data = info.get("info_data") or {}
        vlan_id = data.get("id") if kind == "vlan" and isinstance(data, dict) else None
        addrs: list[Address] = []
        for a in raw.get("addr_info") or []:
            fam = a.get("family")
            if fam not in ("inet", "inet6") or "local" not in a:
                continue
            valid = a.get("valid_life_time")
            addrs.append(
                Address(
                    address=f"{a['local']}/{a.get('prefixlen')}",
                    family=4 if fam == "inet" else 6,
                    scope=a.get("scope"),
                    dynamic=bool(a.get("dynamic")) or (valid is not None and valid != FOREVER),
                    secondary=bool(a.get("secondary")),
                    deprecated=bool(a.get("deprecated")),
                    tentative=bool(a.get("tentative")),
                    label=a.get("label"),
                )
            )
        mac = raw.get("address")
        out.append(
            Interface(
                name=raw.get("ifname", "?"),
                index=int(raw.get("ifindex", 0)),
                kind=str(kind),
                mac=None if kind == "loopback" or mac in (None, "00:00:00:00:00:00") else mac,
                mtu=raw.get("mtu"),
                oper_state=str(raw.get("operstate", "UNKNOWN")).lower(),
                admin_up="UP" in flags,
                carrier="LOWER_UP" in flags and "NO-CARRIER" not in flags,
                parent=raw.get("link") if isinstance(raw.get("link"), str) else None,
                master=raw.get("master"),
                vlan_id=vlan_id,
                addresses=addrs,
            )
        )
    return out


def parse_routes(text: str, family: int) -> list[Route]:
    """`ip -json -4|-6 route show table main`"""
    out: list[Route] = []
    for r in _json_list(text):
        dst = r.get("dst", "")
        if dst == "default":
            dst = "0.0.0.0/0" if family == 4 else "::/0"
        elif dst and "/" not in dst:
            dst = f"{dst}/{32 if family == 4 else 128}"
        out.append(
            Route(
                destination=dst,
                family=4 if family == 4 else 6,
                gateway=r.get("gateway"),
                interface=r.get("dev"),
                metric=r.get("metric"),
                protocol=r.get("protocol"),
                scope=r.get("scope"),
                source=r.get("prefsrc"),
                type=r.get("type"),
                flags=list(r.get("flags") or []),
            )
        )
    return out


# ---- DNS ----------------------------------------------------------------------------------------


def parse_resolv_conf(text: str) -> tuple[list[str], list[str], list[str]]:
    servers: list[str] = []
    search: list[str] = []
    options: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        key, *rest = line.split()
        if key == "nameserver" and rest:
            servers.append(rest[0])
        elif key in ("search", "domain"):
            search = rest  # the last search/domain line wins, as in glibc
        elif key == "options":
            options.extend(rest)
    return servers, search, options


_RESOLVECTL_LINE = re.compile(r"^(Global|Link \d+ \((?P<link>[^)]+)\)):\s*(?P<values>.*)$")


def parse_resolvectl(dns_text: str, domain_text: str) -> list[ResolverScope]:
    scopes: dict[str, ResolverScope] = {}

    def feed(text: str, attr: str) -> None:
        for line in text.splitlines():
            m = _RESOLVECTL_LINE.match(line.strip())
            if not m:
                continue
            scope = m.group("link") or "global"
            entry = scopes.setdefault(scope, ResolverScope(scope=scope))
            getattr(entry, attr).extend(m.group("values").split())

    feed(dns_text, "servers")
    feed(domain_text, "domains")
    return list(scopes.values())


# ---- network managers -----------------------------------------------------------------------


def parse_unit_states(text: str) -> dict[str, str]:
    lines = [ln.strip() for ln in text.splitlines()]
    return {
        unit: (lines[i] if i < len(lines) and lines[i] else "unknown") for i, unit in enumerate(STACK_UNITS)
    }


def parse_netplan_files(text: str) -> list[str]:
    return sorted(ln.strip() for ln in text.splitlines() if ln.strip().endswith((".yaml", ".yml")))


def parse_ifupdown(text: str) -> tuple[list[str], bool]:
    """Interfaces configured in /etc/network/interfaces (excluding lo), and whether it sources other files."""
    names: list[str] = []
    sources = False
    for line in text.splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        if words[0] == "iface" and len(words) > 1 and words[1] != "lo" and words[1] not in names:
            names.append(words[1])
        elif words[0] in ("source", "source-directory"):
            sources = True
    return names, sources


def _split_nmcli(line: str) -> list[str]:
    """nmcli -t escapes ':' and '\\' inside fields with a backslash."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_nmcli_devices(text: str) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = _split_nmcli(line)
        if len(parts) < 4:
            continue
        device, dtype, state, conn = parts[:4]
        out[device] = {"type": dtype, "state": state, "connection": conn or None}
    return out


def parse_networkctl(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].isdigit():
            out[parts[1]] = {"type": parts[2], "operational": parts[3], "setup": parts[4]}
    return out


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.split("%", 1)[0])
        return True
    except ValueError:
        return False


# ---- discovery ----------------------------------------------------------------------------------

# States of an entry that names a real, recently seen host (not FAILED/INCOMPLETE lookups, not NOARP).
_SEEN = {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}


def parse_neighbors(text: str) -> list[Neighbor]:
    """`ip -json neigh show`: hosts with a known link-layer address."""
    out: list[Neighbor] = []
    for n in _json_list(text):
        dst, dev, mac = n.get("dst"), n.get("dev"), n.get("lladdr")
        state = next((x for x in n.get("state") or [] if x in _SEEN), None)
        if not dst or not dev or not mac or state is None:
            continue
        try:
            ip = ipaddress.ip_address(dst)
        except ValueError:
            continue
        if ip.is_multicast:
            continue
        out.append(
            Neighbor(
                address=str(ip),
                family=4 if ip.version == 4 else 6,
                interface=dev,
                mac=mac.lower(),
                state=state,
                router="router" in n,
            )
        )
    return out


def _first(items: Any, key: str = "value") -> str | None:
    """lldpcli's json0 format wraps every value in a list of objects."""
    if isinstance(items, list) and items and isinstance(items[0], dict):
        v = items[0].get(key)
        return str(v) if v not in (None, "") else None
    return None


def parse_lldpcli(text: str) -> list[LldpNeighbor]:
    """`lldpcli -f json0 show neighbors details`"""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return []
    out: list[LldpNeighbor] = []
    for block in data.get("lldp") or []:
        for i in (block or {}).get("interface") or []:
            chassis = (i.get("chassis") or [{}])[0]
            port = (i.get("port") or [{}])[0]
            if not i.get("name"):
                continue
            out.append(
                LldpNeighbor(
                    interface=i["name"],
                    source="lldpd",
                    chassis_id=_first(chassis.get("id")),
                    system_name=_first(chassis.get("name")),
                    system_description=_first(chassis.get("descr")),
                    port_id=_first(port.get("id")),
                    port_description=_first(port.get("descr")),
                    management_addresses=[
                        str(m["value"]) for m in chassis.get("mgmt-ip") or [] if m.get("value")
                    ],
                    capabilities=[
                        str(c["type"]) for c in chassis.get("capability") or [] if c.get("enabled")
                    ],
                )
            )
    return out


# networkctl's capability letters (systemd: src/libsystemd-network/lldp-neighbor.c)
_CAPS = {
    "o": "Other",
    "p": "Repeater",
    "b": "Bridge",
    "w": "WLAN",
    "r": "Router",
    "t": "Telephone",
    "d": "DOCSIS",
    "a": "Station",
    "c": "Customer VLAN",
    "s": "Service VLAN",
    "m": "Two-port MAC relay",
}


def parse_networkctl_lldp(text: str) -> list[LldpNeighbor]:
    """`networkctl lldp --no-pager`: a table whose columns are aligned under its header."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("LINK"):
        return []
    header = lines[0]
    names = header.split()
    starts = [header.index(n) for n in names]
    out: list[LldpNeighbor] = []
    for line in lines[1:]:
        if not line.strip():
            break
        cells = {
            n: line[s : (starts[k + 1] if k + 1 < len(starts) else None)].strip()
            for k, (n, s) in enumerate(zip(names, starts, strict=True))
        }
        if not cells.get("LINK"):
            continue
        caps = cells.get("CAPS", "")
        out.append(
            LldpNeighbor(
                interface=cells["LINK"],
                source="networkd",
                chassis_id=cells.get("CHASSIS-ID") or None,
                system_name=cells.get("SYSTEM-NAME") or None,
                port_id=cells.get("PORT-ID") or None,
                port_description=cells.get("PORT-DESCRIPTION") or None,
                capabilities=[_CAPS[c] for c in caps if c in _CAPS],
            )
        )
    return out
