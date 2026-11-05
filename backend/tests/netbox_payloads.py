"""TEST DOUBLES. Payload builders shaped like NetBox 4.7 REST responses.

Field names follow the serializers in netbox-community/netbox v4.7.4
(ipam/api/serializers_/ip.py, vlans.py; dcim/api/serializers_/devices.py, device_components.py).
These are used only by unit tests; they do not prove compatibility with a live NetBox.
"""

import ipaddress
from typing import Any

NB = "https://netbox.test"


def page(results: list[dict[str, Any]], count: int | None = None) -> dict[str, Any]:
    return {
        "count": len(results) if count is None else count,
        "next": None,
        "previous": None,
        "results": results,
    }


def vrf(id: int = 1, name: str = "prod", rd: str | None = "65000:1") -> dict[str, Any]:
    return {
        "id": id,
        "url": f"{NB}/api/ipam/vrfs/{id}/",
        "display": name,
        "name": name,
        "rd": rd,
        "description": "",
    }


def tag(id: int = 9, slug: str = "lanvex-gateway", name: str = "Lanvex gateway") -> dict[str, Any]:
    return {
        "id": id,
        "url": f"{NB}/api/extras/tags/{id}/",
        "display": name,
        "name": name,
        "slug": slug,
        "color": "2563eb",
    }


def prefix(
    id: int,
    cidr: str,
    *,
    status: str = "active",
    description: str = "",
    vrf_obj: dict[str, Any] | None = None,
    depth: int = 0,
    children: int = 0,
    is_pool: bool = False,
    mark_utilized: bool = False,
    vlan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    net = ipaddress.ip_network(cidr)
    return {
        "id": id,
        "url": f"{NB}/api/ipam/prefixes/{id}/",
        "display_url": f"{NB}/ipam/prefixes/{id}/",
        "display": cidr,
        "family": {"value": net.version, "label": f"IPv{net.version}"},
        "prefix": cidr,
        "vrf": vrf_obj,
        "scope_type": None,
        "scope_id": None,
        "scope": None,
        "tenant": None,
        "vlan": vlan,
        "status": {"value": status, "label": status.title()},
        "role": None,
        "is_pool": is_pool,
        "mark_utilized": mark_utilized,
        "description": description,
        "owner": None,
        "comments": "",
        "tags": [],
        "custom_fields": {},
        "created": "2026-11-01T10:00:00.000000Z",
        "last_updated": "2026-11-01T10:00:00.000000Z",
        "children": children,
        "_depth": depth,
    }


def ip(
    id: int,
    address: str,
    *,
    status: str = "active",
    vrf_obj: dict[str, Any] | None = None,
    tags: list[dict[str, Any]] | None = None,
    interface: dict[str, Any] | None = None,
    description: str = "",
) -> dict[str, Any]:
    v = ipaddress.ip_interface(address).version
    return {
        "id": id,
        "url": f"{NB}/api/ipam/ip-addresses/{id}/",
        "display_url": f"{NB}/ipam/ip-addresses/{id}/",
        "display": address,
        "family": {"value": v, "label": f"IPv{v}"},
        "address": address,
        "vrf": vrf_obj,
        "tenant": None,
        "status": {"value": status, "label": status.title()},
        "role": None,
        "assigned_object_type": "dcim.interface" if interface else None,
        "assigned_object_id": interface["id"] if interface else None,
        "assigned_object": interface,
        "nat_inside": None,
        "nat_outside": [],
        "dns_name": "",
        "description": description,
        "owner": None,
        "comments": "",
        "tags": tags or [],
        "custom_fields": {},
        "created": "2026-11-01T10:00:00Z",
        "last_updated": "2026-11-01T10:00:00Z",
    }


def nested_interface(
    id: int = 21, name: str = "eth0", device_id: int = 5, device_name: str = "core-sw1"
) -> dict[str, Any]:
    return {
        "id": id,
        "url": f"{NB}/api/dcim/interfaces/{id}/",
        "display": name,
        "device": {
            "id": device_id,
            "url": f"{NB}/api/dcim/devices/{device_id}/",
            "display": device_name,
            "name": device_name,
            "description": "",
        },
        "name": name,
        "description": "",
        "cable": None,
        "_occupied": False,
    }


def vlan(id: int, vid: int, name: str, *, group: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": id,
        "url": f"{NB}/api/ipam/vlans/{id}/",
        "display_url": f"{NB}/ipam/vlans/{id}/",
        "display": f"{name} ({vid})",
        "site": None,
        "group": group,
        "vid": vid,
        "name": name,
        "tenant": None,
        "status": {"value": "active", "label": "Active"},
        "role": None,
        "description": "",
        "qinq_role": None,
        "qinq_svlan": None,
        "owner": None,
        "comments": "",
        "l2vpn_termination": None,
        "tags": [],
        "custom_fields": {},
        "created": "2026-11-01T10:00:00Z",
        "last_updated": "2026-11-01T10:00:00Z",
        "prefix_count": 0,
    }


def validation_error(**fields: list[str]) -> dict[str, Any]:
    return dict(fields)
