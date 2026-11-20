"""Integration tests against a REAL, DISPOSABLE NetBox. Skipped unless explicitly configured.

    LANVEX_TEST_NETBOX_URL=http://localhost:8000 \
    LANVEX_TEST_NETBOX_TOKEN=nbt_...            # the token Lanvex uses (try a least-privilege one)
    LANVEX_TEST_NETBOX_ADMIN_TOKEN=nbt_...      # optional: creates/removes the test site, device, VRF
    LANVEX_TEST_NETBOX_READONLY_TOKEN=nbt_...   # optional: a write-disabled token
    uv run pytest -m netbox

Everything goes through the Lanvex HTTP API with a real NetBox client; results are then checked
directly in NetBox. Objects are created under 198.51.100.0/24 (TEST-NET-2), 2001:db8:4c56::/48,
VLAN IDs 3901-3903 and names starting "lanvex-it", and removed afterwards. Never point this at a
production NetBox.
"""

import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from lanvex.audit.models import AuditEvent
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.core.errors import NotFound
from lanvex.core.ratelimit import MemoryRateLimiter
from lanvex.integrations.linux.facts import Route
from lanvex.ipam.prefixes import PrefixService, to_prefix
from lanvex.main import create_app
from lanvex.netbox.client import NetBoxClient

from . import test_server_history
from .conftest import login
from .test_server_history import FakeServer
from .test_servers_api import body

fake = test_server_history.fake

URL = os.environ.get("LANVEX_TEST_NETBOX_URL", "")
TOKEN = os.environ.get("LANVEX_TEST_NETBOX_TOKEN", "")
ADMIN_TOKEN = os.environ.get("LANVEX_TEST_NETBOX_ADMIN_TOKEN") or TOKEN
READONLY_TOKEN = os.environ.get("LANVEX_TEST_NETBOX_READONLY_TOKEN", "")

V4 = "198.51.100.0/24"
V6 = "2001:db8:4c56::/48"
VIDS = (3901, 3902, 3903)

pytestmark = [
    pytest.mark.netbox,
    pytest.mark.skipif(not (URL and TOKEN), reason="LANVEX_TEST_NETBOX_URL / _TOKEN not set"),
]


# ---- NetBox-side fixtures (created with the admin token, not through Lanvex) -------------


async def _purge(nb: NetBoxClient) -> None:
    """Remove anything this suite may have left behind, in dependency order."""
    for parent in (V4, V6):
        for rng in await nb.list_all("ipam/ip-ranges/", {"parent": parent}):
            await nb.delete(f"ipam/ip-ranges/{rng['id']}/")
        for ip in await nb.list_all("ipam/ip-addresses/", {"parent": parent}):
            await nb.delete(f"ipam/ip-addresses/{ip['id']}/")
    for parent in (V4, V6):
        prefixes = await nb.list_all("ipam/prefixes/", {"within_include": parent})
        for p in sorted(prefixes, key=lambda p: -int(p["prefix"].split("/")[1])):
            await nb.delete(f"ipam/prefixes/{p['id']}/")
    for vid in VIDS:
        for v in await nb.list_all("ipam/vlans/", {"vid": vid, "q": "lanvex-it"}):
            await nb.delete(f"ipam/vlans/{v['id']}/")
    for path in (
        "extras/config-contexts/",
        "dcim/devices/",
        "ipam/vlan-groups/",
        "dcim/device-types/",
        "dcim/device-roles/",
        "dcim/manufacturers/",
        "dcim/sites/",
        "ipam/vrfs/",
    ):
        for obj in await nb.list_all(path, {"q": "lanvex-it"}):
            await nb.delete(f"{path}{obj['id']}/")


@pytest.fixture(scope="module")
async def nb_admin() -> AsyncIterator[NetBoxClient]:
    client = NetBoxClient(URL, ADMIN_TOKEN)
    yield client
    await client.aclose()


@pytest.fixture(scope="module")
async def infra(nb_admin: NetBoxClient) -> AsyncIterator[dict[str, Any]]:
    nb = nb_admin
    await _purge(nb)
    tag = secrets.token_hex(3)
    site = await nb.post(
        "dcim/sites/", {"name": f"lanvex-it site {tag}", "slug": f"lanvex-it-{tag}", "status": "active"}
    )
    mfr = await nb.post(
        "dcim/manufacturers/", {"name": f"lanvex-it vendor {tag}", "slug": f"lanvex-it-vendor-{tag}"}
    )
    dtype = await nb.post(
        "dcim/device-types/",
        {"manufacturer": mfr["id"], "model": f"lanvex-it switch {tag}", "slug": f"lanvex-it-switch-{tag}"},
    )
    role = await nb.post(
        "dcim/device-roles/",
        {"name": f"lanvex-it role {tag}", "slug": f"lanvex-it-role-{tag}", "color": "2e6be6"},
    )
    device = await nb.post(
        "dcim/devices/",
        {
            "name": f"lanvex-it-sw-{tag}",
            "device_type": dtype["id"],
            "role": role["id"],
            "site": site["id"],
            "status": "active",
        },
    )
    ifaces = [
        await nb.post("dcim/interfaces/", {"device": device["id"], "name": name, "type": kind})
        for name, kind in (("eth0", "1000base-t"), ("eth1", "1000base-t"), ("mgmt0", "1000base-t"))
    ]
    vrf = await nb.post("ipam/vrfs/", {"name": f"lanvex-it vrf {tag}", "enforce_unique": True})
    group = await nb.post(
        "ipam/vlan-groups/", {"name": f"lanvex-it group {tag}", "slug": f"lanvex-it-group-{tag}"}
    )
    data = {"site": site, "device": device, "ifaces": ifaces, "vrf": vrf, "group": group, "tag": tag}
    yield data
    await _purge(nb)


# ---- Lanvex app wired to the real NetBox --------------------------------------------------


def _app(database: Database, token: str):  # type: ignore[no-untyped-def]
    application = create_app(get_settings())
    application.state.db = database
    application.state.redis = None
    application.state.rate_limiter = MemoryRateLimiter()
    application.state.netbox = NetBoxClient(URL, token, max_retries=1)
    return application


@pytest.fixture
async def api(database: Database, users: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    application = _app(database, TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://lanvex.test"
    ) as c:
        await login(c, "admin", users["admin"])
        yield c
    await application.state.netbox.aclose()


def ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text}"
    return r.json() if r.content else None


# ---- tests --------------------------------------------------------------------------------


async def test_connection_status(api: httpx.AsyncClient) -> None:
    s = ok(await api.get("/api/v1/system/netbox"))
    assert s["reachable"] and s["authenticated"] and s["supported"], s
    assert s["version"].startswith("4.")


async def test_prefix_lifecycle(
    api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any]
) -> None:
    nb = nb_admin
    container = ok(
        await api.post(
            "/api/v1/prefixes", json={"prefix": V4, "name": "lanvex-it container", "status": "container"}
        ),
        201,
    )["prefix"]
    assert container["gateway"] is None

    v = ok(await api.post("/api/v1/prefixes/validate", json={"prefix": "198.51.100.70/26"}))
    assert v["valid"] is False and v["normalized"] == "198.51.100.64/26"

    v = ok(
        await api.post(
            "/api/v1/prefixes/validate", json={"prefix": "198.51.100.64/26", "gateway": "198.51.100.65/26"}
        )
    )
    assert v["valid"] and any(f["code"] == "has_parent" and V4 in f["message"] for f in v["findings"])
    assert v["usable"] == 62 and v["suggested_gateway"] == "198.51.100.65/26"

    vlan = ok(
        await api.post(
            "/api/v1/vlans",
            json={"vid": VIDS[0], "name": "lanvex-it users", "group_id": infra["group"]["id"]},
        ),
        201,
    )["vlan"]

    created = ok(
        await api.post(
            "/api/v1/prefixes",
            json={
                "prefix": "198.51.100.64/26",
                "name": "lanvex-it users",
                "gateway": "198.51.100.65/26",
                "vlan_id": vlan["id"],
            },
        ),
        201,
    )
    assert created["state"] == "documented" and created["warnings"] == []
    child = created["prefix"]
    assert child["gateway"]["address"] == "198.51.100.65/26"
    assert child["vlan"]["vid"] == VIDS[0]
    assert child["utilization"] == {
        "percent": pytest.approx(100 / 62),
        "used": 1,
        "total": 62,
        "basis": "ip_addresses",
    }

    # Checked in NetBox, not through Lanvex.
    raw = await nb.get(f"ipam/prefixes/{child['id']}/")
    assert raw["description"] == "lanvex-it users" and raw["vlan"]["id"] == vlan["id"] and raw["vrf"] is None
    gw = (await nb.page("ipam/ip-addresses/", {"address": "198.51.100.65/26"})).results[0]
    assert "lanvex-gateway" in [t["slug"] for t in gw["tags"]]

    parent = ok(await api.get(f"/api/v1/prefixes/{container['id']}"))
    assert parent["utilization"]["basis"] == "child_prefixes" and parent["utilization"]["used"] == 64
    assert parent["gateway"] is None  # a child's gateway is not the container's

    listing = ok(await api.get("/api/v1/prefixes", params={"within": V4, "global_only": True}))
    depths = {p["prefix"]: p["depth"] for p in listing["results"]}
    assert depths == {V4: 0, "198.51.100.64/26": 1}

    dup = await api.post("/api/v1/prefixes", json={"prefix": "198.51.100.64/26"})
    assert dup.status_code == 422 and "already exists" in dup.json()["error"]["message"]
    assert await nb.count("ipam/prefixes/", {"prefix": "198.51.100.64/26", "vrf_id": "null"}) == 1

    # The same network is allowed in another VRF; duplicates are VRF-scoped.
    in_vrf = ok(
        await api.post("/api/v1/prefixes", json={"prefix": "198.51.100.64/26", "vrf_id": infra["vrf"]["id"]}),
        201,
    )["prefix"]
    assert in_vrf["vrf"]["id"] == infra["vrf"]["id"]
    v = ok(
        await api.post(
            "/api/v1/prefixes/validate", json={"prefix": "198.51.100.64/26", "vrf_id": infra["vrf"]["id"]}
        )
    )
    assert any(f["code"] == "duplicate_prefix" and "selected VRF" in f["message"] for f in v["findings"])

    free = ok(await api.get(f"/api/v1/prefixes/{child['id']}/available-ips", params={"limit": 2}))
    assert [a["address"] for a in free] == ["198.51.100.66/26", "198.51.100.67/26"]
    free_space = {
        a["prefix"] for a in ok(await api.get(f"/api/v1/prefixes/{container['id']}/available-prefixes"))
    }
    assert free_space == {"198.51.100.0/26", "198.51.100.128/25"}

    taken = ok(
        await api.post(f"/api/v1/prefixes/{child['id']}/allocate-ip", json={"description": "lanvex-it host"}),
        201,
    )
    assert taken["ip_address"]["address"] == "198.51.100.66/26"

    updated = ok(
        await api.patch(
            f"/api/v1/prefixes/{child['id']}",
            json={"name": "lanvex-it renamed", "status": "reserved", "gateway": "198.51.100.126/26"},
        )
    )
    assert updated["warnings"] == []
    assert (
        updated["prefix"]["name"] == "lanvex-it renamed"
        and updated["prefix"]["status"]["value"] == "reserved"
    )
    assert updated["prefix"]["gateway"]["address"] == "198.51.100.126/26"
    old_gw = await nb.get(f"ipam/ip-addresses/{gw['id']}/")
    assert "lanvex-gateway" not in [t["slug"] for t in old_gw["tags"]]  # marker moved, record kept

    cleared = ok(await api.patch(f"/api/v1/prefixes/{child['id']}", json={"clear_gateway": True}))
    assert cleared["prefix"]["gateway"] is None

    impact = ok(await api.get(f"/api/v1/prefixes/{child['id']}/delete-impact"))
    assert impact["ip_addresses"] == 3 and impact["child_prefixes"] == 0

    assert (
        await api.delete(f"/api/v1/prefixes/{child['id']}", params={"confirm": "198.51.100.64/27"})
    ).status_code == 422
    ok(await api.delete(f"/api/v1/prefixes/{child['id']}", params={"confirm": "198.51.100.64/26"}))
    with pytest.raises(NotFound):
        await nb.get(f"ipam/prefixes/{child['id']}/")


async def test_ipv6_prefix(api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any]) -> None:
    v = ok(await api.post("/api/v1/prefixes/validate", json={"prefix": "2001:db8:4c56:10::1/64"}))
    assert v["valid"] is False and v["normalized"] == "2001:db8:4c56:10::/64"
    p = ok(
        await api.post(
            "/api/v1/prefixes",
            json={"prefix": "2001:0db8:4c56:0010::/64", "gateway": "2001:db8:4c56:10::1/64"},
        ),
        201,
    )
    pre = p["prefix"]
    assert pre["prefix"] == "2001:db8:4c56:10::/64" and pre["family"] == 6
    assert pre["gateway"]["address"] == "2001:db8:4c56:10::1/64"
    assert pre["utilization"]["total"] == 2**64 and pre["utilization"]["used"] == 1
    nxt = ok(await api.post(f"/api/v1/prefixes/{pre['id']}/allocate-ip", json={}), 201)
    assert nxt["ip_address"]["address"] == "2001:db8:4c56:10::2/64"
    dup = await api.post("/api/v1/ip-addresses", json={"address": "2001:0DB8:4C56:0010:0000::2/64"})
    assert dup.status_code == 422 and "already recorded" in dup.json()["error"]["message"]
    wrong_mask = await api.post("/api/v1/ip-addresses", json={"address": "2001:db8:4c56:10::9/128"})
    assert wrong_mask.status_code == 422 and "/64" in wrong_mask.json()["error"]["message"]


async def test_ip_assignment_lifecycle(
    api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any]
) -> None:
    nb = nb_admin
    eth0, eth1 = infra["ifaces"][0], infra["ifaces"][1]
    device = infra["device"]
    ok(
        await api.post("/api/v1/prefixes", json={"prefix": "198.51.100.128/25", "name": "lanvex-it servers"}),
        201,
    )

    r = await api.post("/api/v1/ip-addresses", json={"address": "198.51.100.255/25"})
    assert r.status_code == 422 and "broadcast" in r.json()["error"]["message"]
    r = await api.post("/api/v1/ip-addresses", json={"address": "198.51.100.130/24"})
    assert r.status_code == 422 and "/25" in r.json()["error"]["message"]

    ip = ok(
        await api.post(
            "/api/v1/ip-addresses",
            json={
                "address": "198.51.100.130/25",
                "interface_id": eth0["id"],
                "dns_name": "lanvex-it.example.net",
            },
        ),
        201,
    )["ip_address"]
    assert ip["assigned"]["interface_name"] == "eth0" and ip["assigned"]["device"]["id"] == device["id"]

    r = await api.post("/api/v1/ip-addresses", json={"address": "198.51.100.130/25"})
    assert r.status_code == 422 and "already recorded" in r.json()["error"]["message"]
    # Outside any leaf prefix (only the /24 container covers it), the same host with another mask is a duplicate.
    loose = ok(await api.post("/api/v1/ip-addresses", json={"address": "198.51.100.10/24"}), 201)[
        "ip_address"
    ]
    r = await api.post("/api/v1/ip-addresses", json={"address": "198.51.100.10/32"})
    assert r.status_code == 422 and "198.51.100.10/24" in r.json()["error"]["message"]
    ok(await api.delete(f"/api/v1/ip-addresses/{loose['id']}"))

    ids = {
        i["id"]
        for i in ok(await api.get("/api/v1/ip-addresses", params={"device_id": device["id"]}))["results"]
    }
    assert ip["id"] in ids
    assert ip["id"] in {
        i["id"]
        for i in ok(
            await api.get("/api/v1/ip-addresses", params={"assigned": True, "parent": "198.51.100.128/25"})
        )["results"]
    }

    detail = ok(await api.get(f"/api/v1/devices/{device['id']}"))
    assert [i["address"] for i in detail["ip_addresses"]] == ["198.51.100.130/25"]
    assert {i["name"] for i in detail["interfaces"]} == {"eth0", "eth1", "mgmt0"}

    upd = ok(
        await api.patch(
            f"/api/v1/ip-addresses/{ip['id']}", json={"status": "reserved", "description": "lanvex-it"}
        )
    )["ip_address"]
    assert upd["status"]["value"] == "reserved" and upd["description"] == "lanvex-it"

    # A device's primary IP cannot be released; Lanvex says so before NetBox would.
    await nb.patch(f"dcim/devices/{device['id']}/", {"primary_ip4": ip["id"]})
    r = await api.post(f"/api/v1/ip-addresses/{ip['id']}/release")
    assert r.status_code == 422 and "primary ip4" in r.json()["error"]["message"]
    await nb.patch(f"dcim/devices/{device['id']}/", {"primary_ip4": None})

    moved = ok(await api.post(f"/api/v1/ip-addresses/{ip['id']}/assign", json={"interface_id": eth1["id"]}))[
        "ip_address"
    ]
    assert moved["assigned"]["interface_name"] == "eth1"
    released = ok(await api.post(f"/api/v1/ip-addresses/{ip['id']}/release"))["ip_address"]
    assert released["assigned"] is None
    raw = await nb.get(f"ipam/ip-addresses/{ip['id']}/")
    assert raw["assigned_object_id"] is None and raw["dns_name"] == "lanvex-it.example.net"

    ok(await api.delete(f"/api/v1/ip-addresses/{ip['id']}"))
    with pytest.raises(NotFound):
        await nb.get(f"ipam/ip-addresses/{ip['id']}/")


async def test_vlans_and_interface_modes(
    api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any]
) -> None:
    group = infra["group"]["id"]
    a = ok(
        await api.post(
            "/api/v1/vlans",
            json={"vid": VIDS[1], "name": "lanvex-it voice", "group_id": group, "description": "x"},
        ),
        201,
    )["vlan"]
    b = ok(
        await api.post("/api/v1/vlans", json={"vid": VIDS[2], "name": "lanvex-it guests", "group_id": group}),
        201,
    )["vlan"]
    assert a["group"]["id"] == group

    r = await api.post("/api/v1/vlans", json={"vid": VIDS[1], "name": "lanvex-it clash", "group_id": group})
    assert r.status_code == 422 and r.json()["error"]["fields"]["vid"]

    renamed = ok(
        await api.patch(f"/api/v1/vlans/{a['id']}", json={"name": "lanvex-it voip", "status": "reserved"})
    )["vlan"]
    assert renamed["name"] == "lanvex-it voip" and renamed["status"]["value"] == "reserved"
    listed = ok(await api.get("/api/v1/vlans", params={"group_id": group}))
    assert {v["vid"] for v in listed["results"]} >= {VIDS[1], VIDS[2]}

    eth1 = infra["ifaces"][1]["id"]
    acc = ok(
        await api.put(
            f"/api/v1/interfaces/{eth1}/vlans", json={"mode": "access", "untagged_vlan_id": a["id"]}
        )
    )["interface"]
    assert acc["mode"]["value"] == "access" and acc["untagged_vlan"]["vid"] == VIDS[1]
    trunk = ok(
        await api.put(
            f"/api/v1/interfaces/{eth1}/vlans",
            json={"mode": "tagged", "untagged_vlan_id": a["id"], "tagged_vlan_ids": [b["id"]]},
        )
    )["interface"]
    assert [v["vid"] for v in trunk["tagged_vlans"]] == [VIDS[2]]
    detail = ok(await api.get(f"/api/v1/devices/{infra['device']['id']}"))
    assert [v["vid"] for v in detail["vlans"]] == [VIDS[1], VIDS[2]]
    all_ = ok(await api.put(f"/api/v1/interfaces/{eth1}/vlans", json={"mode": "tagged-all"}))["interface"]
    assert all_["mode"]["value"] == "tagged-all" and all_["tagged_vlans"] == []
    none = ok(await api.put(f"/api/v1/interfaces/{eth1}/vlans", json={"mode": None}))["interface"]
    assert none["mode"] is None and none["untagged_vlan"] is None

    ok(await api.delete(f"/api/v1/vlans/{b['id']}"))
    with pytest.raises(NotFound):
        await nb_admin.get(f"ipam/vlans/{b['id']}/")


async def test_inventory_lookups_and_dashboard(api: httpx.AsyncClient, infra: dict[str, Any]) -> None:
    devices = ok(await api.get("/api/v1/devices", params={"site_id": infra["site"]["id"]}))
    assert [d["name"] for d in devices["results"]] == [infra["device"]["name"]]
    d = devices["results"][0]
    assert d["device_type"]["manufacturer"]["name"].startswith("lanvex-it") and d["interface_count"] == 3

    ifaces = ok(await api.get("/api/v1/interfaces", params={"device_id": infra["device"]["id"]}))
    assert ifaces["count"] == 3

    for kind in (
        "vrfs",
        "prefix-roles",
        "tenants",
        "sites",
        "vlan-groups",
        "vlans",
        "device-roles",
        "manufacturers",
    ):
        ok(await api.get(f"/api/v1/lookups/{kind}"))
    vrfs = ok(await api.get("/api/v1/lookups/vrfs", params={"q": "lanvex-it"}))
    assert [v["id"] for v in vrfs] == [infra["vrf"]["id"]]

    o = ok(await api.get("/api/v1/dashboard/overview", params={"refresh": True}))
    assert o["counts"]["devices"] >= 1 and o["counts"]["sites"] >= 1 and o["counts"]["vrfs"] >= 1
    assert o["recent_changes_available"] is True, o["recent_changes_message"]
    assert o["recent_changes"] and o["recent_changes"][0]["source"] == "netbox"


async def test_utilized_ranges_counted_identically_in_list_and_detail(
    api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any]
) -> None:
    """The list batches IP-range lookups for a whole page; it must agree with the per-prefix path."""
    a = ok(
        await api.post("/api/v1/prefixes", json={"prefix": "198.51.100.0/27", "name": "lanvex-it pool a"}),
        201,
    )["prefix"]
    b = ok(
        await api.post(
            "/api/v1/prefixes",
            json={"prefix": "198.51.100.32/27", "name": "lanvex-it pool b", "vrf_id": infra["vrf"]["id"]},
        ),
        201,
    )["prefix"]
    await nb_admin.post(
        "ipam/ip-ranges/",
        {
            "start_address": "198.51.100.5/27",
            "end_address": "198.51.100.14/27",
            "status": "active",
            "mark_utilized": True,
        },
    )
    await nb_admin.post(
        "ipam/ip-ranges/",
        {
            "start_address": "198.51.100.40/27",
            "end_address": "198.51.100.41/27",
            "status": "active",
            "mark_utilized": True,
            "vrf": infra["vrf"]["id"],
        },
    )
    await nb_admin.post(
        "ipam/ip-ranges/",
        {
            "start_address": "198.51.100.20/27",
            "end_address": "198.51.100.29/27",
            "status": "active",
            "mark_utilized": False,
        },
    )
    listed = {
        p["id"]: p["utilization"]
        for p in ok(await api.get("/api/v1/prefixes", params={"within": "198.51.100.0/26"}))["results"]
    }
    for pid, used in ((a["id"], 10), (b["id"], 2)):
        detail = ok(await api.get(f"/api/v1/prefixes/{pid}"))["utilization"]
        assert listed[pid] == detail and detail["used"] == used, (pid, listed[pid], detail)


@pytest.mark.skipif(not READONLY_TOKEN, reason="LANVEX_TEST_NETBOX_READONLY_TOKEN not set")
async def test_write_disabled_token_is_reported_and_audited(
    database: Database, users: dict[str, str]
) -> None:
    application = _app(database, READONLY_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://lanvex.test"
    ) as c:
        await login(c, "admin", users["admin"])
        assert ok(await c.get("/api/v1/prefixes", params={"limit": 1}))  # reads work
        r = await c.post("/api/v1/prefixes", json={"prefix": "198.51.100.0/28"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "netbox_permission_denied"
    await application.state.netbox.aclose()
    async with database.sessionmaker() as s:
        ev = (await s.scalars(select(AuditEvent).where(AuditEvent.action == "prefix.create"))).one()
    assert ev.outcome == "failure" and "NetBox denied" in ev.detail["error"]


async def test_gateway_dns_and_routes_compared_and_recorded(
    api: httpx.AsyncClient, nb_admin: NetBoxClient, infra: dict[str, Any], fake: FakeServer
) -> None:
    """The server (a stand-in with real lab facts) uses gateway .254, DNS 127.0.0.11, search domain
    lab.example.net and a static route to 203.0.113.0/24; NetBox documents gateway .193 for the prefix,
    and a site-wide config context documents other DNS servers and another route."""
    nb, device = nb_admin, infra["device"]
    prefix = await nb.post("ipam/prefixes/", {"prefix": "198.51.100.192/26", "status": "active"})
    await PrefixService(nb).set_gateway(to_prefix(prefix), "198.51.100.193/26")
    await nb.post(
        "extras/config-contexts/",
        {
            "name": f"lanvex-it site {infra['tag']}",
            "sites": [infra["site"]["id"]],
            "data": {
                "lanvex": {
                    "dns_servers": ["198.51.100.53"],
                    "routes": [{"to": "192.0.2.0/24", "via": "198.51.100.200"}],
                }
            },
        },
    )
    await nb.patch(f"dcim/devices/{device['id']}/", {"local_context_data": {"ntp": ["pool.ntp.org"]}})
    for r in fake.facts.routes:
        if r.is_default and r.family == 4:
            r.gateway = "198.51.100.254"
    fake.facts.routes.append(
        Route(
            destination="203.0.113.0/24",
            family=4,
            gateway="198.51.100.200",
            interface="eth0",
            protocol="static",
        )
    )
    link = {"object_type": "dcim.device", "object_id": device["id"]}
    s = ok(await api.post("/api/v1/servers", json=body(address="192.0.2.10", netbox=link)), 201)["server"]
    assert ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))["status"] == "ok"

    c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
    rows = {r["id"]: r for r in c["settings"]}
    assert {k: (r["status"], r["server"], r["documented"]) for k, r in rows.items()} == {
        "gateway:198.51.100.254": ("differs", "198.51.100.254", "198.51.100.193"),
        "dns_servers": ("differs", "127.0.0.11", "198.51.100.53"),
        "dns_search": ("only_on_server", "lab.example.net", None),
        "route:192.0.2.0/24": ("only_documented", None, "198.51.100.200"),
        "route:203.0.113.0/24": ("only_on_server", "198.51.100.200", None),
    }

    url = f"/api/v1/servers/{s['id']}/record"
    plan = ok(await api.post(url, json={"settings": list(rows)}))
    done = ok(await api.post(url, json={"settings": list(rows), "token": plan["token"]}))
    assert done["failed"] is None and len(done["done"]) == 5, done

    after = await nb.get(f"dcim/devices/{device['id']}/")
    assert after["local_context_data"] == {
        "ntp": ["pool.ntp.org"],
        "lanvex": {
            "dns_servers": ["127.0.0.11"],
            "dns_search": ["lab.example.net"],
            "routes": [{"to": "203.0.113.0/24", "via": "198.51.100.200"}],
        },
    }
    assert after["config_context"]["lanvex"]["routes"] == [{"to": "203.0.113.0/24", "via": "198.51.100.200"}]
    gateway = await PrefixService(nb).find_gateway(to_prefix(prefix))
    assert gateway is not None and gateway.address == "198.51.100.254/26"
    c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
    assert {r["status"] for r in c["settings"]} == {"match"}
    ok(await api.delete(f"/api/v1/servers/{s['id']}"), 204)
