import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from lanvex.changes.models import IN_PROGRESS, ServerChange
from lanvex.core.config import get_settings
from lanvex.core.errors import Conflict, NotFound
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel
from lanvex.servers.models import AuthMethod, ServerSnapshot
from lanvex.servers.schemas import (
    Comparison,
    Credentials,
    HistoryEntry,
    HostKeyOut,
    NetBoxRef,
    PinnedHostKey,
    ScanRequest,
    ServerCreate,
    ServerCreated,
    ServerOut,
    ServerUpdate,
    SnapshotMeta,
    SnapshotOut,
    TestResult,
)
from lanvex.servers.service import ServerService, authorized_keys_line, to_out

router = APIRouter(tags=["servers"], prefix="/servers")


def _svc(request: Request, db: DB, settings: SettingsDep) -> ServerService:
    nb: NetBoxClient | None = request.app.state.netbox
    return ServerService(db, settings, nb)


def _snapshot_out(s: ServerSnapshot) -> SnapshotOut:
    return SnapshotOut.model_validate({**SnapshotMeta.model_validate(s).model_dump(), "facts": s.facts})


class LinkCandidates(ApiModel):
    results: list[NetBoxRef]


@router.get("", response_model=list[ServerOut])
async def list_servers(request: Request, db: DB, settings: SettingsDep, _: CurrentUser) -> list[ServerOut]:
    return [to_out(s) for s in await _svc(request, db, settings).all()]


@router.post("/scan-host-key", response_model=HostKeyOut)
async def scan_host_key(
    body: ScanRequest, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> HostKeyOut:
    """Fetch a server's SSH host key so an administrator can verify its fingerprint. No login happens."""
    async with audit.track("server.scan_host_key", "server", target_repr=f"{body.address}:{body.port}") as t:
        hk = await _svc(request, db, settings).scan(body.address, body.port)
        t.detail["fingerprint"] = hk.fingerprint_sha256
    return hk


@router.get("/link-candidates", response_model=LinkCandidates)
async def link_candidates(
    request: Request, _: CurrentUser, q: Annotated[str, Query(min_length=1, max_length=100)]
) -> LinkCandidates:
    nb: NetBoxClient | None = request.app.state.netbox
    settings = get_settings()
    out: list[NetBoxRef] = []
    if nb is None:
        return LinkCandidates(results=out)
    for path, otype in (
        ("dcim/devices/", "dcim.device"),
        ("virtualization/virtual-machines/", "virtualization.virtualmachine"),
    ):
        try:
            page = await nb.page(path, {"q": q, "brief": True}, limit=15)
        except Exception:  # noqa: S112 - e.g. the token may not see virtual machines
            continue
        out += [
            NetBoxRef(
                object_type=otype,
                object_id=o["id"],
                name=o.get("name"),
                url=settings.public_netbox_url(o.get("display_url")),
            )
            for o in page.results
        ]
    return LinkCandidates(results=out)


@router.post("", response_model=ServerCreated, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: ServerCreate, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> ServerCreated:
    svc = _svc(request, db, settings)
    detail: dict[str, Any] = {
        "address": body.address,
        "port": body.port,
        "username": body.username,
        "auth_method": body.credentials.method.value,
        "host_key_fingerprint": body.host_key.fingerprint_sha256,
    }
    async with audit.track("server.create", "server", target_repr=body.name, detail=detail) as t:
        s = await svc.create(body, user)
        await db.commit()
        t.target_id = s.id
    if s.auth_method == AuthMethod.GENERATED_KEY:
        step = f"Add this line to ~{s.username}/.ssh/authorized_keys on the server, then test the connection: {authorized_keys_line(s.public_key)}"
    else:
        step = "Test the connection."
    return ServerCreated(server=to_out(s), next_step=step)


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> ServerOut:
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    link = None
    if s.netbox_object_type and s.netbox_object_id:
        from lanvex.servers.schemas import NetBoxLink

        link = NetBoxLink(object_type=s.netbox_object_type, object_id=s.netbox_object_id)  # type: ignore[arg-type]
    return to_out(s, await svc.netbox_ref(link, strict=False))


@router.patch("/{server_id}", response_model=ServerOut)
async def update_server(
    server_id: uuid.UUID,
    body: ServerUpdate,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> ServerOut:
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    detail = body.model_dump(mode="json", exclude_unset=True, exclude={"host_key"})
    if body.host_key:
        detail["host_key_fingerprint"] = body.host_key.fingerprint_sha256
    async with audit.track("server.update", "server", target_id=s.id, target_repr=s.name, detail=detail):
        await svc.update(s, body)
        await db.commit()
    return to_out(await svc.get(server_id))


@router.put("/{server_id}/host-key", response_model=ServerOut)
async def pin_host_key(
    server_id: uuid.UUID,
    body: PinnedHostKey,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> ServerOut:
    """Trust a new host key after a reinstall. The administrator must have verified the fingerprint."""
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    async with audit.track(
        "server.pin_host_key",
        "server",
        target_id=s.id,
        target_repr=s.name,
        detail={"old": s.host_key_fingerprint, "new": body.fingerprint_sha256},
    ):
        await svc.update(s, ServerUpdate(host_key=body))
        await db.commit()
    return to_out(s)


@router.put("/{server_id}/credentials", response_model=ServerCreated)
async def replace_credentials(
    server_id: uuid.UUID,
    body: Credentials,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> ServerCreated:
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    async with audit.track(
        "server.credentials",
        "server",
        target_id=s.id,
        target_repr=s.name,
        detail={"auth_method": body.method.value},
    ):
        await svc.replace_credentials(s, body)
        await db.commit()
    step = (
        f"Replace the old key in ~{s.username}/.ssh/authorized_keys with: {authorized_keys_line(s.public_key)}"
        if s.auth_method == AuthMethod.GENERATED_KEY
        else "Test the connection."
    )
    return ServerCreated(server=to_out(s), next_step=step)


@router.post("/{server_id}/test", response_model=TestResult)
async def test_server(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> TestResult:
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    result = await svc.test(s)
    await db.commit()
    await audit.record(
        "server.test",
        "success" if result.status == "ok" else "failure",
        target_type="server",
        target_id=s.id,
        target_repr=s.name,
        detail={"status": result.status.value, "message": result.message},
    )
    return result


@router.post("/{server_id}/collect", response_model=SnapshotOut)
async def collect(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> SnapshotOut:
    """Read the server's current network state over SSH. Read-only: nothing on the server is changed."""
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    snap = await svc.collect(s, user)
    await db.commit()
    await audit.record(
        "server.collect",
        "success" if snap.status == "ok" else "failure",
        target_type="server",
        target_id=s.id,
        target_repr=s.name,
        detail={"status": snap.status.value, "duration_ms": snap.duration_ms},
    )
    return _snapshot_out(snap)


@router.get("/{server_id}/snapshots", response_model=list[SnapshotMeta])
async def list_snapshots(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> list[ServerSnapshot]:
    svc = _svc(request, db, settings)
    await svc.get(server_id)
    return await svc.snapshots(server_id)


@router.get("/{server_id}/history", response_model=list[HistoryEntry])
async def history(
    server_id: uuid.UUID,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[HistoryEntry]:
    """The server's states over time, newest first, each with what differed from the one before."""
    svc = _svc(request, db, settings)
    await svc.get(server_id)
    return await svc.history(server_id, limit)


@router.get("/{server_id}/snapshots/latest", response_model=SnapshotOut | None)
async def latest_snapshot(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> SnapshotOut | None:
    svc = _svc(request, db, settings)
    await svc.get(server_id)
    snap, _facts = await svc.latest_facts(server_id)
    return _snapshot_out(snap) if snap else None


@router.get("/{server_id}/snapshots/{snapshot_id}", response_model=SnapshotOut)
async def get_snapshot(server_id: uuid.UUID, snapshot_id: uuid.UUID, db: DB, _: CurrentUser) -> SnapshotOut:
    snap = await db.get(ServerSnapshot, snapshot_id)
    if snap is None or snap.server_id != server_id:
        raise NotFound("Snapshot not found.")
    return _snapshot_out(snap)


@router.get("/{server_id}/snapshots/{snapshot_id}/raw")
async def get_snapshot_raw(
    server_id: uuid.UUID, snapshot_id: uuid.UUID, db: DB, _: AdminUser
) -> dict[str, Any]:
    """Command outputs as received, for diagnosing parsing problems. Files that may hold secrets are not stored."""
    snap = await db.get(ServerSnapshot, snapshot_id)
    if snap is None or snap.server_id != server_id:
        raise NotFound("Snapshot not found.")
    return snap.raw or {}


@router.get("/{server_id}/comparison", response_model=Comparison)
async def comparison(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> Comparison:
    svc = _svc(request, db, settings)
    return await svc.compare(await svc.get(server_id))


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> None:
    svc = _svc(request, db, settings)
    s = await svc.get(server_id)
    async with audit.track("server.delete", "server", target_id=s.id, target_repr=s.name):
        busy = await db.scalar(
            select(ServerChange.number).where(
                ServerChange.server_id == s.id, ServerChange.status.in_(IN_PROGRESS)
            )
        )
        if busy:
            raise Conflict(f"Change #{busy} is being applied to this server. Wait for its outcome first.")
        await db.delete(s)
        await db.commit()
