from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import Field

from lanvex.core.errors import ValidationFailed
from lanvex.deps import AdminUser, Audit, CurrentUser, NetBox
from lanvex.ipam.router import WriteResult
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel, Choice, Meta, Page, Ref, Tag, choice, meta, ref, tags

router = APIRouter(tags=["vlans"])
VLANS = "ipam/vlans/"


class VLANStatus(StrEnum):
    ACTIVE = "active"
    RESERVED = "reserved"
    DEPRECATED = "deprecated"


class VLAN(ApiModel):
    id: int
    vid: int
    name: str
    status: Choice
    site: Ref | None
    group: Ref | None
    role: Ref | None
    tenant: Ref | None
    description: str
    prefix_count: int
    tags: list[Tag]
    meta: Meta


class VLANCreate(ApiModel):
    vid: Annotated[int, Field(ge=1, le=4094)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    status: VLANStatus = VLANStatus.ACTIVE
    site_id: int | None = None
    group_id: int | None = None
    role_id: int | None = None
    tenant_id: int | None = None
    description: Annotated[str | None, Field(max_length=200)] = None


class VLANUpdate(ApiModel):
    vid: Annotated[int | None, Field(ge=1, le=4094)] = None
    name: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    status: VLANStatus | None = None
    site_id: int | None = None
    group_id: int | None = None
    role_id: int | None = None
    tenant_id: int | None = None
    description: Annotated[str | None, Field(max_length=200)] = None


class VLANWrite(WriteResult):
    vlan: VLAN


def to_vlan(raw: dict[str, Any]) -> VLAN:
    return VLAN(
        id=raw["id"],
        vid=int(raw["vid"]),
        name=raw.get("name") or "",
        status=choice(raw.get("status")) or choice("active"),  # type: ignore[arg-type]
        site=ref(raw.get("site")),
        group=ref(raw.get("group")),
        role=ref(raw.get("role")),
        tenant=ref(raw.get("tenant")),
        description=raw.get("description") or "",
        prefix_count=int(raw.get("prefix_count") or 0),
        tags=tags(raw.get("tags")),
        meta=meta(raw),
    )


_FK = {"site_id": "site", "group_id": "group", "role_id": "role", "tenant_id": "tenant"}


def _payload(data: VLANCreate | VLANUpdate, only_set: bool) -> dict[str, Any]:
    fields = data.model_fields_set if only_set else set(type(data).model_fields)
    out: dict[str, Any] = {}
    for f in fields:
        v = getattr(data, f)
        if f == "status" and v is not None:
            v = v.value
        if f == "description" and v is None:
            v = ""
        out[_FK.get(f, f)] = v
    return out


async def _check_unique(
    nb: NetBoxClient,
    vid: int,
    name: str | None,
    group_id: int | None,
    site_id: int | None,
    exclude_id: int | None = None,
) -> None:
    """NetBox enforces VID/name uniqueness within a VLAN group; give a clear message before it does."""
    if group_id is None:
        return
    page = await nb.page(VLANS, {"group_id": group_id}, limit=1000)
    for raw in page.results:
        if raw["id"] == exclude_id:
            continue
        if int(raw["vid"]) == vid:
            raise ValidationFailed(
                f"VLAN {vid} already exists in this group.", fields={"vid": ["Already in use."]}
            )
        if name and (raw.get("name") or "").lower() == name.lower():
            raise ValidationFailed(
                f"A VLAN named '{name}' already exists in this group.", fields={"name": ["Already in use."]}
            )


@router.get("/vlans", response_model=Page[VLAN])
async def list_vlans(
    nb: NetBox,
    _: CurrentUser,
    q: str | None = None,
    site_id: int | None = None,
    group_id: int | None = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[VLAN]:
    page = await nb.page(
        VLANS,
        {"q": q, "site_id": site_id, "group_id": group_id, "status": status_, "ordering": "vid"},
        limit=limit,
        offset=offset,
    )
    return Page(count=page.count, limit=limit, offset=offset, results=[to_vlan(r) for r in page.results])


@router.get("/vlans/{vlan_id}", response_model=VLAN)
async def get_vlan(vlan_id: int, nb: NetBox, _: CurrentUser) -> VLAN:
    return to_vlan(await nb.get(f"{VLANS}{vlan_id}/"))


@router.post("/vlans", response_model=VLANWrite, status_code=status.HTTP_201_CREATED)
async def create_vlan(body: VLANCreate, nb: NetBox, _: AdminUser, audit: Audit) -> VLANWrite:
    async with audit.track(
        "vlan.create", "ipam.vlan", target_repr=f"{body.vid} {body.name}", detail=body.model_dump(mode="json")
    ) as t:
        await _check_unique(nb, body.vid, body.name, body.group_id, body.site_id)
        vlan = to_vlan(await nb.post(VLANS, _payload(body, only_set=False)))
        t.target_id = vlan.id
    return VLANWrite(vlan=vlan)


@router.patch("/vlans/{vlan_id}", response_model=VLANWrite)
async def update_vlan(vlan_id: int, body: VLANUpdate, nb: NetBox, _: AdminUser, audit: Audit) -> VLANWrite:
    async with audit.track(
        "vlan.update", "ipam.vlan", target_id=vlan_id, detail=body.model_dump(mode="json", exclude_unset=True)
    ) as t:
        current = to_vlan(await nb.get(f"{VLANS}{vlan_id}/"))
        vid = body.vid if body.vid is not None else current.vid
        group_id = (
            body.group_id
            if "group_id" in body.model_fields_set
            else (current.group.id if current.group else None)
        )
        name = body.name if body.name is not None else current.name
        await _check_unique(nb, vid, name, group_id, None, exclude_id=vlan_id)
        payload = _payload(body, only_set=True)
        if payload:
            await nb.patch(f"{VLANS}{vlan_id}/", payload)
        vlan = to_vlan(await nb.get(f"{VLANS}{vlan_id}/"))
        t.target_repr = f"{vlan.vid} {vlan.name}"
    return VLANWrite(vlan=vlan)


@router.delete("/vlans/{vlan_id}", response_model=WriteResult)
async def delete_vlan(vlan_id: int, nb: NetBox, _: AdminUser, audit: Audit) -> WriteResult:
    async with audit.track("vlan.delete", "ipam.vlan", target_id=vlan_id) as t:
        current = to_vlan(await nb.get(f"{VLANS}{vlan_id}/"))
        t.target_repr = f"{current.vid} {current.name}"
        await nb.delete(f"{VLANS}{vlan_id}/")
    return WriteResult()
