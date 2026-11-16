from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from lanvex.deps import AdminUser, Audit, CurrentUser, NetBox
from lanvex.ipam.ip_addresses import IPAddressService
from lanvex.ipam.prefixes import PrefixService
from lanvex.ipam.schemas import (
    AllocateFromPrefix,
    AvailableIP,
    AvailablePrefix,
    IPAddress,
    IPAddressCreate,
    IPAddressUpdate,
    IPAssign,
    Prefix,
    PrefixCreate,
    PrefixUpdate,
    PrefixValidateRequest,
    PrefixValidation,
)
from lanvex.schemas import ApiModel, Page

router = APIRouter(tags=["ipam"])

Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


class WriteResult(ApiModel):
    """Every write reports where the change landed. In the MVP that is always NetBox (documented state)."""

    state: Literal["documented"] = "documented"
    warnings: list[str] = []


class PrefixWrite(WriteResult):
    prefix: Prefix


class IPWrite(WriteResult):
    ip_address: IPAddress


class DeleteImpact(ApiModel):
    prefix: str
    child_prefixes: int
    ip_addresses: int


# ---- prefixes ------------------------------------------------------------------


@router.get("/prefixes", response_model=Page[Prefix])
async def list_prefixes(
    nb: NetBox,
    _: CurrentUser,
    q: str | None = None,
    family: Literal[4, 6] | None = None,
    vrf_id: int | None = None,
    global_only: bool = False,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    within: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
    include_utilization: bool = True,
) -> Page[Prefix]:
    return await PrefixService(nb).search(
        q=q,
        family=family,
        vrf_id=vrf_id,
        global_only=global_only,
        status=status_,
        within=within,
        limit=limit,
        offset=offset,
        include_utilization=include_utilization and limit <= 100,
        include_gateway=limit <= 1000,
    )


@router.post("/prefixes/validate", response_model=PrefixValidation)
async def validate_prefix(body: PrefixValidateRequest, nb: NetBox, _: CurrentUser) -> PrefixValidation:
    return await PrefixService(nb).validate(body)


@router.post("/prefixes", response_model=PrefixWrite, status_code=status.HTTP_201_CREATED)
async def create_prefix(body: PrefixCreate, nb: NetBox, _: AdminUser, audit: Audit) -> PrefixWrite:
    async with audit.track(
        "prefix.create", "ipam.prefix", target_repr=body.prefix, detail=body.model_dump(mode="json")
    ) as t:
        prefix, warnings = await PrefixService(nb).create(body)
        t.target_id, t.target_repr = prefix.id, prefix.prefix
        if warnings:
            t.detail["warnings"] = warnings
    return PrefixWrite(prefix=prefix, warnings=warnings)


@router.get("/prefixes/{prefix_id}", response_model=Prefix)
async def get_prefix(prefix_id: int, nb: NetBox, _: CurrentUser) -> Prefix:
    return await PrefixService(nb).get(prefix_id)


@router.patch("/prefixes/{prefix_id}", response_model=PrefixWrite)
async def update_prefix(
    prefix_id: int, body: PrefixUpdate, nb: NetBox, _: AdminUser, audit: Audit
) -> PrefixWrite:
    async with audit.track(
        "prefix.update",
        "ipam.prefix",
        target_id=prefix_id,
        detail=body.model_dump(mode="json", exclude_unset=True),
    ) as t:
        prefix, warnings = await PrefixService(nb).update(prefix_id, body)
        t.target_repr = prefix.prefix
    return PrefixWrite(prefix=prefix, warnings=warnings)


@router.get("/prefixes/{prefix_id}/delete-impact", response_model=DeleteImpact)
async def prefix_delete_impact(prefix_id: int, nb: NetBox, _: CurrentUser) -> DeleteImpact:
    return DeleteImpact(**await PrefixService(nb).delete_impact(prefix_id))


@router.delete("/prefixes/{prefix_id}", response_model=WriteResult)
async def delete_prefix(
    prefix_id: int, confirm: Annotated[str, Query(max_length=64)], nb: NetBox, _: AdminUser, audit: Audit
) -> WriteResult:
    async with audit.track("prefix.delete", "ipam.prefix", target_id=prefix_id, target_repr=confirm) as t:
        deleted = await PrefixService(nb).delete(prefix_id, confirm)
        t.detail["name"] = deleted.name
    return WriteResult()


@router.get("/prefixes/{prefix_id}/available-ips", response_model=list[AvailableIP])
async def prefix_available_ips(
    prefix_id: int, nb: NetBox, _: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 10
) -> list[AvailableIP]:
    return await PrefixService(nb).available_ips(prefix_id, limit)


@router.get("/prefixes/{prefix_id}/available-prefixes", response_model=list[AvailablePrefix])
async def prefix_available_prefixes(prefix_id: int, nb: NetBox, _: CurrentUser) -> list[AvailablePrefix]:
    return await PrefixService(nb).available_prefixes(prefix_id)


@router.post("/prefixes/{prefix_id}/allocate-ip", response_model=IPWrite, status_code=status.HTTP_201_CREATED)
async def allocate_ip(
    prefix_id: int, body: AllocateFromPrefix, nb: NetBox, _: AdminUser, audit: Audit
) -> IPWrite:
    async with audit.track(
        "ip.allocate", "ipam.ipaddress", detail={"prefix_id": prefix_id, **body.model_dump(mode="json")}
    ) as t:
        ip = await IPAddressService(nb).allocate(prefix_id, body)
        t.target_id, t.target_repr = ip.id, ip.address
    return IPWrite(ip_address=ip)


# ---- IP addresses -------------------------------------------------------------


@router.get("/ip-addresses", response_model=Page[IPAddress])
async def list_ips(
    nb: NetBox,
    _: CurrentUser,
    q: str | None = None,
    parent: str | None = None,
    vrf_id: int | None = None,
    global_only: bool = False,
    family: Literal[4, 6] | None = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    assigned: bool | None = None,
    device_id: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[IPAddress]:
    return await IPAddressService(nb).search(
        q=q,
        parent=parent,
        vrf_id=vrf_id,
        global_only=global_only,
        family=family,
        status=status_,
        assigned=assigned,
        device_id=device_id,
        limit=limit,
        offset=offset,
    )


@router.post("/ip-addresses", response_model=IPWrite, status_code=status.HTTP_201_CREATED)
async def create_ip(body: IPAddressCreate, nb: NetBox, _: AdminUser, audit: Audit) -> IPWrite:
    async with audit.track(
        "ip.create", "ipam.ipaddress", target_repr=body.address, detail=body.model_dump(mode="json")
    ) as t:
        ip = await IPAddressService(nb).create(body)
        t.target_id, t.target_repr = ip.id, ip.address
    return IPWrite(ip_address=ip)


@router.get("/ip-addresses/{ip_id}", response_model=IPAddress)
async def get_ip(ip_id: int, nb: NetBox, _: CurrentUser) -> IPAddress:
    return await IPAddressService(nb).get(ip_id)


@router.patch("/ip-addresses/{ip_id}", response_model=IPWrite)
async def update_ip(ip_id: int, body: IPAddressUpdate, nb: NetBox, _: AdminUser, audit: Audit) -> IPWrite:
    async with audit.track(
        "ip.update",
        "ipam.ipaddress",
        target_id=ip_id,
        detail=body.model_dump(mode="json", exclude_unset=True),
    ) as t:
        ip = await IPAddressService(nb).update(ip_id, body)
        t.target_repr = ip.address
    return IPWrite(ip_address=ip)


@router.post("/ip-addresses/{ip_id}/assign", response_model=IPWrite)
async def assign_ip(ip_id: int, body: IPAssign, nb: NetBox, _: AdminUser, audit: Audit) -> IPWrite:
    async with audit.track(
        "ip.assign", "ipam.ipaddress", target_id=ip_id, detail={"interface_id": body.interface_id}
    ) as t:
        ip = await IPAddressService(nb).assign(ip_id, body.interface_id)
        t.target_repr = ip.address
    return IPWrite(ip_address=ip)


@router.post("/ip-addresses/{ip_id}/release", response_model=IPWrite)
async def release_ip(ip_id: int, nb: NetBox, _: AdminUser, audit: Audit) -> IPWrite:
    """Remove the interface assignment. The address record stays in NetBox."""
    async with audit.track("ip.release", "ipam.ipaddress", target_id=ip_id) as t:
        ip = await IPAddressService(nb).unassign(ip_id)
        t.target_repr = ip.address
    return IPWrite(ip_address=ip)


@router.delete("/ip-addresses/{ip_id}", response_model=WriteResult)
async def delete_ip(ip_id: int, nb: NetBox, _: AdminUser, audit: Audit) -> WriteResult:
    async with audit.track("ip.delete", "ipam.ipaddress", target_id=ip_id) as t:
        deleted = await IPAddressService(nb).delete(ip_id)
        t.target_repr = deleted.address
    return WriteResult()
