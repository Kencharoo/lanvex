"""Reading a switch over SNMP: system, ports, VLANs, MAC address table, LLDP neighbours and ARP.

Standard MIBs first (IF-MIB, BRIDGE-MIB, Q-BRIDGE-MIB, LLDP-MIB, IP-MIB, IEEE8023-LAG-MIB), so most
managed switches work as they are. Cisco IOS switches without Q-BRIDGE-MIB are read through their VTP
MIB and per-VLAN bridge tables. What a switch does not have is noted, not an error.
"""

import ipaddress
import string
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from lanvex.integrations.snmp.client import ContextUnavailable, Oid


class Walker(Protocol):
    async def get(self, *oids: str) -> dict[str, Any]: ...

    async def walk(self, oid: str, vlan: int | None = None) -> list[tuple[Oid, Any]]: ...


# ---- facts --------------------------------------------------------------------------------------------


class SwitchSystem(BaseModel):
    name: str | None = None
    description: str | None = None
    object_id: str | None = None
    uptime_seconds: int | None = None
    location: str | None = None
    bridge_mac: str | None = None  # the switch's own MAC (dot1dBaseBridgeAddress)
    lldp_chassis_id: str | None = None
    lldp_system_name: str | None = None


class SwitchPort(BaseModel):
    index: int  # ifIndex
    name: str
    description: str | None = None  # ifDescr
    alias: str | None = None  # ifAlias: the description an administrator gave the port
    type: int | None = None  # IANAifType
    kind: str = "other"  # ethernet, lag, vlan, loopback, wireless, other
    mac: str | None = None
    admin_up: bool | None = None
    oper_up: bool | None = None
    speed_mbps: int | None = None
    bridge_port: int | None = None
    lag: int | None = None  # the ifIndex of the link aggregation this port is a member of
    pvid: int | None = None  # untagged traffic coming in goes to this VLAN
    untagged: list[int] = Field(default_factory=list)
    tagged: list[int] = Field(default_factory=list)
    trunk: bool | None = None  # Cisco: the port is trunking


class SwitchVlan(BaseModel):
    id: int
    name: str | None = None


class FdbEntry(BaseModel):
    mac: str
    vlan: int | None = None
    port: int  # ifIndex


class SwitchNeighbor(BaseModel):
    """What the device at the other end of a port announces about itself with LLDP."""

    port: int | None = None  # ifIndex, when Lanvex could tell which port
    port_name: str | None = None
    chassis_id: str | None = None
    port_id: str | None = None
    port_description: str | None = None
    system_name: str | None = None
    system_description: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    management_addresses: list[str] = Field(default_factory=list)


class ArpEntry(BaseModel):
    address: str
    mac: str
    interface: int | None = None  # ifIndex of the routed interface (usually a VLAN interface)


class SwitchFacts(BaseModel):
    collected_at: datetime
    system: SwitchSystem
    ports: list[SwitchPort] = Field(default_factory=list)
    vlans: list[SwitchVlan] = Field(default_factory=list)
    fdb: list[FdbEntry] = Field(default_factory=list)
    neighbors: list[SwitchNeighbor] = Field(default_factory=list)
    arp: list[ArpEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # what the switch does not provide

    def port(self, index: int | None) -> SwitchPort | None:
        return next((p for p in self.ports if p.index == index), None) if index is not None else None


# ---- OIDs ---------------------------------------------------------------------------------------------

SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.3.0"
SYS_NAME, SYS_LOCATION = "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.6.0"

IF = "1.3.6.1.2.1.2.2.1"  # ifTable
IF_DESCR, IF_TYPE, IF_SPEED, IF_MAC = f"{IF}.2", f"{IF}.3", f"{IF}.5", f"{IF}.6"
IF_ADMIN, IF_OPER = f"{IF}.7", f"{IF}.8"
IFX = "1.3.6.1.2.1.31.1.1.1"  # ifXTable
IF_NAME, IF_HIGH_SPEED, IF_ALIAS = f"{IFX}.1", f"{IFX}.15", f"{IFX}.18"

BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"
BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
FDB_PORT, FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.2", "1.3.6.1.2.1.17.4.3.1.3"

Q = "1.3.6.1.2.1.17.7.1"
Q_FDB_PORT, Q_FDB_STATUS = f"{Q}.2.2.1.2", f"{Q}.2.2.1.3"
Q_VLAN_FDB_ID = f"{Q}.4.2.1.3"
Q_CURRENT_EGRESS, Q_CURRENT_UNTAGGED = f"{Q}.4.2.1.4", f"{Q}.4.2.1.5"
Q_STATIC_NAME, Q_STATIC_EGRESS, Q_STATIC_UNTAGGED = f"{Q}.4.3.1.1", f"{Q}.4.3.1.2", f"{Q}.4.3.1.4"
Q_PVID = f"{Q}.4.5.1.1"

LAG_ATTACHED = "1.2.840.10006.300.43.1.2.1.1.13"  # dot3adAggPortAttachedAggID

LLDP = "1.0.8802.1.1.2.1"
LLDP_LOC_CHASSIS_SUBTYPE, LLDP_LOC_CHASSIS = f"{LLDP}.3.1.0", f"{LLDP}.3.2.0"
LLDP_LOC_SYSNAME = f"{LLDP}.3.3.0"
LLDP_LOC_PORT = f"{LLDP}.3.7.1"  # .2 subtype, .3 id, .4 description
LLDP_REM = f"{LLDP}.4.1.1"  # .4 chassis subtype ... .12 capabilities enabled
LLDP_REM_MAN_ADDR = f"{LLDP}.4.2.1.3"

ARP_MAC, ARP_TYPE = "1.3.6.1.2.1.4.22.1.2", "1.3.6.1.2.1.4.22.1.4"  # ipNetToMediaTable
NEIGHBOR_MAC = "1.3.6.1.2.1.4.35.1.4"  # ipNetToPhysicalTable, IPv4 and IPv6

VTP_STATE, VTP_NAME = "1.3.6.1.4.1.9.9.46.1.3.1.1.2", "1.3.6.1.4.1.9.9.46.1.3.1.1.4"
VTP_TRUNK = "1.3.6.1.4.1.9.9.46.1.6.1.1"  # .4 VLANs enabled 0-1023, .5 native VLAN, .14 trunking
VM_VLAN = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"  # access VLAN per ifIndex

CAPABILITIES = ["Other", "Repeater", "Bridge", "WLAN", "Router", "Telephone", "DOCSIS", "Station"]
KIND = {6: "ethernet", 62: "ethernet", 69: "ethernet", 117: "ethernet", 161: "lag", 53: "vlan"}
KIND |= {135: "vlan", 136: "vlan", 24: "loopback", 71: "wireless"}
PER_VLAN_LIMIT = 256  # Cisco per-VLAN reads: at most this many VLANs
CISCO_RESERVED = range(1002, 1006)


# ---- value helpers ------------------------------------------------------------------------------------


def mac(raw: Any) -> str | None:
    if isinstance(raw, bytes) and len(raw) == 6 and any(raw):
        return ":".join(f"{b:02x}" for b in raw)
    if isinstance(raw, tuple) and len(raw) == 6:
        return ":".join(f"{b:02x}" for b in raw)
    return None


_PRINTABLE = set(string.printable) - set("\x0b\x0c")


def text(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        decoded = raw.decode("utf-8", errors="replace").strip("\x00").strip()
        if decoded and not all(c in _PRINTABLE or ord(c) > 127 for c in decoded):
            return ":".join(f"{b:02x}" for b in raw)
        return decoded or None
    return str(raw).strip() or None


def portlist(raw: Any) -> set[int]:
    """A Q-BRIDGE PortList: bit 1 (the first byte's high bit) is bridge port 1."""
    if not isinstance(raw, bytes):
        return set()
    return {i * 8 + b + 1 for i, byte in enumerate(raw) for b in range(8) if byte & (0x80 >> b)}


def _lldp_id(subtype: Any, raw: Any, mac_subtype: int, address_subtype: int) -> str | None:
    if subtype == mac_subtype:
        return mac(raw) or text(raw)
    if subtype == address_subtype and isinstance(raw, bytes) and len(raw) in (5, 17):
        try:
            return str(ipaddress.ip_address(raw[1:]))
        except ValueError:
            return text(raw)
    return text(raw)


def _capabilities(raw: Any) -> list[str]:
    bits = portlist(raw)  # BITS: same layout, counted from 1
    return [CAPABILITIES[b - 1] for b in sorted(bits) if b <= len(CAPABILITIES)]


def _columns(rows: list[tuple[Oid, Any]]) -> dict[int, dict[Oid, Any]]:
    """A table walked whole: {column: {index: value}}."""
    out: dict[int, dict[Oid, Any]] = defaultdict(dict)
    for oid, v in rows:
        if len(oid) >= 2:
            out[oid[0]][oid[1:]] = v
    return out


def _single(rows: list[tuple[Oid, Any]]) -> dict[int, Any]:
    """A column indexed by one integer."""
    return {oid[0]: v for oid, v in rows if len(oid) == 1}


# ---- reading ------------------------------------------------------------------------------------------


async def read_switch(s: Walker) -> SwitchFacts:
    notes: list[str] = []
    sysv = await s.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, SYS_NAME, SYS_LOCATION)
    scalars = await s.get(BRIDGE_ADDRESS, LLDP_LOC_CHASSIS_SUBTYPE, LLDP_LOC_CHASSIS, LLDP_LOC_SYSNAME)
    uptime = sysv.get(SYS_UPTIME)
    system = SwitchSystem(
        name=text(sysv.get(SYS_NAME)),
        description=text(sysv.get(SYS_DESCR)),
        object_id=sysv.get(SYS_OBJECT_ID) if isinstance(sysv.get(SYS_OBJECT_ID), str) else None,
        uptime_seconds=uptime // 100 if isinstance(uptime, int) else None,
        location=text(sysv.get(SYS_LOCATION)),
        bridge_mac=mac(scalars.get(BRIDGE_ADDRESS)),
        lldp_chassis_id=_lldp_id(scalars.get(LLDP_LOC_CHASSIS_SUBTYPE), scalars.get(LLDP_LOC_CHASSIS), 4, 5),
        lldp_system_name=text(scalars.get(LLDP_LOC_SYSNAME)),
    )

    ports = await _ports(s)
    by_index = {p.index: p for p in ports}
    bridge = {bp: idx for bp, idx in _single(await s.walk(BASE_PORT_IFINDEX)).items() if isinstance(idx, int)}
    for bp, idx in bridge.items():
        if idx in by_index:
            by_index[idx].bridge_port = bp
    for idx, agg in _single(await s.walk(LAG_ATTACHED)).items():
        if idx in by_index and isinstance(agg, int) and agg and agg != idx:
            by_index[idx].lag = agg

    vlans, fdb = await _qbridge(s, by_index, bridge)
    if not vlans:
        vlans, fdb_cisco = await _cisco(s, by_index, fdb, notes)
        fdb = fdb or fdb_cisco
    if not fdb:
        fdb = await _fdb(s, bridge, None)
    if not bridge and not fdb:
        notes.append("No bridge tables (BRIDGE-MIB): the MAC address table was not read.")
    if not vlans:
        notes.append("No VLAN tables (Q-BRIDGE-MIB): VLANs were not read.")

    neighbors = await _lldp(s, ports, bridge)
    if neighbors is None:
        notes.append("No LLDP-MIB: neighbours were not read.")

    return SwitchFacts(
        collected_at=datetime.now(UTC),
        system=system,
        ports=ports,
        vlans=sorted(vlans, key=lambda v: v.id),
        fdb=fdb,
        neighbors=neighbors or [],
        arp=await _arp(s),
        notes=notes,
    )


async def _ports(s: Walker) -> list[SwitchPort]:
    descr = _single(await s.walk(IF_DESCR))
    names = _single(await s.walk(IF_NAME))
    aliases = _single(await s.walk(IF_ALIAS))
    types = _single(await s.walk(IF_TYPE))
    macs = _single(await s.walk(IF_MAC))
    admin = _single(await s.walk(IF_ADMIN))
    oper = _single(await s.walk(IF_OPER))
    high = _single(await s.walk(IF_HIGH_SPEED))
    speed = _single(await s.walk(IF_SPEED))
    out = []
    for idx in sorted(set(descr) | set(names)):
        t = types.get(idx)
        mbps = high.get(idx) if isinstance(high.get(idx), int) and high.get(idx) else None
        if mbps is None and isinstance(speed.get(idx), int) and 0 < speed[idx] < 4294967295:
            mbps = speed[idx] // 1_000_000
        out.append(
            SwitchPort(
                index=idx,
                name=text(names.get(idx)) or text(descr.get(idx)) or f"ifIndex {idx}",
                description=text(descr.get(idx)),
                alias=text(aliases.get(idx)),
                type=t if isinstance(t, int) else None,
                kind=KIND.get(t, "other") if isinstance(t, int) else "other",
                mac=mac(macs.get(idx)),
                admin_up=admin[idx] == 1 if isinstance(admin.get(idx), int) else None,
                oper_up=oper[idx] == 1 if isinstance(oper.get(idx), int) else None,
                speed_mbps=mbps or None,
            )
        )
    return out


def _fdb_rows(port_rows: list[tuple[Oid, Any]], status_rows: list[tuple[Oid, Any]]) -> list[tuple[Oid, int]]:
    """(index, bridge port) of the learned entries; entries for the switch itself are left out."""
    status = {oid: v for oid, v in status_rows}
    return [
        (oid, bp)
        for oid, bp in port_rows
        if isinstance(bp, int) and bp > 0 and status.get(oid, 3) in (1, 3)  # other, learned
    ]


async def _fdb(s: Walker, bridge: dict[int, int], vlan: int | None) -> list[FdbEntry]:
    out = []
    for oid, bp in _fdb_rows(await s.walk(FDB_PORT, vlan), await s.walk(FDB_STATUS, vlan)):
        m = mac(oid[-6:]) if len(oid) == 6 else None
        if m and bp in bridge:
            out.append(FdbEntry(mac=m, vlan=vlan, port=bridge[bp]))
    return out


async def _qbridge(
    s: Walker, by_index: dict[int, SwitchPort], bridge: dict[int, int]
) -> tuple[list[SwitchVlan], list[FdbEntry]]:
    names = _single(await s.walk(Q_STATIC_NAME))
    egress = {oid[-1]: v for oid, v in await s.walk(Q_CURRENT_EGRESS) if len(oid) == 2}
    untagged = {oid[-1]: v for oid, v in await s.walk(Q_CURRENT_UNTAGGED) if len(oid) == 2}
    if not egress:
        egress = _single(await s.walk(Q_STATIC_EGRESS))
        untagged = _single(await s.walk(Q_STATIC_UNTAGGED))
    ids = sorted(set(names) | set(egress))
    vlans = [SwitchVlan(id=v, name=text(names.get(v))) for v in ids if 1 <= v <= 4094]
    for vid in ids:
        members, plain = portlist(egress.get(vid)), portlist(untagged.get(vid))
        for bp in members:
            p = by_index.get(bridge.get(bp, -1))
            if p is not None:
                (p.untagged if bp in plain else p.tagged).append(vid)
    for bp, pvid in _single(await s.walk(Q_PVID)).items():
        p = by_index.get(bridge.get(bp, -1))
        if p is not None and isinstance(pvid, int):
            p.pvid = pvid

    fdb_ids = {oid[-1]: fid for oid, fid in await s.walk(Q_VLAN_FDB_ID) if len(oid) == 2}
    vlan_of = {fid: vid for vid, fid in fdb_ids.items() if isinstance(fid, int)}
    fdb = []
    for oid, bp in _fdb_rows(await s.walk(Q_FDB_PORT), await s.walk(Q_FDB_STATUS)):
        if len(oid) != 7 or bp not in bridge:
            continue
        m = mac(oid[1:])
        if m:
            fdb.append(FdbEntry(mac=m, vlan=vlan_of.get(oid[0], oid[0]) or None, port=bridge[bp]))
    return vlans, fdb


async def _cisco(
    s: Walker, by_index: dict[int, SwitchPort], fdb: list[FdbEntry], notes: list[str]
) -> tuple[list[SwitchVlan], list[FdbEntry]]:
    states = {oid[-1]: v for oid, v in await s.walk(VTP_STATE) if len(oid) == 2}
    if not states:
        return [], []
    names = {oid[-1]: v for oid, v in await s.walk(VTP_NAME) if len(oid) == 2}
    vlans = [
        SwitchVlan(id=v, name=text(names.get(v)))
        for v in sorted(states)
        if states[v] == 1 and v not in CISCO_RESERVED
    ]
    ids = {v.id for v in vlans}
    for idx, vid in _single(await s.walk(VM_VLAN)).items():
        p = by_index.get(idx)
        if p is not None and isinstance(vid, int) and vid:
            p.pvid, p.untagged, p.trunk = vid, [vid], False
    trunk = _columns(await s.walk(VTP_TRUNK))
    for (idx,), status in ((k, v) for k, v in trunk.get(14, {}).items() if len(k) == 1):
        p = by_index.get(idx)
        if p is None or status != 1:
            continue
        native = trunk.get(5, {}).get((idx,))
        enabled = {b - 1 for b in portlist(trunk.get(4, {}).get((idx,)))}  # bit 1 is VLAN 0
        p.trunk = True
        p.pvid = native if isinstance(native, int) else p.pvid
        p.untagged = [native] if isinstance(native, int) and native in ids else []
        p.tagged = sorted((enabled & ids) - set(p.untagged))
    if fdb:
        return vlans, []
    out: list[FdbEntry] = []
    failed = 0
    for v in vlans[:PER_VLAN_LIMIT]:
        try:
            bridge = {bp: idx for bp, idx in _single(await s.walk(BASE_PORT_IFINDEX, v.id)).items()}
            if bridge:
                out += await _fdb(s, bridge, v.id)
        except ContextUnavailable:
            failed += 1
            if failed >= 3 and not out:  # not set up on this switch: do not wait for every VLAN
                notes.append(
                    "The per-VLAN MAC address tables do not answer (SNMPv2c: community@vlan, SNMPv3: "
                    "contexts vlan-<id>); only the default one was read."
                )
                return vlans, []
    if len(vlans) > PER_VLAN_LIMIT:
        notes.append(f"MAC addresses were read for the first {PER_VLAN_LIMIT} VLANs only.")
    return vlans, out


def _match_port(ports: list[SwitchPort], *names: str | None) -> int | None:
    wanted = {n.lower() for n in names if n}
    for p in ports:
        if wanted & {(x or "").lower() for x in (p.name, p.description, p.alias)} - {""}:
            return p.index
    return None


async def _lldp(s: Walker, ports: list[SwitchPort], bridge: dict[int, int]) -> list[SwitchNeighbor] | None:
    rem = _columns(await s.walk(LLDP_REM))
    local = _columns(await s.walk(LLDP_LOC_PORT))
    if not rem and not local:
        return None
    indexes = {p.index for p in ports}

    def port_of(num: int) -> int | None:
        idx = None
        if (num,) in local.get(3, {}):
            subtype = local.get(2, {}).get((num,))
            raw = local[3][(num,)]
            if subtype == 3:  # macAddress
                hits = [p.index for p in ports if p.mac and p.mac == mac(raw)]
                idx = hits[0] if len(hits) == 1 else None
            else:
                idx = _match_port(ports, text(raw), text(local.get(4, {}).get((num,))))
        if idx is None and num in bridge:
            idx = bridge[num]
        if idx is None and num in indexes:
            idx = num
        return idx

    addresses: dict[tuple[int, int], list[str]] = defaultdict(list)
    for oid, _ in await s.walk(LLDP_REM_MAN_ADDR):
        # timeMark . localPort . remIndex . addressSubtype . length . address…
        if len(oid) < 6:
            continue
        key, family, length, raw = (oid[1], oid[2]), oid[3], oid[4], bytes(oid[5:])
        if family in (1, 2) and len(raw) == length:
            try:
                addresses[key].append(str(ipaddress.ip_address(raw)))
            except ValueError:
                continue

    out: list[SwitchNeighbor] = []
    for index in sorted({i[1:3] for i in rem.get(5, {}) if len(i) == 3}):

        def col(c: int, index: tuple[int, ...] = index) -> Any:
            return next((v for k, v in rem.get(c, {}).items() if k[1:3] == index), None)

        idx = port_of(index[0])
        p = next((x for x in ports if x.index == idx), None)
        out.append(
            SwitchNeighbor(
                port=idx,
                port_name=p.name if p else None,
                chassis_id=_lldp_id(col(4), col(5), 4, 5),
                port_id=_lldp_id(col(6), col(7), 3, 4),
                port_description=text(col(8)),
                system_name=text(col(9)),
                system_description=text(col(10)),
                capabilities=_capabilities(col(12)),
                management_addresses=addresses.get(index, []),
            )
        )
    return out


async def _arp(s: Walker) -> list[ArpEntry]:
    out: dict[str, ArpEntry] = {}
    types = {oid: v for oid, v in await s.walk(ARP_TYPE)}
    for oid, raw in await s.walk(ARP_MAC):
        m = mac(raw)
        if len(oid) != 5 or not m or types.get(oid) == 2:  # invalid
            continue
        address = ".".join(str(x) for x in oid[1:])
        out[address] = ArpEntry(address=address, mac=m, interface=oid[0])
    for oid, raw in await s.walk(NEIGHBOR_MAC):
        # ifIndex . addressType . length . address…
        m = mac(raw)
        if len(oid) < 4 or not m or oid[1] not in (1, 2) or len(oid) - 3 != oid[2]:
            continue
        try:
            ip = ipaddress.ip_address(bytes(oid[3:]))
        except ValueError:
            continue
        if ip.is_link_local or ip.is_multicast:
            continue
        out.setdefault(str(ip), ArpEntry(address=str(ip), mac=m, interface=oid[0]))
    return sorted(out.values(), key=lambda a: (":" in a.address, ipaddress.ip_address(a.address)))
