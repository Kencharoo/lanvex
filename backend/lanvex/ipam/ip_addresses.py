from typing import Any

from lanvex.core.errors import ValidationFailed
from lanvex.ipam import addressing as addr
from lanvex.ipam.prefixes import GATEWAY_TAG_SLUG, IPS, PREFIXES, vrf_filter
from lanvex.ipam.schemas import (
    AllocateFromPrefix,
    AssignedInterface,
    IPAddress,
    IPAddressCreate,
    IPAddressUpdate,
)
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import Page, choice, meta, ref, tags

INTERFACE_TYPE = "dcim.interface"


def to_ip(raw: dict[str, Any]) -> IPAddress:
    assigned = None
    obj = raw.get("assigned_object")
    if obj and raw.get("assigned_object_type"):
        parent = obj.get("device") or obj.get("virtual_machine")
        assigned = AssignedInterface(
            interface_id=int(obj["id"]),
            interface_name=str(obj.get("name") or obj.get("display") or obj["id"]),
            device=ref(parent),
            object_type=str(raw["assigned_object_type"]),
        )
    raw_tags = raw.get("tags") or []
    return IPAddress(
        id=raw["id"],
        address=raw["address"],
        family=int((raw.get("family") or {}).get("value") or addr.parse_interface(raw["address"]).version),
        status=choice(raw.get("status")) or choice("active"),  # type: ignore[arg-type]
        role=choice(raw.get("role")),
        vrf=ref(raw.get("vrf")),
        tenant=ref(raw.get("tenant")),
        dns_name=raw.get("dns_name") or "",
        description=raw.get("description") or "",
        assigned=assigned,
        is_gateway=any(t.get("slug") == GATEWAY_TAG_SLUG for t in raw_tags),
        tags=tags(raw_tags),
        meta=meta(raw),
    )


class IPAddressService:
    def __init__(self, nb: NetBoxClient):
        self.nb = nb

    async def search(
        self,
        *,
        q: str | None = None,
        parent: str | None = None,
        vrf_id: int | None = None,
        global_only: bool = False,
        family: int | None = None,
        status: list[str] | None = None,
        assigned: bool | None = None,
        device_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IPAddress]:
        if parent:
            parent = str(addr.parse_network(parent))
        params = {
            "q": q,
            "parent": parent,
            "vrf_id": "null" if global_only else vrf_id,
            "family": family,
            "status": status,
            "assigned": assigned,
            "device_id": device_id,
            "ordering": "vrf,address",
        }
        page = await self.nb.page(IPS, params, limit=limit, offset=offset)
        return Page(count=page.count, limit=limit, offset=offset, results=[to_ip(r) for r in page.results])

    async def get(self, ip_id: int) -> IPAddress:
        return to_ip(await self.nb.get(f"{IPS}{ip_id}/"))

    async def _check_interface(self, interface_id: int) -> None:
        await self.nb.get(f"dcim/interfaces/{interface_id}/")  # NotFound if missing

    async def _check_duplicate(self, address: str, vrf_id: int | None) -> None:
        host = addr.parse_interface(address).ip
        # Match the host regardless of mask: 10.0.0.5/24 and 10.0.0.5/32 are the same address.
        # NetBox's `address` filter matches a bare host under any mask.
        existing = await self.nb.page(IPS, {"address": str(host), "vrf_id": vrf_filter(vrf_id)}, limit=50)
        for raw in existing.results:
            if addr.parse_interface(raw["address"]).ip == host:
                scope = "the selected VRF" if vrf_id is not None else "the global table"
                raise ValidationFailed(
                    f"{host} is already recorded in {scope} as {raw['address']}.",
                    fields={"address": ["This address already exists."]},
                )

    async def create(self, data: IPAddressCreate) -> IPAddress:
        try:
            iface = addr.parse_interface(data.address)
        except addr.AddressError as e:
            raise ValidationFailed(e.message, fields={"address": [e.message]}) from e

        # If the address sits inside a known prefix, make sure it is a usable host with the right mask.
        containing = await self.nb.page(
            PREFIXES, {"contains": str(iface.ip), "vrf_id": vrf_filter(data.vrf_id)}, limit=100
        )
        if containing.results:
            pfx = max(containing.results, key=lambda r: addr.parse_network(r["prefix"]).prefixlen)
            if (pfx.get("status") or {}).get("value") != "container":
                problem = addr.address_in_prefix(
                    iface, addr.parse_network(pfx["prefix"]), bool(pfx.get("is_pool"))
                )
                if problem:
                    raise ValidationFailed(problem, fields={"address": [problem]})
        await self._check_duplicate(str(iface), data.vrf_id)
        if data.interface_id is not None:
            await self._check_interface(data.interface_id)

        payload: dict[str, Any] = {
            "address": str(iface),
            "vrf": data.vrf_id,
            "status": data.status.value,
            "role": data.role.value if data.role else "",
            "dns_name": data.dns_name or "",
            "description": data.description or "",
        }
        if data.interface_id is not None:
            payload |= {"assigned_object_type": INTERFACE_TYPE, "assigned_object_id": data.interface_id}
        return to_ip(await self.nb.post(IPS, payload))

    async def allocate(self, prefix_id: int, data: AllocateFromPrefix) -> IPAddress:
        """Atomically take the next free address using NetBox's available-ips endpoint."""
        if data.interface_id is not None:
            await self._check_interface(data.interface_id)
        payload: dict[str, Any] = {
            "status": data.status.value,
            "description": data.description or "",
            "dns_name": data.dns_name or "",
        }
        if data.interface_id is not None:
            payload |= {"assigned_object_type": INTERFACE_TYPE, "assigned_object_id": data.interface_id}
        created = await self.nb.post(f"{PREFIXES}{prefix_id}/available-ips/", payload)
        if isinstance(created, list):
            created = created[0]
        if not created:
            raise ValidationFailed("No free addresses remain in this prefix.")
        return await self.get(int(created["id"]))

    async def update(self, ip_id: int, data: IPAddressUpdate) -> IPAddress:
        payload: dict[str, Any] = {}
        for field in data.model_fields_set:
            value = getattr(data, field)
            if field in ("status", "role"):
                value = value.value if hasattr(value, "value") else (value or "")
            elif value is None:
                value = ""
            payload[field] = value
        if payload:
            await self.nb.patch(f"{IPS}{ip_id}/", payload)
        return await self.get(ip_id)

    async def assign(self, ip_id: int, interface_id: int) -> IPAddress:
        await self._check_interface(interface_id)
        await self.nb.patch(
            f"{IPS}{ip_id}/", {"assigned_object_type": INTERFACE_TYPE, "assigned_object_id": interface_id}
        )
        return await self.get(ip_id)

    async def unassign(self, ip_id: int) -> IPAddress:
        current = await self.nb.get(f"{IPS}{ip_id}/")
        # NetBox refuses to unassign an address that is a device's primary IP; say so up front.
        obj = current.get("assigned_object") or {}
        device = obj.get("device")
        if device:
            dev = await self.nb.get(f"dcim/devices/{device['id']}/")
            for key in ("primary_ip4", "primary_ip6", "oob_ip"):
                if (dev.get(key) or {}).get("id") == ip_id:
                    raise ValidationFailed(
                        f"{current['address']} is the {key.replace('_', ' ')} of {dev.get('name')}. "
                        f"Change the device's primary IP in {self.nb.name} first."
                    )
        await self.nb.patch(f"{IPS}{ip_id}/", {"assigned_object_type": None, "assigned_object_id": None})
        return await self.get(ip_id)

    async def delete(self, ip_id: int) -> IPAddress:
        current = await self.get(ip_id)
        await self.nb.delete(f"{IPS}{ip_id}/")
        return current
