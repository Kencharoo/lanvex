import asyncio
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from lanvex.audit.models import AuditEvent
from lanvex.core.errors import LanvexError, PermissionDenied
from lanvex.deps import DB, CurrentUser, NetBox
from lanvex.ipam.prefixes import PrefixService
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel

router = APIRouter(tags=["dashboard"])

CACHE_SECONDS = 30
UTILIZATION_SAMPLE = 60


class Counts(ApiModel):
    prefixes: int
    prefixes_v4: int
    prefixes_v6: int
    ip_addresses: int
    ip_assigned: int
    ip_unassigned: int
    vlans: int
    devices: int
    vrfs: int
    sites: int


class StatusBreakdown(ApiModel):
    status: str
    count: int


class TopPrefix(ApiModel):
    id: int
    prefix: str
    name: str
    vrf: str | None
    percent: float
    used: int
    total: int


class Change(ApiModel):
    time: datetime
    user: str | None
    action: str
    object_type: str
    object_repr: str
    source: str  # "netbox" or "lanvex"


class Overview(ApiModel):
    counts: Counts
    ip_status: list[StatusBreakdown]
    top_utilized: list[TopPrefix]
    utilization_sampled: int
    recent_changes: list[Change]
    recent_changes_available: bool
    recent_changes_message: str | None
    generated_at: datetime


_cache: dict[str, tuple[float, Overview]] = {}


async def _counts(nb: NetBoxClient) -> Counts:
    keys = [
        ("prefixes", "ipam/prefixes/", {}),
        ("prefixes_v4", "ipam/prefixes/", {"family": 4}),
        ("prefixes_v6", "ipam/prefixes/", {"family": 6}),
        ("ip_addresses", "ipam/ip-addresses/", {}),
        ("ip_assigned", "ipam/ip-addresses/", {"assigned": True}),
        ("vlans", "ipam/vlans/", {}),
        ("devices", "dcim/devices/", {}),
        ("vrfs", "ipam/vrfs/", {}),
        ("sites", "dcim/sites/", {}),
    ]
    values = await asyncio.gather(*(nb.count(path, params) for _, path, params in keys))
    data = dict(zip([k for k, _, _ in keys], values, strict=True))
    return Counts(**data, ip_unassigned=max(data["ip_addresses"] - data["ip_assigned"], 0))


async def _ip_status(nb: NetBoxClient) -> list[StatusBreakdown]:
    statuses = ["active", "reserved", "dhcp", "slaac", "deprecated"]
    counts = await asyncio.gather(*(nb.count("ipam/ip-addresses/", {"status": s}) for s in statuses))
    return [StatusBreakdown(status=s, count=c) for s, c in zip(statuses, counts, strict=True)]


async def _top_utilized(nb: NetBoxClient) -> tuple[list[TopPrefix], int]:
    svc = PrefixService(nb)
    page = await svc.search(status=["active", "reserved"], limit=UTILIZATION_SAMPLE, include_utilization=True)
    ranked = sorted(
        (p for p in page.results if p.utilization),
        key=lambda p: p.utilization.percent if p.utilization else 0,
        reverse=True,
    )
    top = [
        TopPrefix(
            id=p.id,
            prefix=p.prefix,
            name=p.name,
            vrf=p.vrf.name if p.vrf else None,
            percent=round(p.utilization.percent, 1),
            used=p.utilization.used,
            total=p.utilization.total,
        )
        for p in ranked[:8]
        if p.utilization
    ]
    return top, len(page.results)


async def _netbox_changes(nb: NetBoxClient) -> list[Change]:
    page = await nb.page("core/object-changes/", {"ordering": "-time"}, limit=10)
    out = []
    for c in page.results:
        action = c.get("action") or {}
        out.append(
            Change(
                time=c["time"],
                user=c.get("user_name"),
                action=str(action.get("value") if isinstance(action, dict) else action),
                object_type=str(c.get("changed_object_type") or ""),
                object_repr=str(c.get("object_repr") or ""),
                source="netbox",
            )
        )
    return out


@router.get("/dashboard/overview", response_model=Overview)
async def overview(nb: NetBox, db: DB, _: CurrentUser, refresh: bool = False) -> Overview:
    cached = _cache.get(nb.base_url)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    counts, ip_status, (top, sampled) = await asyncio.gather(_counts(nb), _ip_status(nb), _top_utilized(nb))

    available, message = True, None
    try:
        changes = await _netbox_changes(nb)
    except PermissionDenied:
        available, message = False, "The NetBox token cannot read the change log (core.view_objectchange)."
        changes = []
    except LanvexError as e:
        available, message, changes = False, e.message, []

    if not changes:
        # Fall back to Lanvex's own audit trail so the panel is still useful.
        rows = await db.scalars(
            select(AuditEvent)
            .where(AuditEvent.outcome == "success", ~AuditEvent.action.startswith("auth."))
            .order_by(AuditEvent.occurred_at.desc())
            .limit(10)
        )
        changes = [
            Change(
                time=r.occurred_at,
                user=r.actor_username,
                action=r.action.split(".")[-1],
                object_type=r.target_type or "",
                object_repr=r.target_repr or "",
                source="lanvex",
            )
            for r in rows
        ]

    result = Overview(
        counts=counts,
        ip_status=ip_status,
        top_utilized=top,
        utilization_sampled=sampled,
        recent_changes=changes,
        recent_changes_available=available,
        recent_changes_message=message,
        generated_at=datetime.now().astimezone(),
    )
    _cache[nb.base_url] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()


__all__: list[Any] = ["router", "clear_cache"]
