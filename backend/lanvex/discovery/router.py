"""Discovery API: hosts and LLDP links servers see, and recording them in NetBox."""

from typing import Any

from fastapi import APIRouter, Request

from lanvex.audit.service import Auditor
from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.discovery.service import (
    DiscoveredHosts,
    DiscoveredLinks,
    DiscoveryPlan,
    DiscoveryRecordRequest,
    DiscoveryService,
    stale,
)
from lanvex.netbox.client import NetBoxClient

router = APIRouter(tags=["discovery"], prefix="/discovery")


def _svc(request: Request, db: DB, settings: SettingsDep) -> DiscoveryService:
    nb: NetBoxClient | None = request.app.state.netbox
    return DiscoveryService(db, settings, nb)


@router.get("/hosts", response_model=DiscoveredHosts)
async def hosts(
    request: Request, db: DB, settings: SettingsDep, _: CurrentUser, undocumented: bool = False
) -> DiscoveredHosts:
    return await _svc(request, db, settings).hosts(undocumented=undocumented)


@router.get("/links", response_model=DiscoveredLinks)
async def links(request: Request, db: DB, settings: SettingsDep, _: CurrentUser) -> DiscoveredLinks:
    return await _svc(request, db, settings).links()


async def _apply(
    svc: DiscoveryService,
    plan: DiscoveryPlan,
    writes: list[dict[str, Any]],
    body: DiscoveryRecordRequest,
    audit: Auditor,
    action: str,
    path: str,
) -> DiscoveryPlan:
    if body.token is None:
        return plan
    if body.token != plan.token:
        raise stale(svc.netbox_client().name)
    plan = await svc.apply(plan, writes, path)
    detail: dict[str, Any] = {
        "done": [a.text for a in plan.done],
        "skipped": [f"{a.text}: {a.problem}" for a in plan.actions if a.problem],
    }
    if plan.failed:
        detail |= {"failed": plan.failed.text, "error": plan.error}
    await audit.record(
        action, "failure" if plan.failed else "success", target_type="discovery", detail=detail
    )
    return plan


@router.post("/hosts/record", response_model=DiscoveryPlan)
async def record_hosts(
    body: DiscoveryRecordRequest, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> DiscoveryPlan:
    """Record hosts servers saw as unassigned IP addresses. Without a token this only previews."""
    svc = _svc(request, db, settings)
    plan, writes = await svc.plan_hosts(list(dict.fromkeys(body.items)))
    return await _apply(svc, plan, writes, body, audit, "discovery.record_hosts", "ipam/ip-addresses/")


@router.post("/links/record", response_model=DiscoveryPlan)
async def record_links(
    body: DiscoveryRecordRequest, request: Request, db: DB, settings: SettingsDep, _: AdminUser, audit: Audit
) -> DiscoveryPlan:
    """Record cables for LLDP links. Without a token this only previews."""
    svc = _svc(request, db, settings)
    plan, writes = await svc.plan_links(list(dict.fromkeys(body.items)))
    return await _apply(svc, plan, writes, body, audit, "discovery.record_links", "dcim/cables/")
