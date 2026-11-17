"""Network topology from documented cables.

Devices and circuits are nodes; cables are links. Everything here is documented state from NetBox:
a cable in the map is a cable someone recorded, not one Lanvex has seen. Lanvex servers linked to a
device are marked, so the map shows which devices Lanvex can also read. Connections switches see on their
ports that no cable records are listed apart (`seen`), never mixed with the documented cables.
"""

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from sqlalchemy import select

from lanvex.core.config import Settings, get_settings
from lanvex.core.errors import LanvexError
from lanvex.core.logging import get_logger
from lanvex.deps import DB, CurrentUser, NetBox, SettingsDep
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel, Choice, Ref, choice, ref
from lanvex.servers.models import Server
from lanvex.switches.models import Switch
from lanvex.switches.ports import PortLocator

router = APIRouter(tags=["topology"])
log = get_logger("lanvex.topology")

# Beyond this many devices or cables in one view, Lanvex asks for a narrower view (one site).
DEVICE_CAP = 2000
CABLE_CAP = 5000

NodeKind = Literal["device", "circuit"]
LinkKind = Literal["data", "patch", "console", "power", "circuit"]


class TopologyNode(ApiModel):
    id: str  # "device:12" or "circuit:3": unique across kinds
    kind: NodeKind
    object_id: int
    name: str
    role: Ref | None = None
    color: str | None = None  # the device role's colour in NetBox, as "#rrggbb"
    site: Ref | None = None
    status: Choice | None = None
    device_type: str | None = None
    primary_ip: str | None = None
    provider: str | None = None
    # True when the node is outside the requested view (another site) but a cable leads to it.
    external: bool = False
    server_id: uuid.UUID | None = None
    switch_id: uuid.UUID | None = None
    netbox_url: str | None = None


class TopologyLink(ApiModel):
    id: str  # the cable id, with a suffix when one cable joins more than two nodes
    cable_id: int
    a: str
    b: str
    a_ports: list[str]
    b_ports: list[str]
    kind: LinkKind
    status: Choice | None
    type: str | None
    label: str
    color: str | None
    netbox_url: str | None


class SeenLink(ApiModel):
    """A connection a switch sees on one of its ports that the cables do not show (switches/ports.py)."""

    id: str
    a: str  # the switch's node
    b: str
    a_port: str
    b_port: str | None
    status: Literal["undocumented", "different"]


class Topology(ApiModel):
    nodes: list[TopologyNode]
    links: list[TopologyLink]
    seen: list[SeenLink] = []
    sites: list[Ref]
    site_id: int | None
    truncated: bool
    skipped_cables: int  # cables to things that are not devices or circuits (power feeds, …)


def _color(value: str | None) -> str | None:
    value = (value or "").strip().lstrip("#")
    return (
        f"#{value.lower()}" if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value) else None
    )


def _ip(value: dict[str, Any] | None) -> str | None:
    return value["address"].split("/")[0] if value and value.get("address") else None


def _device_node(raw: dict[str, Any], colors: dict[int, str | None]) -> TopologyNode:
    role = raw.get("role") or {}
    dt = raw.get("device_type") or {}
    return TopologyNode(
        id=f"device:{raw['id']}",
        kind="device",
        object_id=raw["id"],
        name=raw.get("name") or f"Unnamed device {raw['id']}",
        role=ref(raw.get("role")),
        color=colors.get(role.get("id")) if role else None,
        site=ref(raw.get("site")),
        status=choice(raw.get("status")),
        device_type=dt.get("model") or dt.get("display"),
        primary_ip=_ip(raw.get("primary_ip")),
        netbox_url=get_settings().public_netbox_url(raw.get("display_url")),
    )


def _endpoint(term: dict[str, Any]) -> tuple[str, TopologyNode, str] | None:
    """Which node a cable termination belongs to, the node as far as the termination tells, and the port."""
    obj = term.get("object") or {}
    kind = term.get("object_type") or ""
    if kind == "circuits.circuittermination":
        circuit = obj.get("circuit") or {}
        if not circuit.get("id"):
            return None
        node = TopologyNode(
            id=f"circuit:{circuit['id']}",
            kind="circuit",
            object_id=circuit["id"],
            name=circuit.get("cid") or circuit.get("display") or f"Circuit {circuit['id']}",
            provider=(circuit.get("provider") or {}).get("name"),
        )
        return node.id, node, f"Side {obj.get('term_side') or '?'}"
    device = obj.get("device") or {}
    if not device.get("id"):
        return None  # power feeds and other terminations without a device
    node = TopologyNode(
        id=f"device:{device['id']}",
        kind="device",
        object_id=device["id"],
        name=device.get("name") or device.get("display") or f"Device {device['id']}",
        external=True,
    )
    return node.id, node, obj.get("name") or obj.get("display") or ""


def _link_kind(types: set[str]) -> LinkKind:
    if any(t.startswith("circuits.") for t in types):
        return "circuit"
    if types & {"dcim.consoleport", "dcim.consoleserverport"}:
        return "console"
    if types & {"dcim.powerport", "dcim.poweroutlet", "dcim.powerfeed"}:
        return "power"
    if types & {"dcim.frontport", "dcim.rearport"}:
        return "patch"
    return "data"


def build(
    devices: list[dict[str, Any]],
    cables: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    servers: dict[int, uuid.UUID],
) -> tuple[list[TopologyNode], list[TopologyLink], int]:
    colors = {r["id"]: _color(r.get("color")) for r in roles}
    nodes: dict[str, TopologyNode] = {}
    for raw in devices:
        node = _device_node(raw, colors)
        nodes[node.id] = node
    links: list[TopologyLink] = []
    skipped = 0
    for cable in cables:
        sides: list[dict[str, list[str]]] = []
        for key in ("a_terminations", "b_terminations"):
            ports: dict[str, list[str]] = {}
            for term in cable.get(key) or []:
                found = _endpoint(term)
                if found is None:
                    continue
                node_id, node, port = found
                nodes.setdefault(node_id, node)
                ports.setdefault(node_id, []).append(port)
            sides.append(ports)
        if not sides[0] or not sides[1]:
            skipped += 1
            continue
        types = {
            t.get("object_type") or ""
            for k in ("a_terminations", "b_terminations")
            for t in cable.get(k) or []
        }
        pairs = [(a, b) for a in sides[0] for b in sides[1]]
        for n, (a, b) in enumerate(pairs):
            links.append(
                TopologyLink(
                    id=str(cable["id"]) if len(pairs) == 1 else f"{cable['id']}-{n}",
                    cable_id=cable["id"],
                    a=a,
                    b=b,
                    a_ports=sides[0][a],
                    b_ports=sides[1][b],
                    kind=_link_kind(types),
                    status=choice(cable.get("status")),
                    type=(cable.get("type") or None),
                    label=cable.get("label") or "",
                    color=_color(cable.get("color")),
                    netbox_url=get_settings().public_netbox_url(cable.get("display_url")),
                )
            )
    for node in nodes.values():
        if node.kind == "device":
            node.server_id = servers.get(node.object_id)
    return sorted(nodes.values(), key=lambda n: (n.kind, n.name.lower())), links, skipped


async def _seen(db: DB, settings: Settings, nb: NetBoxClient, nodes: list[TopologyNode]) -> list[SeenLink]:
    """Connections switches see that are not cabled as seen; only between devices on the map."""
    ids = {n.id for n in nodes}
    try:
        found, _ = await PortLocator(db, settings, nb).findings()
    except LanvexError as e:
        log.warning("topology_seen_skipped", error=e.message)
        return []
    out = []
    for p in found.ports:
        if p.status not in ("undocumented", "different") or not p.device_id or not p.peer_device_id:
            continue
        a, b = f"device:{p.device_id}", f"device:{p.peer_device_id}"
        if a in ids and b in ids and a != b:
            out.append(
                SeenLink(id=f"seen-{p.key}", a=a, b=b, a_port=p.port, b_port=p.peer_port, status=p.status)
            )  # type: ignore[arg-type]
    return out


@router.get("/topology", response_model=Topology)
async def get_topology(
    nb: NetBox, db: DB, settings: SettingsDep, _: CurrentUser, site_id: int | None = None
) -> Topology:
    scope = {"site_id": site_id} if site_id else {}
    devices, cables, roles, sites = await asyncio.gather(
        nb.list_all(
            "dcim/devices/", {**scope, "omit": "config_context", "ordering": "name"}, cap=DEVICE_CAP + 1
        ),
        nb.list_all("dcim/cables/", scope, cap=CABLE_CAP + 1),
        nb.list_all("dcim/device-roles/", {}, cap=1000),
        nb.list_all("dcim/sites/", {"brief": True, "ordering": "name"}, cap=1000),
    )
    truncated = len(devices) > DEVICE_CAP or len(cables) > CABLE_CAP
    rows = await db.execute(
        select(Server.netbox_object_id, Server.id).where(Server.netbox_object_type == "dcim.device")
    )
    servers = {object_id: server_id for object_id, server_id in rows.all() if object_id is not None}
    nodes, links, skipped = build(devices[:DEVICE_CAP], cables[:CABLE_CAP], roles, servers)
    switches = dict(
        (
            await db.execute(
                select(Switch.netbox_device_id, Switch.id).where(Switch.netbox_device_id.is_not(None))
            )
        ).all()
    )
    for node in nodes:
        if node.kind == "device":
            node.switch_id = switches.get(node.object_id)
    return Topology(
        nodes=nodes,
        links=links,
        seen=await _seen(db, settings, nb, nodes) if switches else [],
        sites=[r for r in (ref(s) for s in sites) if r],
        site_id=site_id,
        truncated=truncated,
        skipped_cables=skipped,
    )
