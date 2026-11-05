"""Device inventory, read from NetBox.

Everything here is documented state. Live device data (interface operational status, running
config) will come from integration adapters later and will be shown separately, never merged in.
"""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from lanvex.deps import AdminUser, Audit, CurrentUser, NetBox
from lanvex.ipam.ip_addresses import to_ip
from lanvex.ipam.router import WriteResult
from lanvex.ipam.schemas import IPAddress, VlanRef
from lanvex.schemas import ApiModel, Choice, Meta, Page, Ref, choice, meta, ref

router = APIRouter(tags=["devices"])


class DeviceType(ApiModel):
    id: int
    model: str
    manufacturer: Ref | None


class Device(ApiModel):
    id: int
    name: str
    status: Choice
    role: Ref | None
    device_type: DeviceType | None
    platform: Ref | None
    site: Ref | None
    location: Ref | None
    rack: Ref | None
    tenant: Ref | None
    serial: str
    asset_tag: str | None
    primary_ip: str | None
    oob_ip: str | None
    interface_count: int
    description: str
    meta: Meta


class Interface(ApiModel):
    id: int
    name: str
    type: Choice | None
    enabled: bool
    mgmt_only: bool
    mtu: int | None
    mac_address: str | None
    description: str
    mode: Choice | None
    untagged_vlan: VlanRef | None
    tagged_vlans: list[VlanRef]
    connected: bool
    lag: Ref | None


class DeviceDetail(Device):
    comments: str
    interfaces: list[Interface]
    ip_addresses: list[IPAddress]
    vlans: list[VlanRef]


class InterfaceVlans(ApiModel):
    mode: Literal["access", "tagged", "tagged-all"] | None
    untagged_vlan_id: int | None = None
    tagged_vlan_ids: list[int] = []


class InterfaceWrite(WriteResult):
    interface: Interface


def _vlanref(v: dict[str, Any] | None) -> VlanRef | None:
    return VlanRef(id=v["id"], vid=v["vid"], name=v.get("name") or "") if v else None


def _ip_text(v: dict[str, Any] | None) -> str | None:
    return v.get("address") if v else None


def to_device(raw: dict[str, Any]) -> Device:
    dt = raw.get("device_type")
    return Device(
        id=raw["id"],
        name=raw.get("name") or f"Unnamed device {raw['id']}",
        status=choice(raw.get("status")) or choice("active"),  # type: ignore[arg-type]
        role=ref(raw.get("role")),
        device_type=DeviceType(
            id=dt["id"],
            model=dt.get("model") or dt.get("display") or "",
            manufacturer=ref(dt.get("manufacturer")),
        )
        if dt
        else None,
        platform=ref(raw.get("platform")),
        site=ref(raw.get("site")),
        location=ref(raw.get("location")),
        rack=ref(raw.get("rack")),
        tenant=ref(raw.get("tenant")),
        serial=raw.get("serial") or "",
        asset_tag=raw.get("asset_tag"),
        primary_ip=_ip_text(raw.get("primary_ip")),
        oob_ip=_ip_text(raw.get("oob_ip")),
        interface_count=int(raw.get("interface_count") or 0),
        description=raw.get("description") or "",
        meta=meta(raw),
    )


def to_interface(raw: dict[str, Any]) -> Interface:
    mac = raw.get("mac_address")
    if not mac and raw.get("primary_mac_address"):
        mac = raw["primary_mac_address"].get("mac_address")
    return Interface(
        id=raw["id"],
        name=raw.get("name") or "",
        type=choice(raw.get("type")),
        enabled=bool(raw.get("enabled", True)),
        mgmt_only=bool(raw.get("mgmt_only")),
        mtu=raw.get("mtu"),
        mac_address=mac,
        description=raw.get("description") or "",
        mode=choice(raw.get("mode")),
        untagged_vlan=_vlanref(raw.get("untagged_vlan")),
        tagged_vlans=[v for v in (_vlanref(x) for x in raw.get("tagged_vlans") or []) if v],
        connected=bool(raw.get("cable") or raw.get("mark_connected")),
        lag=ref(raw.get("lag")),
    )


@router.get("/devices", response_model=Page[Device])
async def list_devices(
    nb: NetBox,
    _: CurrentUser,
    q: str | None = None,
    site_id: int | None = None,
    role_id: int | None = None,
    manufacturer_id: int | None = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[Device]:
    page = await nb.page(
        "dcim/devices/",
        {
            "q": q,
            "site_id": site_id,
            "role_id": role_id,
            "manufacturer_id": manufacturer_id,
            "status": status_,
            "ordering": "name",
            "omit": "config_context",
        },
        limit=limit,
        offset=offset,
    )
    return Page(count=page.count, limit=limit, offset=offset, results=[to_device(r) for r in page.results])


@router.get("/devices/{device_id}", response_model=DeviceDetail)
async def get_device(device_id: int, nb: NetBox, _: CurrentUser) -> DeviceDetail:
    raw, interfaces, ips = await asyncio.gather(
        nb.get(f"dcim/devices/{device_id}/", {"omit": "config_context"}),
        nb.list_all("dcim/interfaces/", {"device_id": device_id}, cap=2000),
        nb.list_all("ipam/ip-addresses/", {"device_id": device_id}, cap=2000),
    )
    ifaces = [to_interface(i) for i in interfaces]
    vlans: dict[int, VlanRef] = {}
    for i in ifaces:
        for v in ([i.untagged_vlan] if i.untagged_vlan else []) + i.tagged_vlans:
            vlans[v.id] = v
    base = to_device(raw)
    return DeviceDetail(
        **base.model_dump(),
        comments=raw.get("comments") or "",
        interfaces=ifaces,
        ip_addresses=[to_ip(i) for i in ips],
        vlans=sorted(vlans.values(), key=lambda v: v.vid),
    )


@router.get("/interfaces", response_model=Page[Interface])
async def list_interfaces(
    nb: NetBox,
    _: CurrentUser,
    device_id: int,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[Interface]:
    page = await nb.page("dcim/interfaces/", {"device_id": device_id, "q": q}, limit=limit, offset=offset)
    return Page(count=page.count, limit=limit, offset=offset, results=[to_interface(r) for r in page.results])


@router.put("/interfaces/{interface_id}/vlans", response_model=InterfaceWrite)
async def set_interface_vlans(
    interface_id: int, body: InterfaceVlans, nb: NetBox, _: AdminUser, audit: Audit
) -> InterfaceWrite:
    """Document the 802.1Q mode and VLANs of an interface in NetBox. Does not touch the device."""
    payload: dict[str, Any]
    if body.mode is None:
        payload = {"mode": "", "untagged_vlan": None, "tagged_vlans": []}
    elif body.mode == "access":
        payload = {"mode": "access", "untagged_vlan": body.untagged_vlan_id, "tagged_vlans": []}
    elif body.mode == "tagged":
        payload = {
            "mode": "tagged",
            "untagged_vlan": body.untagged_vlan_id,
            "tagged_vlans": body.tagged_vlan_ids,
        }
    else:
        payload = {"mode": "tagged-all", "untagged_vlan": body.untagged_vlan_id, "tagged_vlans": []}
    async with audit.track(
        "interface.vlans", "dcim.interface", target_id=interface_id, detail=body.model_dump(mode="json")
    ) as t:
        await nb.patch(f"dcim/interfaces/{interface_id}/", payload)
        iface = to_interface(await nb.get(f"dcim/interfaces/{interface_id}/"))
        t.target_repr = iface.name
    return InterfaceWrite(interface=iface)
