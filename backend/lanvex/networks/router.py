import uuid
from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import Field, model_validator

from lanvex.changes.schemas import LEASE_RE, AccessSettings, ChangeDetail
from lanvex.changes.service import ChangeService, to_detail
from lanvex.core.errors import Conflict
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.netbox.client import NetBoxClient
from lanvex.networks.schemas import FixedIn, ImportReport, NetworksView, NewNetwork
from lanvex.networks.service import DEFAULT_LEASE, NetworksService
from lanvex.schemas import ApiModel
from lanvex.servers.service import ServerService

router = APIRouter(tags=["networks"])


def _svc(request: Request, db: DB, settings: SettingsDep) -> tuple[NetworksService, ServerService]:
    nb: NetBoxClient | None = request.app.state.netbox
    servers = ServerService(db, settings, nb)
    return NetworksService(servers, nb), servers


class PoolIn(ApiModel):
    """A network's DHCP pool; null start and end remove it."""

    start: str | None = None
    end: str | None = None
    lease: str = Field(default=DEFAULT_LEASE)

    @model_validator(mode="after")
    def _check(self) -> "PoolIn":
        if (self.start is None) != (self.end is None):
            raise ValueError("Give the pool's first and last address, or neither.")
        if not LEASE_RE.match(self.lease):
            raise ValueError("A lease time like 12h, 30m, 2d or infinite.")
        return self


@router.get("/servers/{server_id}/networks", response_model=NetworksView)
async def networks(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> NetworksView:
    """The server's networks as Lanvex Core documents them, next to what the server has."""
    svc, servers = _svc(request, db, settings)
    return await svc.view(await servers.get(server_id))


@router.post(
    "/servers/{server_id}/networks", response_model=NetworksView, status_code=status.HTTP_201_CREATED
)
async def add_network(
    server_id: uuid.UUID,
    body: NewNetwork,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    """Document a new network on the server in Lanvex Core. The server itself changes only through a change."""
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.add", "server", target_id=s.id, target_repr=f"{s.name} {body.name}"
    ) as t:
        t.detail.update(body.model_dump())
        t.detail["created"] = await svc.add(s, body)
    return await svc.view(s)


@router.delete("/servers/{server_id}/networks/{name}", response_model=NetworksView)
async def remove_network(
    server_id: uuid.UUID,
    name: str,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.remove", "server", target_id=s.id, target_repr=f"{s.name} {name}"
    ) as t:
        t.detail["removed"] = await svc.remove(s, name)
    return await svc.view(s)


class ImportIn(ApiModel):
    source: Literal["netzverwaltung"] = "netzverwaltung"


@router.post("/servers/{server_id}/networks/import", response_model=ImportReport)
async def import_networks(
    server_id: uuid.UUID,
    body: ImportIn,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> ImportReport:
    """Read another tool's networks on the server (through the change helper) and document them in Lanvex
    Core. Nothing on the server changes; taking them over is a change of its own."""
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    changes = ChangeService(db, settings, servers)
    async with audit.track(
        "server.network.import", "server", target_id=s.id, target_repr=f"{s.name} {body.source}"
    ) as t:
        found = await changes.inspect(s, body.source)
        report = await svc.import_netzverwaltung(s, found) if found.get("found") else []
        t.detail["networks"] = {n.name: n.result for n in report}
    return ImportReport(
        found=bool(found.get("found")), source=body.source, networks=report, view=await svc.view(s)
    )


@router.put("/servers/{server_id}/networks/{name}/access", response_model=NetworksView)
async def set_access(
    server_id: uuid.UUID,
    name: str,
    body: AccessSettings,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    """What devices on the network may reach, documented in Lanvex Core. The server changes only through
    the approved change."""
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.access", "server", target_id=s.id, target_repr=f"{s.name} {name}"
    ) as t:
        t.detail.update(body.model_dump(mode="json"))
        await svc.set_access(s, name, body)
    return await svc.view(s)


@router.put("/servers/{server_id}/networks/{name}/pool", response_model=NetworksView)
async def set_pool(
    server_id: uuid.UUID,
    name: str,
    body: PoolIn,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.pool", "server", target_id=s.id, target_repr=f"{s.name} {name}"
    ) as t:
        t.detail.update(body.model_dump())
        await svc.set_pool(s, name, body.start, body.end, body.lease)
    return await svc.view(s)


@router.put("/servers/{server_id}/networks/{name}/fixed", response_model=NetworksView)
async def set_fixed(
    server_id: uuid.UUID,
    name: str,
    body: FixedIn,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.fixed", "server", target_id=s.id, target_repr=f"{s.name} {name} {body.ip}"
    ) as t:
        t.detail.update(body.model_dump())
        await svc.set_fixed(s, name, body)
    return await svc.view(s)


@router.delete("/servers/{server_id}/networks/{name}/fixed/{ip}", response_model=NetworksView)
async def remove_fixed(
    server_id: uuid.UUID,
    name: str,
    ip: str,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> NetworksView:
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    async with audit.track(
        "server.network.unfix", "server", target_id=s.id, target_repr=f"{s.name} {name} {ip}"
    ):
        await svc.remove_fixed(s, name, ip)
    return await svc.view(s)


@router.post(
    "/servers/{server_id}/networks/change", response_model=ChangeDetail, status_code=status.HTTP_201_CREATED
)
async def prepare_networks_change(
    server_id: uuid.UUID,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
    takeover: Literal["netzverwaltung"] | None = None,
) -> ChangeDetail:
    """Prepare the change that gives the server exactly the networks Lanvex Core documents for it.
    takeover=netzverwaltung: Netzverwaltung's networks become Lanvex's in this change."""
    svc, servers = _svc(request, db, settings)
    s = await servers.get(server_id)
    view = await svc.view(s, takeover=takeover)
    if not view.linked:
        raise Conflict(f"Link {s.name} to its device first.")
    if view.change is None:
        raise Conflict(
            "Nothing to change: the server has the networks Lanvex Core documents."
            if view.in_sync
            else "Read the server first; Lanvex prepares changes only for servers managed by NetworkManager.",
            code="no_change",
        )
    changes = ChangeService(db, settings, servers)
    async with audit.track(
        "server.change.prepare", "server", target_id=s.id, target_repr=f"{s.name} networks"
    ) as t:
        c = await changes.prepare(s, view.change, user)
        await db.commit()
        t.target_type, t.target_id = "server_change", c.id
        t.target_repr = f"#{c.number} {s.name} networks"
        t.detail["summary"] = c.summary
    return to_detail(c, user, settings)
