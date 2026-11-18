"""Where devices are connected: switch ports, from the switches' MAC address tables and LLDP, compared
with the cables the documentation records.

A MAC address shows up on every switch between it and the switch that reads it: on the port it is
plugged into, and on the uplinks towards it. A port is an uplink when it leads to another switch Lanvex
reads (LLDP names that switch, or that switch's own MAC address is learned on it). A device is placed on
the one port that is not an uplink; several candidates (an unmanaged switch Lanvex cannot see) go to the
port with the fewest addresses.

Nothing here writes to NetBox. Recording a cable goes through the preview and token of
discovery/service.py.
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.core.config import Settings
from lanvex.core.errors import LanvexError
from lanvex.core.vendors import vendor
from lanvex.integrations.linux.facts import HostFacts
from lanvex.integrations.snmp.switch import SwitchFacts, SwitchNeighbor, SwitchPort
from lanvex.netbox.cables import cableable, describe_ends, far_ends, iface_type, not_cableable
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import ConnectionStatus, DiscoveredAddress, Server, ServerSnapshot
from lanvex.switches.models import Switch, SwitchMac, SwitchRead
from lanvex.switches.schemas import Endpoint, PortFinding, PortFindings

CHUNK = 40  # MAC addresses per NetBox lookup


def short(name: str | None) -> str:
    return (name or "").split(".")[0].strip().lower()


def port_key(switch_id: uuid.UUID, if_index: int) -> str:
    return hashlib.sha256(f"{switch_id}|{if_index}".encode()).hexdigest()[:20]


@dataclass
class PortMap:
    """What the switches' latest reads and address tables say, without the documentation."""

    switches: dict[uuid.UUID, Switch]
    facts: dict[uuid.UUID, SwitchFacts]
    uplinks: dict[tuple[uuid.UUID, int], uuid.UUID | None]  # port -> the switch it leads to
    by_port: dict[tuple[uuid.UUID, int], list[SwitchMac]]  # the MAC addresses placed on each port
    placed: dict[str, SwitchMac]  # MAC -> where it is (the latest entry)
    own: dict[str, uuid.UUID]  # MAC addresses of the switches themselves
    chassis: dict[str, uuid.UUID]  # LLDP chassis IDs and names of the switches

    def current(self, m: SwitchMac) -> bool:
        sw = self.switches.get(m.switch_id)
        return bool(sw and sw.last_read_at and m.last_seen >= sw.last_read_at)

    def switch_for(self, n: SwitchNeighbor) -> uuid.UUID | None:
        for k in ((n.chassis_id or "").lower(), short(n.system_name)):
            if k and k in self.chassis:
                return self.chassis[k]
        return None


async def port_map(db: AsyncSession) -> PortMap:
    switches = {s.id: s for s in (await db.scalars(select(Switch))).all()}
    facts = {
        r.switch_id: SwitchFacts.model_validate(r.facts)
        for r in (await db.scalars(select(SwitchRead))).all()
        if r.switch_id in switches
    }
    own: dict[str, uuid.UUID] = {}
    chassis: dict[str, uuid.UUID] = {}
    for sid, f in facts.items():
        for m in [f.system.bridge_mac, *(p.mac for p in f.ports)]:
            if m:
                own.setdefault(m, sid)
        for k in (
            (f.system.lldp_chassis_id or "").lower(),
            (f.system.bridge_mac or ""),
            short(f.system.lldp_system_name),
            short(f.system.name),
            short(switches[sid].name),
        ):
            if k:
                chassis.setdefault(k, sid)

    pm = PortMap(switches, facts, {}, defaultdict(list), {}, own, chassis)
    for sid, f in facts.items():
        for n in f.neighbors:
            other = pm.switch_for(n)
            if n.port is not None and other is not None and other != sid:
                pm.uplinks[(sid, n.port)] = other

    rows = list(
        (
            await db.scalars(
                select(SwitchMac)
                .order_by(SwitchMac.last_seen.desc())
                .execution_options(populate_existing=True)  # rows a read just updated
            )
        ).all()
    )
    for m in rows:
        other = own.get(m.mac)
        if other is not None and other != m.switch_id and pm.current(m):
            pm.uplinks.setdefault((m.switch_id, m.if_index), other)

    candidates: dict[str, list[SwitchMac]] = defaultdict(list)
    load: dict[tuple[uuid.UUID, int], set[str]] = defaultdict(set)
    for m in rows:
        if m.mac in own or (m.switch_id, m.if_index) in pm.uplinks or m.switch_id not in switches:
            continue
        candidates[m.mac].append(m)
        if pm.current(m):
            load[(m.switch_id, m.if_index)].add(m.mac)

    for mac, entries in candidates.items():
        now = [e for e in entries if pm.current(e)] or entries
        newest = max(e.last_seen for e in now)
        # Same read or not: what was seen last counts; among those, the port with the fewest devices.
        recent = [e for e in now if (newest - e.last_seen).total_seconds() < 3600]
        best = min(
            recent, key=lambda e: (len(load[(e.switch_id, e.if_index)]) or 1, -e.last_seen.timestamp())
        )
        pm.placed[mac] = best
        for e in recent:
            if (e.switch_id, e.if_index) == (best.switch_id, best.if_index):
                pm.by_port[(e.switch_id, e.if_index)].append(e)
    return pm


@dataclass
class Move:
    """A device now plugged into another port than at the last read."""

    mac: str
    vlan: int | None
    from_switch: uuid.UUID
    from_port: str
    to_switch: uuid.UUID
    to_port: str


def moves(before: dict[str, tuple[uuid.UUID, int, str, bool]], after: PortMap, macs: set[str]) -> list[Move]:
    """MAC addresses among `macs` that were on one port in the reads before and are on another now."""
    out = []
    for mac in sorted(macs):
        was, now = before.get(mac), after.placed.get(mac)
        if was is None or now is None or not was[3] or not after.current(now):
            continue
        if (was[0], was[1]) in after.uplinks:  # it was placed there for want of knowing better
            continue
        if (was[0], was[1]) != (now.switch_id, now.if_index):
            out.append(Move(mac, now.vlan or None, was[0], was[2], now.switch_id, now.port))
    return out


# ---- who a MAC address belongs to -------------------------------------------------------------------


@dataclass
class Identity:
    kind: str  # server, device, vm, unknown
    name: str | None = None
    interface: str | None = None
    server_id: uuid.UUID | None = None
    url: str | None = None
    nb_iface: dict[str, Any] | None = None  # the NetBox dcim interface, when documented
    addresses: list[str] = field(default_factory=list)


def _mac_of(obj: dict[str, Any]) -> set[str]:
    found = [m.get("mac_address") for m in obj.get("mac_addresses") or [] if isinstance(m, dict)]
    found += [(obj.get("primary_mac_address") or {}).get("mac_address"), obj.get("mac_address")]
    return {str(m).lower().replace("-", ":") for m in found if m}


def _is_mac(value: str | None) -> bool:
    return bool(value) and len(value or "") == 17 and (value or "").count(":") == 5


def _macs_of(neighbors: list[SwitchNeighbor], e: Endpoint) -> list[str]:
    """The MAC addresses the LLDP neighbour behind endpoint `e` announced (chassis and port)."""
    if e.via != "lldp":
        return []
    return [
        m
        for n in neighbors
        if (n.system_name or n.chassis_id) == e.name
        for m in (n.chassis_id, n.port_id)
        if m and _is_mac(m)
    ]


def _port_matches(iface: dict[str, Any], *names: str | None) -> bool:
    mine = {(iface.get("name") or "").lower(), (iface.get("label") or "").lower()} - {""}
    return bool(mine & ({(n or "").lower() for n in names} - {""}))


class PortLocator:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None) -> None:
        self.db, self.settings, self.nb = db, settings, nb
        self._ifaces: dict[int, list[dict[str, Any]]] = {}
        self._writes: dict[str, dict[str, Any]] = {}

    async def _interfaces(self, device_id: int) -> list[dict[str, Any]]:
        if device_id not in self._ifaces:
            assert self.nb is not None
            self._ifaces[device_id] = await self.nb.list_all(
                "dcim/interfaces/", {"device_id": device_id}, cap=5000
            )
        return self._ifaces[device_id]

    async def _servers(self) -> tuple[dict[str, tuple[Server, str]], dict[str, Server]]:
        """MAC -> (server, interface) from each server's latest read, and servers by short name."""
        by_mac: dict[str, tuple[Server, str]] = {}
        by_name: dict[str, Server] = {}
        order = {"ethernet": 0, "bond": 1}
        for s in (await self.db.scalars(select(Server))).all():
            by_name.setdefault(short(s.name), s)
            snap = await self.db.scalar(
                select(ServerSnapshot)
                .where(ServerSnapshot.server_id == s.id, ServerSnapshot.status == ConnectionStatus.OK)
                .order_by(ServerSnapshot.taken_at.desc())
                .limit(1)
            )
            if snap is None or not snap.facts:
                continue
            facts = HostFacts.model_validate(snap.facts)
            if facts.system.hostname:
                by_name.setdefault(short(facts.system.hostname), s)
            for i in sorted(facts.interfaces, key=lambda i: order.get(i.kind, 2)):
                if i.mac and i.kind != "loopback":
                    by_mac.setdefault(i.mac.lower(), (s, i.name))
        return by_mac, by_name

    async def _addresses(self, pm: PortMap) -> dict[str, list[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for f in pm.facts.values():
            for a in f.arp:
                out[a.mac].add(a.address)
        for mac, address in (
            await self.db.execute(
                select(DiscoveredAddress.mac, DiscoveredAddress.address).where(
                    DiscoveredAddress.mac.is_not(None)
                )
            )
        ).all():
            out[str(mac).lower()].add(address)
        return {m: sorted(a, key=lambda x: (":" in x, x))[:4] for m, a in out.items()}

    async def _documented(self, macs: list[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        """MAC -> ("device" or "vm", the NetBox interface)."""
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        if self.nb is None:
            return found
        wanted = sorted(set(macs))
        for i in range(0, len(wanted), CHUNK):
            chunk = wanted[i : i + CHUNK]
            for path, kind in (("dcim/interfaces/", "device"), ("virtualization/interfaces/", "vm")):
                try:
                    objs = await self.nb.list_all(path, {"mac_address": chunk}, cap=CHUNK * 10)
                except LanvexError:
                    continue
                for obj in objs:
                    for m in _mac_of(obj) & set(chunk):
                        found.setdefault(m, (kind, obj))
        return found

    async def identities(
        self, pm: PortMap, macs: list[str], servers: dict[str, tuple[Server, str]] | None = None
    ) -> dict[str, Identity]:
        if servers is None:
            servers, _ = await self._servers()
        addresses = await self._addresses(pm)
        documented = await self._documented(macs)
        out: dict[str, Identity] = {}
        for mac in macs:
            ident = Identity(kind="unknown", addresses=addresses.get(mac, []))
            doc = documented.get(mac)
            if doc is not None:
                kind, obj = doc
                parent = obj.get("device") or obj.get("virtual_machine") or {}
                ident.kind, ident.name, ident.interface = kind, parent.get("name"), obj.get("name")
                ident.url = self.settings.public_netbox_url(
                    parent.get("display_url") or obj.get("display_url")
                )
                if kind == "device":
                    ident.nb_iface = obj
            if mac in servers:
                s, iface = servers[mac]
                ident.kind, ident.name, ident.interface, ident.server_id = "server", s.name, iface, s.id
                if (
                    ident.nb_iface is None
                    and s.netbox_object_type == "dcim.device"
                    and s.netbox_object_id
                    and self.nb
                ):
                    try:
                        ident.nb_iface = next(
                            (i for i in await self._interfaces(s.netbox_object_id) if i.get("name") == iface),
                            None,
                        )
                    except LanvexError:
                        pass
            out[mac] = ident
        return out

    # ---- findings ---------------------------------------------------------------------------------------

    async def findings(
        self, switch_id: uuid.UUID | None = None
    ) -> tuple[PortFindings, dict[str, dict[str, Any]]]:
        """Every port with something seen on it, and for those whose cable can be recorded, the cable."""
        pm = await port_map(self.db)
        macs = sorted({m.mac for rows in pm.by_port.values() for m in rows})
        server_macs, server_names = await self._servers()
        idents = await self.identities(pm, macs, server_macs)
        nb = self.nb
        out: list[PortFinding] = []
        writes: dict[str, dict[str, Any]] = {}
        devices_by_name: dict[str, dict[str, Any]] = {}
        names = {
            short(n.system_name)
            for sid, f in pm.facts.items()
            for n in f.neighbors
            if n.system_name and (switch_id is None or sid == switch_id)
        }
        if nb is not None and names:
            try:
                for d in await nb.list_all("dcim/devices/", {"name__ie": sorted(names)}, cap=1000):
                    devices_by_name.setdefault(short(d.get("name")), d)
            except LanvexError:
                pass

        for sid, f in sorted(pm.facts.items(), key=lambda kv: pm.switches[kv[0]].name.lower()):
            if switch_id is not None and sid != switch_id:
                continue
            sw = pm.switches[sid]
            seen_at = f.collected_at
            lldp: dict[int, list[SwitchNeighbor]] = defaultdict(list)
            for n in f.neighbors:
                if n.port is not None:
                    lldp[n.port].append(n)
            indexes = sorted(
                set(lldp)
                | {i for (s2, i) in pm.by_port if s2 == sid}
                | {i for (s2, i) in pm.uplinks if s2 == sid}
            )
            mine_ifaces: list[dict[str, Any]] | None = None
            if nb is not None and sw.netbox_device_id:
                try:
                    mine_ifaces = await self._interfaces(sw.netbox_device_id)
                except LanvexError:
                    mine_ifaces = None
            for idx in indexes:
                port = f.port(idx) or SwitchPort(index=idx, name=f"ifIndex {idx}")
                finding = PortFinding(
                    key=port_key(sid, idx),
                    switch_id=sid,
                    switch_name=sw.name,
                    if_index=idx,
                    port=port.name,
                    alias=port.alias,
                    uplink=(sid, idx) in pm.uplinks,
                    endpoints=[],
                    status="not_checked",
                )
                peer_device: dict[str, Any] | None = None  # NetBox device the LLDP neighbour is
                peer_switch = pm.uplinks.get((sid, idx))
                for n in lldp.get(idx, []):
                    other = pm.switch_for(n)
                    server = server_names.get(short(n.system_name)) if n.system_name else None
                    device = devices_by_name.get(short(n.system_name)) if n.system_name else None
                    kind = "switch" if other else "server" if server else "device" if device else "unknown"
                    finding.endpoints.append(
                        Endpoint(
                            via="lldp",
                            kind=kind,  # type: ignore[arg-type]
                            name=n.system_name or n.chassis_id,
                            # A port ID that is a name says more than the free-text description; one
                            # that is a MAC address (Linux hosts) says less.
                            interface=n.port_id
                            if n.port_id and not _is_mac(n.port_id)
                            else n.port_description,
                            mac=(
                                lldp_mac := next((m for m in (n.port_id, n.chassis_id) if _is_mac(m)), None)
                            ),
                            vendor=vendor(lldp_mac),
                            addresses=n.management_addresses,
                            server_id=server.id if server else None,
                            switch_id=other,
                            netbox_url=self.settings.public_netbox_url((device or {}).get("display_url")),
                            last_seen=seen_at,
                            current=True,
                        )
                    )
                    if other is not None and pm.switches[other].netbox_device_id and nb is not None:
                        try:
                            peer_device = await nb.get(f"dcim/devices/{pm.switches[other].netbox_device_id}/")
                        except LanvexError:
                            peer_device = device
                    elif (
                        server is not None
                        and server.netbox_object_type == "dcim.device"
                        and server.netbox_object_id
                    ):
                        peer_device = {"id": server.netbox_object_id, "name": server.name}
                    else:
                        peer_device = peer_device or device
                if peer_switch is not None and not lldp.get(idx):
                    other_sw = pm.switches[peer_switch]
                    finding.endpoints.append(
                        Endpoint(
                            via="mac",
                            kind="switch",
                            name=other_sw.name,
                            switch_id=peer_switch,
                            last_seen=seen_at,
                            current=True,
                        )
                    )
                placed = list({m.mac: m for m in reversed(pm.by_port.get((sid, idx), []))}.values())[::-1]
                announced = {
                    m: e for e in finding.endpoints for m in (e.mac, *_macs_of(lldp.get(idx, []), e)) if m
                }
                for m in placed:
                    ident = idents.get(m.mac, Identity(kind="unknown"))
                    same = announced.get(m.mac)
                    if same is not None:  # the device that announced itself over LLDP
                        same.vlan = same.vlan or m.vlan or None
                        same.addresses = same.addresses or ident.addresses
                        same.server_id = same.server_id or ident.server_id
                        same.netbox_url = same.netbox_url or ident.url
                        continue
                    finding.endpoints.append(
                        Endpoint(
                            via="mac",
                            kind=ident.kind,  # type: ignore[arg-type]
                            name=ident.name,
                            interface=ident.interface,
                            mac=m.mac,
                            vendor=vendor(m.mac),
                            vlan=m.vlan or None,
                            addresses=ident.addresses,
                            server_id=ident.server_id,
                            netbox_url=ident.url,
                            last_seen=m.last_seen,
                            current=pm.current(m),
                        )
                    )
                uplink_device = pm.switches[peer_switch].netbox_device_id if peer_switch else None
                await self._compare(
                    finding,
                    sw,
                    port,
                    lldp.get(idx, []),
                    peer_device,
                    uplink_device,
                    placed,
                    idents,
                    mine_ifaces,
                    [p.name for p in f.ports if p.lag == idx],
                )
                if finding.key in self._writes:
                    writes[finding.key] = self._writes.pop(finding.key) | {
                        "description": f"Seen by Lanvex on {sw.name} / {port.name} on {seen_at:%Y-%m-%d}"[
                            :200
                        ]
                    }
                out.append(finding)
        return PortFindings(ports=out, switches_read=len(pm.facts), netbox_checked=nb is not None), writes

    async def _compare(
        self,
        finding: PortFinding,
        sw: Switch,
        port: SwitchPort,
        neighbors: list[SwitchNeighbor],
        peer_device: dict[str, Any] | None,
        uplink_device: int | None,
        placed: list[SwitchMac],
        idents: dict[str, Identity],
        mine_ifaces: list[dict[str, Any]] | None,
        members: list[str],
    ) -> None:
        nb = self.nb
        if nb is None:
            return
        if not sw.netbox_device_id:
            finding.status = "not_linked"
            return
        finding.device_id = sw.netbox_device_id
        if mine_ifaces is None:
            finding.status, finding.problem = (
                "not_checked",
                f"Could not read the switch's interfaces in {nb.name}.",
            )
            return
        mine = next((i for i in mine_ifaces if _port_matches(i, port.name, port.description)), None)
        if mine is None:
            finding.status = "no_port"
            finding.problem = f"The switch's device in {nb.name} has no interface {port.name}."
            return
        finding.documented = describe_ends(mine)

        peer: dict[str, Any] | None = None
        if peer_device is not None and neighbors:
            n = neighbors[0]
            try:
                peer = next(
                    (
                        i
                        for i in await self._interfaces(peer_device["id"])
                        if _port_matches(i, n.port_id, n.port_description)
                    ),
                    None,
                )
            except LanvexError:
                peer = None
            if peer is None:
                finding.status = "unidentified"
                finding.problem = f"{peer_device.get('name')} has no interface {n.port_id or n.port_description} in {nb.name}."
                return
        elif neighbors and peer_device is None and not placed:
            n = neighbors[0]
            finding.status = "unidentified"
            finding.problem = f"No device named {n.system_name or n.chassis_id} in {nb.name}."
            return
        if peer is None and finding.uplink:
            if mine.get("cable"):
                ends = far_ends(mine)
                leads_there = uplink_device is None or any(
                    (e.get("device") or {}).get("id") == uplink_device for e in ends
                )
                finding.status = "documented" if leads_there else "different"
            else:
                finding.status = "unidentified"
                finding.problem = (
                    "Which of its ports this leads to is not known: LLDP is off on one of the switches."
                )
            return
        if peer is None:
            documented: dict[Any, dict[str, Any]] = {}
            for m in placed:
                ident = idents.get(m.mac)
                if ident and ident.nb_iface is not None:
                    dev = (ident.nb_iface.get("device") or {}).get("id")
                    documented.setdefault(dev, ident.nb_iface)
            if len(documented) > 1:
                names = sorted(str((i.get("device") or {}).get("name")) for i in documented.values())
                finding.status = "shared"
                finding.problem = (
                    f"{len(documented)} documented devices on one port ({', '.join(names)}): "
                    "something Lanvex cannot see is in between."
                )
                return
            if len(documented) == 1:
                peer = next(iter(documented.values()))
            else:
                kinds = {idents[m.mac].kind for m in placed if m.mac in idents}
                finding.status = "virtual" if kinds == {"vm"} else "unidentified"
                return

        peer_name = f"{(peer.get('device') or {}).get('name') or (peer_device or {}).get('name')} / {peer.get('name')}"
        finding.peer = peer_name
        finding.peer_device_id = (peer.get("device") or {}).get("id") or (peer_device or {}).get("id")
        finding.peer_port = peer.get("name")
        if mine.get("cable"):
            match = any(e.get("id") == peer.get("id") for e in far_ends(mine))
            finding.status = "documented" if match else "different"
            return
        if peer.get("cable"):
            finding.status = "different"
            finding.documented = None
            finding.problem = f"{nb.name} has a cable from {peer_name} to {describe_ends(peer)}."
            return
        finding.status = "undocumented"
        where = f"{sw.name} / {port.name}"
        if port.kind == "lag" or iface_type(mine)[0] == "lag":
            finding.problem = (
                f"{port.name} is a link aggregation: record the cables of its member ports"
                + (f" ({', '.join(members)})" if members else "")
                + f" in {nb.name}."
            )
        elif not cableable(mine):
            finding.problem = not_cableable(where, mine, nb.name)
        elif not cableable(peer):
            finding.problem = not_cableable(peer_name, peer, nb.name)
        else:
            self._writes[finding.key] = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": mine["id"]}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": peer["id"]}],
                "status": "connected",
            }
