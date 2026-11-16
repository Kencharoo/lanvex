import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from lanvex.changes.models import ChangeStatus
from lanvex.changes.runner import ChangeRunner
from lanvex.changes.schemas import (
    AnyChangeRequest,
    ApproveRequest,
    BrowserDecision,
    ChangeDetail,
    ChangeOut,
    EnabledUpdate,
    HelperInstall,
    Readiness,
)
from lanvex.changes.service import ChangeService, awaiting_browser, to_detail, to_out
from lanvex.core.errors import Conflict
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.integrations.linux import helper
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.service import ServerService

router = APIRouter(tags=["changes"])


def _svc(request: Request, db: DB, settings: SettingsDep) -> ChangeService:
    nb: NetBoxClient | None = request.app.state.netbox
    return ChangeService(db, settings, ServerService(db, settings, nb))


def _runner(request: Request) -> ChangeRunner | None:
    return getattr(request.app.state, "change_runner", None)


# ---- per server ----------------------------------------------------------------------------------------


@router.get("/servers/{server_id}/changes/readiness", response_model=Readiness)
async def readiness(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> Readiness:
    svc = _svc(request, db, settings)
    return await svc.readiness(await svc.servers.get(server_id))


@router.post("/servers/{server_id}/changes/helper/check", response_model=Readiness)
async def check_helper(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> Readiness:
    """Ask the server whether the change helper is installed, allowed through sudo, and current."""
    svc = _svc(request, db, settings)
    s = await svc.servers.get(server_id)
    async with audit.track("server.helper.check", "server", target_id=s.id, target_repr=s.name) as t:
        out = await svc.check_helper(s)
        await db.commit()
        t.detail["state"] = out.helper.state
    return out


@router.post("/servers/{server_id}/changes/helper/upload", response_model=HelperInstall)
async def upload_helper(
    server_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> HelperInstall:
    """Copy the helper into the SSH user's home directory. An administrator then installs it with sudo."""
    svc = _svc(request, db, settings)
    s = await svc.servers.get(server_id)
    async with audit.track("server.helper.upload", "server", target_id=s.id, target_repr=s.name) as t:
        out = await svc.upload_helper(s)
        t.detail.update(path=out.path, sha256=out.sha256)
    return out


@router.get("/changes/helper", response_class=Response)
async def download_helper(_: AdminUser) -> Response:
    return Response(
        helper.source(),
        media_type="text/x-python",
        headers={
            "Content-Disposition": f'attachment; filename="{helper.UPLOAD_NAME}"',
            "X-Lanvex-SHA256": helper.sha256(),
        },
    )


@router.put("/servers/{server_id}/changes/enabled", response_model=Readiness)
async def set_enabled(
    server_id: uuid.UUID,
    body: EnabledUpdate,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> Readiness:
    svc = _svc(request, db, settings)
    s = await svc.servers.get(server_id)
    action = "server.changes.enable" if body.enabled else "server.changes.disable"
    async with audit.track(action, "server", target_id=s.id, target_repr=s.name):
        await svc.set_enabled(s, body.enabled)
        await db.commit()
    return await svc.readiness(s)


@router.post("/servers/{server_id}/changes", response_model=ChangeDetail, status_code=status.HTTP_201_CREATED)
async def prepare_change(
    server_id: uuid.UUID,
    body: AnyChangeRequest,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> ChangeDetail:
    """Read the server, validate the change, and have the helper show exactly what it would do.

    Nothing on the server changes here. The result waits for approval.
    """
    svc = _svc(request, db, settings)
    s = await svc.servers.get(server_id)
    async with audit.track(
        "server.change.prepare", "server", target_id=s.id, target_repr=f"{s.name} {body.interface}"
    ) as t:
        c = await svc.prepare(s, body, user)
        await db.commit()
        t.target_type, t.target_id = "server_change", c.id
        t.target_repr = f"#{c.number} {s.name} {c.interface}"
        t.detail["summary"] = c.summary
    return to_detail(c, user, settings)


# ---- changes ------------------------------------------------------------------------------------------


@router.get("/changes", response_model=list[ChangeOut])
async def list_changes(
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: CurrentUser,
    server_id: uuid.UUID | None = None,
    status_: Annotated[ChangeStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    open_: Annotated[bool, Query(alias="open", description="Only changes not finished yet.")] = False,
) -> list[ChangeOut]:
    changes = await _svc(request, db, settings).query(server_id, status_, limit, open_only=open_)
    return [to_out(c) for c in changes]


def _host(request: Request) -> str:
    """The host name or address this browser uses for Lanvex (the Host header, without the port)."""
    return request.url.hostname or ""


@router.get("/changes/{change_id}", response_model=ChangeDetail)
async def get_change(
    change_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: CurrentUser
) -> ChangeDetail:
    svc = _svc(request, db, settings)
    c = await svc.get(change_id)
    where = await svc.browser_address(c, _host(request)) if awaiting_browser(c) else None
    return to_detail(c, user, settings, where)


@router.post("/changes/{change_id}/decision", response_model=ChangeDetail)
async def decide_change(
    change_id: uuid.UUID,
    body: BrowserDecision,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> ChangeDetail:
    """Keep or undo an applied change whose server is confirmed in the browser. That this request arrives
    at all is the point: it shows the browser still reaches Lanvex after the change."""
    svc = _svc(request, db, settings)
    c = await svc.get(change_id)
    where = await svc.browser_address(c, _host(request))
    async with audit.track(
        "server.change.keep" if body.decision == "keep" else "server.change.undo",
        "server_change",
        target_id=c.id,
        target_repr=f"#{c.number} {c.server_name} {c.interface}",
    ) as t:
        t.detail.update(host=where.host, on_server=where.on_server)
        await svc.decide(c, user, body.decision, where, body.acknowledge_indirect)
        await db.commit()
    runner = _runner(request)
    if runner is not None:
        runner.wake()
    return to_detail(c, user, settings)


@router.post("/changes/{change_id}/approve", response_model=ChangeDetail)
async def approve_change(
    change_id: uuid.UUID,
    body: ApproveRequest,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> ChangeDetail:
    svc = _svc(request, db, settings)
    c = await svc.get(change_id)
    async with audit.track(
        "server.change.approve",
        "server_change",
        target_id=c.id,
        target_repr=f"#{c.number} {c.server_name} {c.interface}",
    ):
        runner = _runner(request)
        if runner is None and settings.automation_enabled:
            raise Conflict(
                "The change runner is not running in this Lanvex process.", code="runner_unavailable"
            )
        await svc.approve(c, user, body.confirm_name)
        await db.commit()
    if runner is not None:
        runner.wake()
    return to_detail(c, user, settings)


@router.post("/changes/{change_id}/cancel", response_model=ChangeDetail)
async def cancel_change(
    change_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> ChangeDetail:
    svc = _svc(request, db, settings)
    c = await svc.get(change_id)
    async with audit.track(
        "server.change.cancel",
        "server_change",
        target_id=c.id,
        target_repr=f"#{c.number} {c.server_name} {c.interface}",
    ):
        await svc.cancel(c, user)
        await db.commit()
    return to_detail(c, user, settings)


@router.post("/changes/{change_id}/recheck", response_model=ChangeDetail)
async def recheck_change(
    change_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> ChangeDetail:
    """Ask the server again what became of a change whose outcome Lanvex could not determine."""
    svc = _svc(request, db, settings)
    c = await svc.get(change_id)
    if c.status != ChangeStatus.NEEDS_ATTENTION:
        raise Conflict(
            f"Change #{c.number} is {c.status.value.replace('_', ' ')}; there is nothing to check."
        )
    runner = _runner(request) or ChangeRunner(
        request.app.state.db, settings, lambda: request.app.state.netbox
    )
    async with audit.track(
        "server.change.recheck",
        "server_change",
        target_id=c.id,
        target_repr=f"#{c.number} {c.server_name} {c.interface}",
    ):
        await runner.reconcile(c.id)
    await db.refresh(c)
    return to_detail(c, user, settings)
