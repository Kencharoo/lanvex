"""Small reference lists used by filters and form pickers (VRFs, sites, roles, …)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from lanvex.deps import CurrentUser, NetBox
from lanvex.schemas import ApiModel

router = APIRouter(tags=["lookups"])

Kind = Literal[
    "vrfs", "prefix-roles", "tenants", "sites", "vlan-groups", "vlans", "device-roles", "manufacturers"
]

ENDPOINTS: dict[str, str] = {
    "vrfs": "ipam/vrfs/",
    "prefix-roles": "ipam/roles/",
    "tenants": "tenancy/tenants/",
    "sites": "dcim/sites/",
    "vlan-groups": "ipam/vlan-groups/",
    "vlans": "ipam/vlans/",
    "device-roles": "dcim/device-roles/",
    "manufacturers": "dcim/manufacturers/",
}


class Option(ApiModel):
    id: int
    label: str
    hint: str | None = None


@router.get("/lookups/{kind}", response_model=list[Option])
async def lookup(
    kind: Kind,
    nb: NetBox,
    _: CurrentUser,
    q: Annotated[str | None, Query(max_length=100)] = None,
    site_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[Option]:
    params: dict[str, str | int | None] = {"q": q}
    if kind == "vlans":
        params["site_id"] = site_id
    page = await nb.page(ENDPOINTS[kind], params, limit=limit)
    out: list[Option] = []
    for r in page.results:
        if kind == "vlans":
            out.append(
                Option(
                    id=r["id"],
                    label=f"{r.get('name', '')} ({r['vid']})",
                    hint=(r.get("site") or {}).get("name"),
                )
            )
        elif kind == "vrfs":
            out.append(Option(id=r["id"], label=r.get("name") or r.get("display"), hint=r.get("rd")))
        else:
            out.append(Option(id=r["id"], label=r.get("name") or r.get("display") or str(r["id"])))
    return out
