"""The first vertical slice: list → validate → create → persist → read back. NetBox is a TEST DOUBLE."""

import json

import httpx
import respx
from sqlalchemy import select

from lanvex.audit.models import AuditEvent
from lanvex.core.db import Database

from . import netbox_payloads as p


def _ip_count(router: respx.MockRouter, count: int = 0) -> respx.Route:
    return router.get("ipam/ip-addresses/", params={"limit": "1"}).respond(200, json=p.page([], count=count))


async def test_list_prefixes_maps_and_computes_utilization(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    prod = p.vrf()
    netbox_mock.get("ipam/prefixes/", params={"ordering": "vrf,prefix"}).respond(
        200,
        json=p.page(
            [
                p.prefix(1, "10.0.0.0/16", status="container", description="Campus"),
                p.prefix(2, "10.0.1.0/24", description="Office LAN", depth=1, vrf_obj=prod),
            ]
        ),
    )
    # Container: child prefixes within it (two /24s → 512 of 65536)
    netbox_mock.get("ipam/prefixes/", params={"within": "10.0.0.0/16"}).respond(
        200, json=p.page([p.prefix(2, "10.0.1.0/24"), p.prefix(3, "10.0.2.0/24")])
    )
    ips = netbox_mock.get("ipam/ip-addresses/", params={"parent": "10.0.1.0/24", "vrf_id": "1"}).respond(
        200, json=p.page([], count=127)
    )
    netbox_mock.get("ipam/ip-ranges/").respond(200, json=p.page([]))

    r = await admin.get("/api/v1/prefixes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    campus, office = body["results"]
    assert campus["name"] == "Campus" and campus["status"]["value"] == "container"
    assert campus["utilization"]["basis"] == "child_prefixes"
    assert campus["utilization"]["used"] == 512
    assert office["vrf"] == {"id": 1, "name": "prod", "slug": None}
    assert office["utilization"] == {"percent": 50.0, "used": 127, "total": 254, "basis": "ip_addresses"}
    assert ips.called
    # Raw NetBox internals never leak to the UI contract.
    assert "_depth" not in r.text and "custom_fields" not in r.text


async def test_global_container_counts_children_in_all_vrfs(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    netbox_mock.get("ipam/prefixes/", params={"ordering": "vrf,prefix"}).respond(
        200, json=p.page([p.prefix(1, "10.0.0.0/8", status="container")])
    )
    within = netbox_mock.get("ipam/prefixes/", params={"within": "10.0.0.0/8"}).respond(200, json=p.page([]))
    await admin.get("/api/v1/prefixes")
    assert "vrf_id" not in within.calls.last.request.url.params  # NetBox semantics for global containers


async def test_validate_reports_host_bits(admin: httpx.AsyncClient) -> None:
    r = await admin.post("/api/v1/prefixes/validate", json={"prefix": "10.0.0.7/24"})
    v = r.json()
    assert v["valid"] is False and v["normalized"] == "10.0.0.0/24"
    assert v["findings"][0]["code"] == "invalid_prefix"


async def test_validate_detects_duplicate_and_parent(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    netbox_mock.get("ipam/prefixes/", params={"contains": "10.0.1.0/24"}).respond(
        200,
        json=p.page(
            [p.prefix(1, "10.0.0.0/16", status="container", description="Campus"), p.prefix(2, "10.0.1.0/24")]
        ),
    )
    netbox_mock.get("ipam/prefixes/", params={"within": "10.0.1.0/24"}).respond(200, json=p.page([]))
    v = (await admin.post("/api/v1/prefixes/validate", json={"prefix": "10.0.1.0/24"})).json()
    codes = {f["code"]: f["level"] for f in v["findings"]}
    assert codes["duplicate_prefix"] == "error"
    assert codes["has_parent"] == "info"
    assert v["valid"] is False
    assert {r["relation"] for r in v["related"]} == {"duplicate", "parent"}
    assert v["usable"] == 254 and v["suggested_gateway"] == "10.0.1.1/24"


async def test_validate_ipv6_and_children(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    netbox_mock.get("ipam/prefixes/", params={"contains": "2001:db8::/32"}).respond(200, json=p.page([]))
    netbox_mock.get("ipam/prefixes/", params={"within": "2001:db8::/32"}).respond(
        200, json=p.page([p.prefix(5, "2001:db8:1::/48")], count=3)
    )
    v = (await admin.post("/api/v1/prefixes/validate", json={"prefix": "2001:db8::/32"})).json()
    assert v["valid"] is True and v["family"] == 6
    assert any(f["code"] == "has_children" and "3 existing" in f["message"] for f in v["findings"])


def _empty_overlap(router: respx.MockRouter, cidr: str) -> None:
    router.get("ipam/prefixes/", params={"contains": cidr}).respond(200, json=p.page([]))
    router.get("ipam/prefixes/", params={"within": cidr}).respond(200, json=p.page([]))


def _readback(router: respx.MockRouter, created: dict, cidr: str, gateway: dict | None = None) -> None:
    router.get(f"ipam/prefixes/{created['id']}/").respond(200, json=created)
    router.get("ipam/ip-addresses/", params={"tag": "lanvex-gateway"}).respond(
        200, json=p.page([gateway] if gateway else [])
    )
    router.get("ipam/ip-addresses/", params={"parent": cidr, "limit": "1", "brief": "true"}).respond(
        200, json=p.page([], count=1 if gateway else 0)
    )
    router.get("ipam/ip-ranges/").respond(200, json=p.page([]))


async def test_create_prefix_persists_via_netbox_and_audits(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter, database: Database
) -> None:
    cidr = "10.20.0.0/24"
    _empty_overlap(netbox_mock, cidr)
    created = p.prefix(42, cidr, description="Lab network")
    post = netbox_mock.post("ipam/prefixes/").respond(201, json=created)
    _readback(netbox_mock, created, cidr)

    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr, "name": "Lab network", "status": "active"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "documented"  # recorded in NetBox, not deployed anywhere
    assert body["prefix"]["id"] == 42 and body["prefix"]["name"] == "Lab network"

    sent = json.loads(post.calls.last.request.content)
    assert sent["prefix"] == cidr and sent["description"] == "Lab network" and sent["vrf"] is None
    assert sent["status"] == "active"

    async with database.sessionmaker() as s:
        ev = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "prefix.create"))).one()
    assert ev.outcome == "success" and ev.target_id == "42" and ev.actor_username == "admin"


async def test_create_rejects_duplicate_without_calling_netbox_write(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter, database: Database
) -> None:
    cidr = "10.20.0.0/24"
    netbox_mock.get("ipam/prefixes/", params={"contains": cidr}).respond(
        200, json=p.page([p.prefix(1, cidr)])
    )
    netbox_mock.get("ipam/prefixes/", params={"within": cidr}).respond(200, json=p.page([]))
    post = netbox_mock.post("ipam/prefixes/").respond(201, json={})
    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr})
    assert r.status_code == 422 and "already exists" in r.json()["error"]["message"]
    assert not post.called
    async with database.sessionmaker() as s:
        ev = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "prefix.create"))).one()
    assert ev.outcome == "failure"


async def test_create_with_gateway_creates_tagged_ip(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    cidr = "10.30.0.0/24"
    _empty_overlap(netbox_mock, cidr)
    created = p.prefix(7, cidr)
    netbox_mock.post("ipam/prefixes/").respond(201, json=created)
    netbox_mock.get("extras/tags/").respond(200, json=p.page([]))
    tag_post = netbox_mock.post("extras/tags/").respond(201, json=p.tag(9))
    netbox_mock.get("ipam/ip-addresses/", params={"address": "10.30.0.1/24"}).respond(200, json=p.page([]))
    ip_post = netbox_mock.post("ipam/ip-addresses/").respond(
        201, json=p.ip(100, "10.30.0.1/24", tags=[p.tag(9)])
    )
    _readback(netbox_mock, created, cidr, gateway=p.ip(100, "10.30.0.1/24", tags=[p.tag(9)]))

    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr, "gateway": "10.30.0.1/24"})
    assert r.status_code == 201, r.text
    assert r.json()["warnings"] == []
    assert r.json()["prefix"]["gateway"] == {"ip_address_id": 100, "address": "10.30.0.1/24"}
    assert json.loads(tag_post.calls.last.request.content)["slug"] == "lanvex-gateway"
    assert json.loads(ip_post.calls.last.request.content) == {
        "address": "10.30.0.1/24",
        "vrf": None,
        "status": "active",
        "description": "Gateway",
        "tags": [9],
    }


async def test_invalid_gateway_blocks_create(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    cidr = "10.30.0.0/24"
    _empty_overlap(netbox_mock, cidr)
    post = netbox_mock.post("ipam/prefixes/").respond(201, json={})
    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr, "gateway": "10.30.0.255/24"})
    assert r.status_code == 422 and "broadcast" in r.json()["error"]["fields"]["gateway"][0]
    assert not post.called


async def test_gateway_failure_after_create_is_reported_not_hidden(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    cidr = "10.40.0.0/24"
    _empty_overlap(netbox_mock, cidr)
    created = p.prefix(8, cidr)
    netbox_mock.post("ipam/prefixes/").respond(201, json=created)
    netbox_mock.get("extras/tags/").respond(403, json={"detail": "You do not have permission"})
    _readback(netbox_mock, created, cidr)
    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr, "gateway": "10.40.0.1/24"})
    assert r.status_code == 201
    assert r.json()["warnings"] and "gateway failed" in r.json()["warnings"][0]


async def test_netbox_validation_error_surfaces_fields(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    cidr = "10.50.0.0/24"
    _empty_overlap(netbox_mock, cidr)
    netbox_mock.post("ipam/prefixes/").respond(
        400, json={"vlan": ["Related object not found using the provided numeric ID: 99"]}
    )
    r = await admin.post("/api/v1/prefixes", json={"prefix": cidr, "vlan_id": 99})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "netbox_validation"
    assert "vlan" in r.json()["error"]["fields"]


async def test_update_sends_only_changed_fields(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    cidr = "10.60.0.0/24"
    current = p.prefix(11, cidr, description="Old")
    _readback(netbox_mock, current, cidr)
    patch = netbox_mock.patch("ipam/prefixes/11/").respond(200, json=current)
    r = await admin.patch("/api/v1/prefixes/11", json={"name": "New name", "vlan_id": None})
    assert r.status_code == 200, r.text
    assert json.loads(patch.calls.last.request.content) == {"description": "New name", "vlan": None}


async def test_delete_requires_exact_confirmation(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    cidr = "10.70.0.0/24"
    _readback(netbox_mock, p.prefix(12, cidr), cidr)
    delete = netbox_mock.delete("ipam/prefixes/12/").respond(204)
    r = await admin.delete("/api/v1/prefixes/12", params={"confirm": "10.70.0.0/25"})
    assert r.status_code == 422 and not delete.called
    r = await admin.delete("/api/v1/prefixes/12", params={"confirm": cidr})
    assert r.status_code == 200 and delete.called


async def test_netbox_down_returns_502(admin: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    netbox_mock.get("ipam/prefixes/").mock(side_effect=httpx.ConnectError("refused"))
    r = await admin.get("/api/v1/prefixes")
    assert r.status_code == 502 and r.json()["error"]["code"] == "netbox_unreachable"
    assert r.json()["error"]["request_id"]


async def test_container_has_no_gateway_and_child_gateways_are_ignored(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    """Regression: a /16 must not report the tagged gateway of a /24 inside it."""
    cidr = "10.10.0.0/16"
    active = p.prefix(30, cidr, status="active")
    netbox_mock.get("ipam/prefixes/30/").respond(200, json=active)
    gw_lookup = netbox_mock.get("ipam/ip-addresses/", params={"tag": "lanvex-gateway"}).respond(
        200, json=p.page([p.ip(5, "10.10.10.1/24", tags=[p.tag()])])
    )
    netbox_mock.get("ipam/ip-addresses/", params={"limit": "1", "brief": "true"}).respond(
        200, json=p.page([], count=1)
    )
    netbox_mock.get("ipam/ip-ranges/").respond(200, json=p.page([]))
    r = await admin.get("/api/v1/prefixes/30")
    assert r.json()["gateway"] is None
    assert gw_lookup.calls.last.request.url.params["mask_length"] == "16"


async def test_prefix_detail_works_before_the_gateway_tag_exists(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    """Regression: on a fresh NetBox the lanvex-gateway tag does not exist, and NetBox rejects the filter."""
    netbox_mock.get("ipam/prefixes/31/").respond(200, json=p.prefix(31, "10.31.0.0/24", status="active"))
    netbox_mock.get("ipam/ip-addresses/", params={"tag": "lanvex-gateway"}).respond(
        400, json={"tag": ["Select a valid choice. lanvex-gateway is not one of the available choices."]}
    )
    netbox_mock.get("ipam/ip-addresses/", params={"limit": "1", "brief": "true"}).respond(
        200, json=p.page([], count=0)
    )
    netbox_mock.get("ipam/ip-ranges/").respond(200, json=p.page([]))
    r = await admin.get("/api/v1/prefixes/31")
    assert r.status_code == 200, r.text
    assert r.json()["gateway"] is None
