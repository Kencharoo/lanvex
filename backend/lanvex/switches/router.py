import uuid
from typing import Any

from fastapi import APIRouter, Request, status

from lanvex.core.errors import LanvexError
from lanvex.core.logging import get_logger
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.integrations.snmp.switch import SwitchFacts
from lanvex.monitoring.service import check_after_switch_read
from lanvex.netbox.client import NetBoxClient
from lanvex.switches.models import Switch
from lanvex.switches.ports import PortLocator
from lanvex.switches.schemas import (
    SwitchCreate,
    SwitchDetail,
    SwitchOut,
    SwitchTestRequest,
    SwitchTestResult,
    SwitchUpdate,
)
from lanvex.switches.service import SwitchService, to_out

router = APIRouter(tags=["switches"], prefix="/switches")
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
