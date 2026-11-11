"""IP address, VLAN, device and dashboard endpoints against a NetBox TEST DOUBLE."""

import json

import httpx
import respx

from . import netbox_payloads as p


async def test_create_ip_rejects_broadcast(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    netbox_mock.get("ipam/prefixes/", params={"contains": "10.0.0.255"}).respond(
        200, json=p.page([p.prefix(1, "10.0.0.0/16", status="container"), p.prefix(2, "10.0.0.0/24")])
    )
    post = netbox_mock.post("ipam/ip-addresses/").respond(201, json={})
    r = await admin.post("/api/v1/ip-addresses", json={"address": "10.0.0.255/24"})
    assert r.status_code == 422 and "broadcast" in r.json()["error"]["message"]
    assert not post.called


async def test_create_ip_rejects_duplicate_host_any_mask(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    netbox_mock.get("ipam/prefixes/", params={"contains": "10.0.0.5"}).respond(200, json=p.page([]))
    netbox_mock.get("ipam/ip-addresses/", params={"address": "10.0.0.5"}).respond(
        200, json=p.page([p.ip(3, "10.0.0.50/24"), p.ip(4, "10.0.0.5/32")])
    )
    r = await admin.post("/api/v1/ip-addresses", json={"address": "10.0.0.5/24"})
    assert r.status_code == 422 and "10.0.0.5/32" in r.json()["error"]["message"]


async def test_create_ip_assigned_to_interface(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    iface = p.nested_interface()
    netbox_mock.get("ipam/prefixes/", params={"contains": "10.0.0.9"}).respond(
        200, json=p.page([p.prefix(2, "10.0.0.0/24")])
    )
    netbox_mock.get("ipam/ip-addresses/", params={"address": "10.0.0.9"}).respond(200, json=p.page([]))
    netbox_mock.get("dcim/interfaces/21/").respond(200, json=iface)
    post = netbox_mock.post("ipam/ip-addresses/").respond(201, json=p.ip(9, "10.0.0.9/24", interface=iface))
    r = await admin.post(
        "/api/v1/ip-addresses", json={"address": "10.0.0.9/24", "interface_id": 21, "status": "reserved"}
    )
    assert r.status_code == 201, r.text
    sent = json.loads(post.calls.last.request.content)
    assert sent["assigned_object_type"] == "dcim.interface" and sent["assigned_object_id"] == 21
    assert sent["status"] == "reserved"
    assigned = r.json()["ip_address"]["assigned"]
    assert assigned["interface_name"] == "eth0" and assigned["device"]["name"] == "core-sw1"


async def test_allocate_next_free_uses_netbox_available_ips(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    post = netbox_mock.post("ipam/prefixes/2/available-ips/").respond(201, json=p.ip(15, "10.0.0.3/24"))
    netbox_mock.get("ipam/ip-addresses/15/").respond(200, json=p.ip(15, "10.0.0.3/24", description="web01"))
    r = await admin.post("/api/v1/prefixes/2/allocate-ip", json={"description": "web01"})
    assert r.status_code == 201 and r.json()["ip_address"]["address"] == "10.0.0.3/24"
    assert json.loads(post.calls.last.request.content)["description"] == "web01"


async def test_release_blocked_for_primary_ip(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    iface = p.nested_interface()
    netbox_mock.get("ipam/ip-addresses/9/").respond(200, json=p.ip(9, "10.0.0.9/24", interface=iface))
    netbox_mock.get("dcim/devices/5/").respond(
        200, json={"id": 5, "name": "core-sw1", "primary_ip4": {"id": 9}}
    )
    patch = netbox_mock.patch("ipam/ip-addresses/9/").respond(200, json={})
    r = await admin.post("/api/v1/ip-addresses/9/release")
    assert r.status_code == 422 and "primary ip4" in r.json()["error"]["message"]
    assert not patch.called


async def test_release_unassigns(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    iface = p.nested_interface()
    netbox_mock.get("ipam/ip-addresses/9/").mock(
        side_effect=[
            httpx.Response(200, json=p.ip(9, "10.0.0.9/24", interface=iface)),
            httpx.Response(200, json=p.ip(9, "10.0.0.9/24")),
        ]
    )
    netbox_mock.get("dcim/devices/5/").respond(200, json={"id": 5, "name": "core-sw1", "primary_ip4": None})
    patch = netbox_mock.patch("ipam/ip-addresses/9/").respond(200, json={})
    r = await admin.post("/api/v1/ip-addresses/9/release")
    assert r.status_code == 200 and r.json()["ip_address"]["assigned"] is None
    assert json.loads(patch.calls.last.request.content) == {
        "assigned_object_type": None,
        "assigned_object_id": None,
    }


async def test_vlan_crud_and_group_uniqueness(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    group = {"id": 3, "name": "campus", "slug": "campus"}
    netbox_mock.get("ipam/vlans/", params={"group_id": "3"}).respond(
        200, json=p.page([p.vlan(1, 10, "users", group=group)])
    )
    r = await admin.post("/api/v1/vlans", json={"vid": 10, "name": "voice", "group_id": 3})
    assert r.status_code == 422 and r.json()["error"]["fields"]["vid"]

    post = netbox_mock.post("ipam/vlans/").respond(201, json=p.vlan(2, 20, "voice", group=group))
    r = await admin.post("/api/v1/vlans", json={"vid": 20, "name": "voice", "group_id": 3})
    assert r.status_code == 201 and r.json()["vlan"]["vid"] == 20
    sent = json.loads(post.calls.last.request.content)
    assert sent["vid"] == 20 and sent["group"] == 3 and sent["site"] is None

    r = await admin.post("/api/v1/vlans", json={"vid": 4095, "name": "bad"})
    assert r.status_code == 422


async def test_device_detail_collects_vlans(reader: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    netbox_mock.get("dcim/devices/5/").respond(
        200,
        json={
            "id": 5,
            "name": "core-sw1",
            "status": {"value": "active", "label": "Active"},
            "role": {"id": 1, "name": "Core switch", "slug": "core"},
            "device_type": {
                "id": 2,
                "model": "C9300",
                "manufacturer": {"id": 1, "name": "Cisco", "slug": "cisco"},
            },
            "site": {"id": 1, "name": "HQ", "slug": "hq"},
            "primary_ip": {"id": 9, "address": "10.0.0.2/24"},
            "interface_count": 2,
            "serial": "FOC123",
        },
    )
    netbox_mock.get("dcim/interfaces/").respond(
        200,
        json=p.page(
            [
                {
                    "id": 21,
                    "name": "Gi1/0/1",
                    "enabled": True,
                    "mode": {"value": "access", "label": "Access"},
                    "untagged_vlan": {"id": 1, "vid": 10, "name": "users"},
                    "tagged_vlans": [],
                },
                {
                    "id": 22,
                    "name": "Gi1/0/48",
                    "enabled": True,
                    "mode": {"value": "tagged", "label": "Tagged"},
                    "untagged_vlan": None,
                    "tagged_vlans": [
                        {"id": 1, "vid": 10, "name": "users"},
                        {"id": 2, "vid": 20, "name": "voice"},
                    ],
                },
            ]
        ),
    )
    netbox_mock.get("ipam/ip-addresses/", params={"device_id": "5"}).respond(
        200, json=p.page([p.ip(9, "10.0.0.2/24")])
    )
    r = await reader.get("/api/v1/devices/5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["device_type"]["manufacturer"]["name"] == "Cisco" and d["primary_ip"] == "10.0.0.2/24"
    assert [v["vid"] for v in d["vlans"]] == [10, 20]
    assert len(d["interfaces"]) == 2 and d["ip_addresses"][0]["address"] == "10.0.0.2/24"
    assert netbox_mock.calls[0].request.url.params["omit"] == "config_context"


async def test_interface_vlans_access_mode(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    patch = netbox_mock.patch("dcim/interfaces/21/").respond(200, json={})
    netbox_mock.get("dcim/interfaces/21/").respond(
        200,
        json={
            "id": 21,
            "name": "Gi1/0/1",
            "mode": {"value": "access"},
            "untagged_vlan": {"id": 1, "vid": 10, "name": "users"},
        },
    )
    r = await admin.put(
        "/api/v1/interfaces/21/vlans", json={"mode": "access", "untagged_vlan_id": 1, "tagged_vlan_ids": [5]}
    )
    assert r.status_code == 200
    assert json.loads(patch.calls.last.request.content) == {
        "mode": "access",
        "untagged_vlan": 1,
        "tagged_vlans": [],
    }


async def test_dashboard_falls_back_to_lanvex_audit(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    netbox_mock.get("ipam/prefixes/", params={"limit": "1"}).respond(200, json=p.page([], count=4))
    netbox_mock.get("ipam/ip-addresses/", params={"limit": "1"}).respond(200, json=p.page([], count=10))
    for path in ("ipam/vlans/", "dcim/devices/", "ipam/vrfs/", "dcim/sites/"):
        netbox_mock.get(path, params={"limit": "1"}).respond(200, json=p.page([], count=2))
    netbox_mock.get("ipam/prefixes/", params={"limit": "60"}).respond(200, json=p.page([]))
    netbox_mock.get("core/object-changes/").respond(403, json={"detail": "denied"})
    r = await admin.get("/api/v1/dashboard/overview")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["counts"]["prefixes"] == 4 and o["counts"]["devices"] == 2
    assert o["recent_changes_available"] is False and "change log" in o["recent_changes_message"]
