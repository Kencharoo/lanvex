"""Discovery: what servers and switches see around them, compared with what NetBox documents.

From servers, read over SSH as part of every server read (no extra access):
- the neighbour table (ARP / IPv6 neighbour discovery): hosts each server talked to recently. Kept in
  `discovered_addresses` with first and last time seen, so hosts stay listed between reads.
- LLDP: what the device on the other end of each interface announces about itself. Taken from each
  server's latest successful read, and compared with the cable NetBox documents on that interface.
From switches, read over SNMP: what is connected to each port (switches/ports.py).

Nothing here writes to NetBox by itself. An administrator picks what to record, sees a preview of the
exact NetBox writes, and only that plan is applied (the same token check as servers/record.py).
"""

import hashlib
import ipaddress
import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.core.config import Settings
from lanvex.core.errors import Conflict, LanvexError, ValidationFailed
from lanvex.core.vendors import vendor
from lanvex.integrations.linux.facts import HostFacts, LldpNeighbor
from lanvex.netbox.cables import cableable as _cableable
from lanvex.netbox.cables import describe_ends
from lanvex.netbox.cables import not_cableable as _not_cableable
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel
from lanvex.servers.models import ConnectionStatus, DiscoveredAddress, Server, ServerSnapshot
from lanvex.servers.record import RecordAction
from lanvex.switches.ports import PortLocator, PortMap, port_map
from lanvex.switches.schemas import PortFindings

CHUNK = 100  # addresses per NetBox lookup


# ---- schemas ------------------------------------------------------------------------------------


class SeenBy(ApiModel):
    server_id: uuid.UUID
    server_name: str
    interface: str
    mac: str | None
    last_seen: datetime


class NetBoxIP(ApiModel):
    id: int
    address: str
    assigned_to: str | None  # "device / interface", or None when unassigned
    url: str | None


class SwitchPortRef(ApiModel):
    """The switch port a host's MAC address was learned on."""

    switch_id: uuid.UUID
    switch_name: str
    port: str
    vlan: int | None = None
    last_seen: datetime
    current: bool


class DiscoveredHost(ApiModel):
    address: str
    family: int
    mac: str | None
    vendor: str | None = None  # who made it, from its MAC address
    router: bool
    first_seen: datetime
    last_seen: datetime
    seen_by: list[SeenBy]
    server: str | None  # the Lanvex server with this address, if any
    server_id: uuid.UUID | None = None
    netbox: NetBoxIP | None
    record_as: str | None  # the address with the prefix length it would be recorded with
    switch_port: SwitchPortRef | None = None


class DiscoveredHosts(ApiModel):
    hosts: list[DiscoveredHost]
    total: int
    undocumented: int
    netbox_checked: bool


LinkStatus = Literal["documented", "different", "undocumented", "several", "not_linked", "virtual"]


class DiscoveredLink(ApiModel):
    key: str
    server_id: uuid.UUID
    server_name: str
    interface: str
    neighbor: LldpNeighbor
    seen_at: datetime
    status: LinkStatus
    documented: str | None = None  # what NetBox has at the other end of this interface's cable
    peer: str | None = None  # the NetBox device / interface the neighbour matches
    problem: str | None = None  # why a cable cannot be recorded


class DiscoveredLinks(ApiModel):
    links: list[DiscoveredLink]
    servers_read: int
    servers_without_lldp: list[str]


class DiscoveryRecordRequest(ApiModel):
    items: list[str] = Field(min_length=1, max_length=500)  # addresses, or link keys
    token: str | None = None


class DiscoveryPlan(ApiModel):
    actions: list[RecordAction]
    token: str
    applied: bool = False
    done: list[RecordAction] = Field(default_factory=list)
    failed: RecordAction | None = None
    error: str | None = None


# ---- helpers ------------------------------------------------------------------------------------


def _listed(ip: str) -> bool:
    """Link-local addresses are never documented; they would only be noise."""
    a = ipaddress.ip_address(ip)
    return not (a.is_link_local or a.is_loopback or a.is_multicast or a.is_unspecified)


def _short(name: str | None) -> str:
    return (name or "").split(".")[0].lower()


def _token(actions: list[RecordAction], writes: list[Any]) -> str:
    payload = [[a.model_dump(mode="json"), w] for a, w in zip(actions, writes, strict=True)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _link_key(server_id: uuid.UUID, n: LldpNeighbor) -> str:
    raw = f"{server_id}|{n.interface}|{n.chassis_id}|{n.port_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def stale(netbox_name: str) -> Conflict:
    return Conflict(
        f"What the servers see or what {netbox_name} documents changed since the preview. "
        "Review the new preview."
    )


class DiscoveryService:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None) -> None:
        self.db, self.settings, self.nb = db, settings, nb

    async def _latest_facts(self) -> list[tuple[Server, ServerSnapshot, HostFacts]]:
        """Each server's latest successful read."""
        out = []
        for s in (await self.db.scalars(select(Server).order_by(Server.name))).all():
            snap = await self.db.scalar(
                select(ServerSnapshot)
                .where(ServerSnapshot.server_id == s.id, ServerSnapshot.status == ConnectionStatus.OK)
                .order_by(ServerSnapshot.taken_at.desc())
                .limit(1)
            )
            if snap is not None and snap.facts:
                out.append((s, snap, HostFacts.model_validate(snap.facts)))
        return out

    # ---- addresses --------------------------------------------------------------------------------

    async def hosts(self, *, undocumented: bool = False) -> DiscoveredHosts:
        rows = (
            await self.db.execute(
                select(DiscoveredAddress, Server.name)
                .join(Server, Server.id == DiscoveredAddress.server_id)
                .order_by(DiscoveredAddress.last_seen.desc())
            )
        ).all()
        by_address: dict[str, list[tuple[DiscoveredAddress, str]]] = defaultdict(list)
        for d, name in rows:
            if _listed(d.address):
                by_address[d.address].append((d, name))

        servers = list((await self.db.scalars(select(Server))).all())
        own = {str(s.address): s for s in servers}
        facts = {s.id: f for s, _, f in await self._latest_facts()}
        ports = await port_map(self.db)

        recorded: dict[str, NetBoxIP] = {}
        checked = False
        if self.nb is not None and by_address:
            addresses = sorted(by_address)
            for i in range(0, len(addresses), CHUNK):
                for ip in await self.nb.list_all(
                    "ipam/ip-addresses/", {"address": addresses[i : i + CHUNK]}, cap=CHUNK * 4
                ):
                    host = str(ipaddress.ip_interface(ip["address"]).ip)
                    obj = ip.get("assigned_object") or {}
                    parent = obj.get("device") or obj.get("virtual_machine") or {}
                    recorded.setdefault(
                        host,
                        NetBoxIP(
                            id=ip["id"],
                            address=ip["address"],
                            assigned_to=f"{parent.get('name')} / {obj.get('name')}" if parent else None,
                            url=self.settings.public_netbox_url(ip.get("display_url")),
                        ),
                    )
            checked = True

        hosts: list[DiscoveredHost] = []
        for address, seen in by_address.items():
            newest = seen[0][0]
            hosts.append(
                DiscoveredHost(
                    address=address,
                    family=newest.family,
                    mac=newest.mac,
                    vendor=vendor(newest.mac),
                    router=any(d.router for d, _ in seen),
                    first_seen=min(d.first_seen for d, _ in seen),
                    last_seen=newest.last_seen,
                    seen_by=[
                        SeenBy(
                            server_id=d.server_id,
                            server_name=name,
                            interface=d.interface,
                            mac=d.mac,
                            last_seen=d.last_seen,
                        )
                        for d, name in seen
                    ],
                    server=own[address].name if address in own else None,
                    server_id=own[address].id if address in own else None,
                    netbox=recorded.get(address),
                    record_as=_record_as(address, [(facts.get(d.server_id), d.interface) for d, _ in seen]),
                    switch_port=_switch_port(ports, newest.mac),
                )
            )
        hosts.sort(key=lambda h: (h.netbox is not None, h.family, ipaddress.ip_address(h.address)))
        missing = sum(1 for h in hosts if h.netbox is None)
        return DiscoveredHosts(
            hosts=[h for h in hosts if h.netbox is None] if undocumented else hosts,
            total=len(hosts),
            undocumented=missing if checked else 0,
            netbox_checked=checked,
        )

    async def plan_hosts(self, addresses: list[str]) -> tuple[DiscoveryPlan, list[dict[str, Any]]]:
        nb = self.netbox_client()
        wanted = set(addresses)
        found = {h.address: h for h in (await self.hosts()).hosts if h.address in wanted}
        linked = {s.id: bool(s.netbox_object_id) for s in (await self.db.scalars(select(Server))).all()}
        actions: list[RecordAction] = []
        writes: list[dict[str, Any]] = []
        for a in addresses:
            h = found.get(a)
            if h is None:
                actions.append(
                    RecordAction(
                        address=a,
                        kind="create",
                        text=f"Record {a}",
                        problem="No server has seen it recently.",
                    )
                )
                writes.append({})
                continue
            if h.netbox is not None:
                actions.append(
                    RecordAction(
                        address=a,
                        kind="create",
                        text=f"Record {a}",
                        problem=f"Already recorded in {nb.name} as {h.netbox.address}"
                        + (f" on {h.netbox.assigned_to}." if h.netbox.assigned_to else "."),
                    )
                )
                writes.append({})
                continue
            if h.server_id is not None and linked.get(h.server_id):
                actions.append(
                    RecordAction(
                        address=a,
                        kind="create",
                        text=f"Record {a}",
                        problem=f"It is {h.server}'s own address: record it from that server's page, where it is "
                        "assigned to the right interface.",
                    )
                )
                writes.append({})
                continue
            first = h.seen_by[0]
            where = ", ".join(f"{s.server_name} ({s.interface})" for s in h.seen_by[:3])
            body = {
                "address": h.record_as,
                "status": "active",
                "description": f"Seen by {where} on {h.last_seen:%Y-%m-%d}"[:200],
            }
            mac = f", MAC {first.mac}" if first.mac else ""
            actions.append(
                RecordAction(
                    address=a,
                    kind="create",
                    text=f"Record {h.record_as} (unassigned{mac}), seen by {where}",
                )
            )
            writes.append(body)
        return DiscoveryPlan(actions=actions, token=_token(actions, writes)), writes

    # ---- LLDP links -------------------------------------------------------------------------------

    async def links(self) -> DiscoveredLinks:
        return (await self._links())[0]

    async def _links(self) -> tuple[DiscoveredLinks, dict[str, dict[str, Any]]]:
        """The links, and for those that can be recorded, the cable NetBox would get."""
        latest = await self._latest_facts()
        out: list[DiscoveredLink] = []
        writes: dict[str, dict[str, Any]] = {}
        names: set[str] = set()
        for _, _, f in latest:
            for n in f.lldp:
                if n.system_name:
                    names |= {n.system_name, _short(n.system_name)}

        # NetBox: each linked device's interfaces, and the devices the neighbours name.
        nb = self.nb
        local_ifaces: dict[int, dict[str, dict[str, Any]]] = {}
        devices_by_name: dict[str, dict[str, Any]] = {}
        if nb is not None:
            for s, _, _ in latest:
                if s.netbox_object_type == "dcim.device" and s.netbox_object_id:
                    local_ifaces[s.netbox_object_id] = {
                        i["name"]: i
                        for i in await nb.list_all(
                            "dcim/interfaces/", {"device_id": s.netbox_object_id}, cap=5000
                        )
                    }
            if names:
                for d in await nb.list_all("dcim/devices/", {"name__ie": sorted(names)}, cap=1000):
                    devices_by_name.setdefault(_short(d.get("name")), d)
        peer_ifaces: dict[int, list[dict[str, Any]]] = {}

        async def interfaces_of(device_id: int) -> list[dict[str, Any]]:
            if device_id not in peer_ifaces:
                assert nb is not None
                peer_ifaces[device_id] = await nb.list_all(
                    "dcim/interfaces/", {"device_id": device_id}, cap=5000
                )
            return peer_ifaces[device_id]

        without: list[str] = []
        for s, snap, f in latest:
            if not f.lldp:
                without.append(s.name)
                continue
            per_iface: dict[str, int] = defaultdict(int)
            for n in f.lldp:
                per_iface[n.interface] += 1
            seen_at = snap.confirmed_at or snap.taken_at
            for n in f.lldp:
                link = DiscoveredLink(
                    key=_link_key(s.id, n),
                    server_id=s.id,
                    server_name=s.name,
                    interface=n.interface,
                    neighbor=n,
                    seen_at=seen_at,
                    status="undocumented",
                )
                out.append(link)
                if nb is None or not s.netbox_object_id:
                    link.status = "not_linked"
                    continue
                if s.netbox_object_type != "dcim.device":
                    link.status = "virtual"
                    continue
                mine = local_ifaces.get(s.netbox_object_id, {}).get(n.interface)
                if mine is not None and mine.get("cable"):
                    ends = mine.get("connected_endpoints") or mine.get("link_peers") or []
                    link.documented = describe_ends(mine)
                    match = any(
                        _short((e.get("device") or {}).get("name")) == _short(n.system_name)
                        and _port_matches(e, n)
                        for e in ends
                    )
                    link.status = "documented" if match else "different"
                    continue
                if per_iface[n.interface] > 1:
                    link.status = "several"
                    link.problem = (
                        f"{n.interface} sees {per_iface[n.interface]} devices over LLDP, so it is not connected "
                        "straight to one of them (a switch that does not announce itself, or a virtual network)."
                    )
                    continue
                if mine is None:
                    link.problem = f"{s.name}'s device in {nb.name} has no interface {n.interface}. Record the server's addresses first, or add it."
                    continue
                if not _cableable(mine):
                    link.problem = _not_cableable(f"{s.name} / {n.interface}", mine, nb.name)
                    continue
                device = devices_by_name.get(_short(n.system_name))
                if device is None:
                    link.problem = f"No device named {n.system_name or n.chassis_id} in {nb.name}."
                    continue
                port = next((i for i in await interfaces_of(device["id"]) if _port_matches(i, n)), None)
                if port is None:
                    link.problem = f"{device['name']} has no interface named {n.port_id} or {n.port_description} in {nb.name}."
                    continue
                link.peer = f"{device['name']} / {port['name']}"
                if not _cableable(port):
                    link.problem = _not_cableable(link.peer, port, nb.name)
                    continue
                if port.get("cable"):
                    link.problem = f"{device['name']} / {port['name']} already has a cable in {nb.name}."
                    continue
                writes[link.key] = {
                    "a_terminations": [{"object_type": "dcim.interface", "object_id": mine["id"]}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": port["id"]}],
                    "status": "connected",
                    "description": f"Seen over LLDP by {s.name} on {seen_at:%Y-%m-%d}"[:200],
                }
        return DiscoveredLinks(links=out, servers_read=len(latest), servers_without_lldp=without), writes

    async def plan_links(self, keys: list[str]) -> tuple[DiscoveryPlan, list[dict[str, Any]]]:
        nb = self.netbox_client()
        found, cables = await self._links()
        by_key = {link.key: link for link in found.links}
        actions: list[RecordAction] = []
        writes: list[dict[str, Any]] = []
        for k in keys:
            link = by_key.get(k)
            if link is None:
                actions.append(
                    RecordAction(
                        address=None, kind="create", text="Record a cable", problem="No longer seen."
                    )
                )
                writes.append({})
                continue
            text = f"Record a cable {link.server_name} / {link.interface} ↔ {link.peer or link.neighbor.system_name}"
            write = cables.get(k)
            problem = link.problem
            if link.status == "documented":
                problem = f"Already documented in {nb.name}."
            elif link.status == "different":
                problem = (
                    f"{nb.name} documents a cable to {link.documented}. Change it there if that is wrong."
                )
            elif link.status in ("not_linked", "virtual") or write is None:
                problem = problem or (
                    f"Link {link.server_name} to its {nb.name} device first."
                    if link.status == "not_linked"
                    else "Virtual machines have no cables."
                )
            actions.append(RecordAction(address=None, kind="create", text=text, problem=problem))
            writes.append(write if not problem and write else {})
        return DiscoveryPlan(actions=actions, token=_token(actions, writes)), writes

    # ---- switch ports -------------------------------------------------------------------------------

    async def ports(self) -> PortFindings:
        return (await PortLocator(self.db, self.settings, self.nb).findings())[0]

    async def plan_ports(self, keys: list[str]) -> tuple[DiscoveryPlan, list[dict[str, Any]]]:
        nb = self.netbox_client()
        found, cables = await PortLocator(self.db, self.settings, nb).findings()
        by_key = {p.key: p for p in found.ports}
        actions: list[RecordAction] = []
        writes: list[dict[str, Any]] = []
        for k in keys:
            p = by_key.get(k)
            if p is None:
                actions.append(
                    RecordAction(
                        address=None, kind="create", text="Record a cable", problem="No longer seen."
                    )
                )
                writes.append({})
                continue
            text = f"Record a cable {p.switch_name} / {p.port} ↔ {p.peer or 'unknown'}"
            write = cables.get(k)
            problem = p.problem
            if p.status == "documented":
                problem = f"Already documented in {nb.name}."
            elif p.status == "different":
                problem = (
                    problem
                    or f"{nb.name} documents a cable to {p.documented}. Change it there if that is wrong."
                )
            elif p.status == "not_linked":
                problem = f"Link {p.switch_name} to its device in {nb.name} first."
            elif write is None:
                problem = problem or "What is connected is not documented well enough to record a cable."
            actions.append(RecordAction(address=None, kind="create", text=text, problem=problem))
            writes.append(write if not problem and write else {})
        return DiscoveryPlan(actions=actions, token=_token(actions, writes)), writes

    # ---- applying ----------------------------------------------------------------------------------

    async def apply(self, plan: DiscoveryPlan, writes: list[dict[str, Any]], path: str) -> DiscoveryPlan:
        nb = self.netbox_client()
        plan.applied = True
        for action, body in zip(plan.actions, writes, strict=True):
            if action.problem or not body:
                continue
            try:
                await nb.post(path, body)
            except LanvexError as e:
                plan.failed, plan.error = action, e.message
                break
            plan.done.append(action)
        return plan

    def netbox_client(self) -> NetBoxClient:
        if self.nb is None:
            raise ValidationFailed("Recording needs a configured NetBox connection.")
        return self.nb


def _port_matches(iface: dict[str, Any], n: LldpNeighbor) -> bool:
    names = {(iface.get("name") or "").lower(), (iface.get("label") or "").lower()} - {""}
    return bool(names & {(n.port_id or "").lower(), (n.port_description or "").lower()} - {""})


def _switch_port(ports: PortMap, mac: str | None) -> SwitchPortRef | None:
    m = ports.placed.get((mac or "").lower())
    if m is None or m.switch_id not in ports.switches:
        return None
    return SwitchPortRef(
        switch_id=m.switch_id,
        switch_name=ports.switches[m.switch_id].name,
        port=m.port,
        vlan=m.vlan or None,
        last_seen=m.last_seen,
        current=ports.current(m),
    )


def _record_as(address: str, seen: list[tuple[HostFacts | None, str]]) -> str:
    """The prefix length of the network the host was seen on: that of the observing interface's
    address in the same subnet; a host address (/32, /128) when none contains it."""
    ip = ipaddress.ip_address(address)
    for facts, iface in seen:
        if facts is None:
            continue
        for i in facts.interfaces:
            if i.name != iface:
                continue
            for a in i.addresses:
                net = ipaddress.ip_interface(a.address).network
                if net.version == ip.version and ip in net and net.prefixlen < net.max_prefixlen:
                    return f"{ip}/{net.prefixlen}"
    return f"{ip}/{ip.max_prefixlen}"
