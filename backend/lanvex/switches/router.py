import uuid
from typing import Any

from fastapi import APIRouter, Request, status

from lanvex.core.errors import LanvexError
from lanvex.core.logging import get_logger
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.integrations.snmp.switch import SwitchFacts
from lanvex.monitoring.service import check_after_switch_read
from lanvex.netbox.client import NetBoxClient
from lanvex.switches.changes import SwitchChangeService
from lanvex.switches.changes import to_out as change_out
from lanvex.switches.models import Switch, SwitchChange
from lanvex.switches.ports import PortLocator
from lanvex.switches.schemas import (
    PortVlanRequest,
    SwitchApprove,
    SwitchChangeDetail,
    SwitchChangeOut,
    SwitchChanges,
    SwitchCreate,
    SwitchDetail,
    SwitchOut,
    SwitchTestRequest,
    SwitchTestResult,
    SwitchUpdate,
    SwitchWrite,
)
from lanvex.switches.service import SwitchService, to_out

router = APIRouter(tags=["switches"], prefix="/switches")
changes_router = APIRouter(tags=["switches"], prefix="/switch-changes")
log = get_logger("lanvex.switches")


def _svc(request: Request, db: DB, settings: SettingsDep) -> SwitchService:
    nb: NetBoxClient | None = request.app.state.netbox
    return SwitchService(db, settings, nb)


async def _detail(svc: SwitchService, sw: Switch) -> SwitchDetail:
    out = to_out(sw, await svc.netbox_ref(sw))
    row = await svc.latest(sw)
    detail = SwitchDetail(**out.model_dump(), facts=SwitchFacts.model_validate(row.facts) if row else None)
    if row is not None:
        try:
            found, _ = await PortLocator(svc.db, svc.settings, svc.nb).findings(sw.id)
            detail.findings = found.ports
        except LanvexError as e:
            detail.findings_error = e.message
    return detail


@router.get("", response_model=list[SwitchOut])
async def list_switches(request: Request, db: DB, settings: SettingsDep, _: CurrentUser) -> list[SwitchOut]:
    return [to_out(sw) for sw in await _svc(request, db, settings).all()]


@router.post("/test", response_model=SwitchTestResult)
async def test_switch(
    body: SwitchTestRequest, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> SwitchTestResult:
    """Sign in to a switch with these settings and read what it says it is. Nothing is stored."""
    result = await _svc(request, db, settings).test(body.address, body.port, body.credentials)
    await audit.record(
        "switch.test",
        "success" if result.status == "ok" else "failure",
        target_type="switch",
        target_repr=f"{body.address}:{body.port}",
        detail={"snmp": body.credentials.summary(), "status": result.status, "message": result.message},
    )
    return result


@router.post("", response_model=SwitchOut, status_code=status.HTTP_201_CREATED)
async def create_switch(
    body: SwitchCreate, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> SwitchOut:
    svc = _svc(request, db, settings)
    detail: dict[str, Any] = {
        "address": body.address,
        "port": body.port,
        "snmp": body.credentials.summary(),
        "netbox_device_id": body.netbox_device_id,
    }
    async with audit.track("switch.create", "switch", target_repr=body.name, detail=detail) as t:
        sw = await svc.create(body, user)
        await db.commit()
        t.target_id = sw.id
    return to_out(sw)


@router.get("/{switch_id}", response_model=SwitchDetail)
async def get_switch(
    switch_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> SwitchDetail:
    svc = _svc(request, db, settings)
    return await _detail(svc, await svc.get(switch_id))


@router.patch("/{switch_id}", response_model=SwitchOut)
async def update_switch(
    switch_id: uuid.UUID,
    body: SwitchUpdate,
    request: Request,
    db: DB,
    settings: SettingsDep,
    _: AdminUser,
    audit: Audit,
) -> SwitchOut:
    svc = _svc(request, db, settings)
    sw = await svc.get(switch_id)
    detail = body.model_dump(mode="json", exclude_unset=True, exclude={"credentials"})
    if body.credentials is not None:
        detail["snmp"] = body.credentials.summary()
    async with audit.track("switch.update", "switch", target_id=sw.id, target_repr=sw.name, detail=detail):
        await svc.update(sw, body)
        await db.commit()
    return to_out(sw, await svc.netbox_ref(sw))


@router.post("/{switch_id}/read", response_model=SwitchDetail)
async def read_switch(
    switch_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> SwitchDetail:
    """Read the switch's ports, VLANs, MAC address table and LLDP neighbours over SNMP. Read-only."""
    svc = _svc(request, db, settings)
    sw = await svc.get(switch_id)
    facts = await svc.read(sw)
    await check_after_switch_read(db, settings, request.app.state.netbox, sw, facts, svc.moves)
    await db.commit()
    await audit.record(
        "switch.read",
        "success" if facts is not None else "failure",
        target_type="switch",
        target_id=sw.id,
        target_repr=sw.name,
        detail={"status": sw.last_status, "message": sw.last_status_message},
    )
    return await _detail(svc, sw)


@router.delete("/{switch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_switch(
    switch_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> None:
    svc = _svc(request, db, settings)
    sw = await svc.get(switch_id)
    async with audit.track("switch.delete", "switch", target_id=sw.id, target_repr=sw.name):
        await db.delete(sw)
        await db.commit()


# ---- changes (port VLANs) ---------------------------------------------------------------------------


def _changes(request: Request, db: DB, settings: SettingsDep) -> SwitchChangeService:
    return SwitchChangeService(db, settings, request.app.state.netbox)


def _change_detail(svc: SwitchChangeService, c: SwitchChange, user: Any) -> SwitchChangeDetail:
    return SwitchChangeDetail(
        **change_out(c),
        request=c.request,
        before=c.before,
        writes=[SwitchWrite(text=op["text"], oids=[b[0] for b in op["binds"]]) for op in c.plan],
        warnings=c.warnings,
        log=c.log,
        approve_blocker=svc.approve_blocker(c, user) if user.role == "admin" else None,
    )


def _wake(request: Request) -> None:
    runner = getattr(request.app.state, "switch_change_runner", None)
    if runner is not None:
        runner.wake()


@router.get("/{switch_id}/changes", response_model=SwitchChanges)
async def switch_changes(
    switch_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> SwitchChanges:
    sw = await _svc(request, db, settings).get(switch_id)
    svc = _changes(request, db, settings)
    current = await svc.open_change(sw.id)
    return SwitchChanges(
        automation_enabled=settings.automation_enabled,
        changes_enabled=sw.changes_enabled,
        open=SwitchChangeOut(**change_out(current)) if current else None,
        recent=[SwitchChangeOut(**change_out(c)) for c in await svc.recent(sw.id, limit=20)],
    )


@router.post("/{switch_id}/ports/{if_index}/vlans", response_model=SwitchChangeDetail, status_code=201)
async def prepare_port_vlans(
    switch_id: uuid.UUID,
    if_index: int,
    body: PortVlanRequest,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> SwitchChangeDetail:
    """Prepare a change of the port's VLANs: read it fresh, check it is safe, plan the writes. Nothing is
    written to the switch until the change is approved."""
    sw = await _svc(request, db, settings).get(switch_id)
    svc = _changes(request, db, settings)
    detail = {"if_index": if_index, **body.model_dump()}
    async with audit.track(
        "switch.change.prepare", "switch", target_id=sw.id, target_repr=sw.name, detail=detail
    ) as t:
        c = await svc.prepare(sw, if_index, body.untagged, body.tagged, user)
        await db.commit()
        t.detail.update(change=c.number, summary=c.summary)
    return _change_detail(svc, c, user)


@changes_router.get("", response_model=list[SwitchChangeOut])
async def list_switch_changes(
    request: Request, db: DB, settings: SettingsDep, _: CurrentUser
) -> list[SwitchChangeOut]:
    return [SwitchChangeOut(**change_out(c)) for c in await _changes(request, db, settings).recent()]


@changes_router.get("/{change_id}", response_model=SwitchChangeDetail)
async def get_switch_change(
    change_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: CurrentUser
) -> SwitchChangeDetail:
    svc = _changes(request, db, settings)
    return _change_detail(svc, await svc.get(change_id), user)


@changes_router.post("/{change_id}/approve", response_model=SwitchChangeDetail)
async def approve_switch_change(
    change_id: uuid.UUID,
    body: SwitchApprove,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> SwitchChangeDetail:
    svc = _changes(request, db, settings)
    c = await svc.get(change_id)
    async with audit.track(
        "switch.change.approve",
        "switch",
        target_id=c.switch_id,
        target_repr=c.switch_name,
        detail={"change": c.number},
    ):
        await svc.approve(c, user, body.confirm_name)
        await db.commit()
    _wake(request)
    return _change_detail(svc, c, user)


@changes_router.post("/{change_id}/cancel", response_model=SwitchChangeDetail)
async def cancel_switch_change(
    change_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> SwitchChangeDetail:
    svc = _changes(request, db, settings)
    c = await svc.get(change_id)
    async with audit.track(
        "switch.change.cancel",
        "switch",
        target_id=c.switch_id,
        target_repr=c.switch_name,
        detail={"change": c.number},
    ):
        await svc.cancel(c, user)
        await db.commit()
    return _change_detail(svc, c, user)
