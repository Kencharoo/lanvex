"""Gateways, DNS and static routes: compared with the documentation, and recorded in NetBox.

The server's facts are real lab output (tests/fixtures/linux/netplan: default route via 172.30.0.1 on
eth0, DNS 127.0.0.11, search domain lab.example.net) plus a static route the tests add. NetBox is the
stand-in from test_server_record.py, with a prefix, the gateway tag and config contexts added.
"""

import copy
import json
from typing import Any

import httpx
import pytest
import respx

from lanvex.integrations.linux.facts import Route

from .test_server_history import FakeServer
from .test_server_record import DEVICE, NetBox, fake, linked_server

__all__ = ["fake"]

PREFIX, TAG = 70, 900
SITE_CONTEXT = {  # what a site-wide config context documents for every server there
    "lanvex": {
        "dns_servers": ["10.0.0.53"],
        "routes": [{"to": "192.0.2.0/24", "via": "172.30.0.254"}],
    }
}


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """NetBox's deep merge of config contexts: dictionaries merge, everything else is replaced."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


class SettingsNetBox(NetBox):
    def __init__(self) -> None:
        super().__init__()
        self.devices[DEVICE]["local_context_data"] = {"ntp": ["pool.ntp.org"]}
        self.prefix = {
            "id": PREFIX,
            "prefix": "172.30.0.0/24",
            "status": {"value": "active"},
            "vrf": None,
            "is_pool": False,
        }
        self.gateway = self.ip("172.30.0.2/24", None, tags=[TAG])  # documented: .2; the server uses .1

    def ser_ip(self, o: dict[str, Any]) -> dict[str, Any]:
        out = super().ser_ip(o)
        out["tags"] = [{"id": t, "slug": "lanvex-gateway"} for t in o.get("tags", [])]
        return out

    def ser_device(self, d: dict[str, Any]) -> dict[str, Any]:
        out = super().ser_device(d)
        out["config_context"] = merge(SITE_CONTEXT, d.get("local_context_data") or {})
        return out

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        qp = request.url.params
        if request.method == "PATCH" and path == f"dcim/devices/{DEVICE}/":
            self.devices[DEVICE].update(json.loads(request.content))
            return httpx.Response(200, json=self.ser_device(self.devices[DEVICE]))
        if path == "ipam/prefixes/":
            hit = qp.get("contains", "").startswith("172.30.0.")
            return httpx.Response(200, json={"count": int(hit), "results": [self.prefix] if hit else []})
        if path == f"ipam/prefixes/{PREFIX}/":
            return httpx.Response(200, json=self.prefix)
        if path == "extras/tags/":
            return httpx.Response(200, json={"count": 1, "results": [{"id": TAG, "slug": "lanvex-gateway"}]})
        if path == "ipam/ip-addresses/" and request.method == "GET" and qp.get("tag"):
            rows = [o for o in self.ips.values() if TAG in o.get("tags", [])]
            return httpx.Response(200, json={"count": len(rows), "results": [self.ser_ip(o) for o in rows]})
        if path == "ipam/ip-addresses/" and request.method == "POST":
            b = json.loads(request.content)
            i = self.ip(b["address"], None, status=b.get("status", "active"), tags=b.get("tags", []))
            return httpx.Response(201, json=self.ser_ip(self.ips[i]))
        if request.method == "GET" and path.startswith("ipam/ip-addresses/") and path.count("/") == 3:
            return httpx.Response(200, json=self.ser_ip(self.ips[int(path.split("/")[2])]))
        return super().handle(request)


@pytest.fixture
def nb(netbox_mock: respx.MockRouter) -> SettingsNetBox:
    state = SettingsNetBox()
    netbox_mock.route().mock(side_effect=state.handle)
    return state


def add_static_route(fake: FakeServer) -> None:
    fake.facts.routes.append(
        Route(
            destination="10.50.0.0/16", family=4, gateway="172.30.0.254", interface="eth0", protocol="static"
        )
    )


async def settings(admin: httpx.AsyncClient, sid: str) -> dict[str, dict[str, Any]]:
    r = await admin.get(f"/api/v1/servers/{sid}/comparison")
    assert r.status_code == 200, r.text
    return {s["id"]: s for s in r.json()["settings"]}


async def test_compared_with_the_prefix_gateway_and_the_config_context(
    admin: httpx.AsyncClient, nb: SettingsNetBox, fake: FakeServer
) -> None:
    add_static_route(fake)
    sid = await linked_server(admin, fake)
    rows = await settings(admin, sid)
    assert {k: v["status"] for k, v in rows.items()} == {
        "gateway:172.30.0.1": "differs",
        "dns_servers": "differs",
        "dns_search": "only_on_server",
        "route:10.50.0.0/16": "only_on_server",
        "route:192.0.2.0/24": "only_documented",
    }
    gw = rows["gateway:172.30.0.1"]
    assert (gw["server"], gw["documented"], gw["prefix"], gw["interface"]) == (
        "172.30.0.1",
        "172.30.0.2",
        "172.30.0.0/24",
        "eth0",
    )
    assert (rows["dns_servers"]["server"], rows["dns_servers"]["documented"]) == ("127.0.0.11", "10.0.0.53")


async def test_record_previews_then_writes_the_documentation(
    admin: httpx.AsyncClient, nb: SettingsNetBox, fake: FakeServer
) -> None:
    add_static_route(fake)
    sid = await linked_server(admin, fake)
    pick = list(await settings(admin, sid))
    url = f"/api/v1/servers/{sid}/record"
    plan = (await admin.post(url, json={"settings": pick})).json()
    assert [a["text"] for a in plan["actions"]] == [
        "Make 172.30.0.1 the gateway of 172.30.0.0/24 (instead of 172.30.0.2)",
        "Record DNS servers 127.0.0.11 for web-01 (instead of 10.0.0.53)",
        "Record search domains lab.example.net for web-01",
        "Record the route to 10.50.0.0/16 via 172.30.0.254 for web-01",
        "Remove the route to 192.0.2.0/24 via 172.30.0.254 from web-01's documentation",
    ]
    assert nb.devices[DEVICE]["local_context_data"] == {"ntp": ["pool.ntp.org"]}  # a preview writes nothing

    out = (await admin.post(url, json={"settings": pick, "token": plan["token"]})).json()
    assert out["failed"] is None and len(out["done"]) == 5
    assert nb.devices[DEVICE]["local_context_data"] == {
        "ntp": ["pool.ntp.org"],  # kept
        "lanvex": {
            "dns_servers": ["127.0.0.11"],
            "dns_search": ["lab.example.net"],
            "routes": [{"to": "10.50.0.0/16", "via": "172.30.0.254"}],
        },
    }
    tagged = sorted(o["address"] for o in nb.ips.values() if TAG in o.get("tags", []))
    assert tagged == ["172.30.0.1/24"]  # the old gateway's record stays, without the tag
    assert nb.ips[nb.gateway]["address"] == "172.30.0.2/24"
    assert {v["status"] for v in (await settings(admin, sid)).values()} == {"match"}


async def test_a_changed_documentation_needs_a_new_preview(
    admin: httpx.AsyncClient, nb: SettingsNetBox, fake: FakeServer
) -> None:
    sid = await linked_server(admin, fake)
    url = f"/api/v1/servers/{sid}/record"
    plan = (await admin.post(url, json={"settings": ["dns_servers"]})).json()
    nb.devices[DEVICE]["local_context_data"] = {"ntp": ["time.example.net"]}  # edited in NetBox meanwhile
    r = await admin.post(url, json={"settings": ["dns_servers"], "token": plan["token"]})
    assert r.status_code == 409
    assert nb.devices[DEVICE]["local_context_data"] == {"ntp": ["time.example.net"]}


async def test_what_no_longer_differs_is_not_recorded(
    admin: httpx.AsyncClient, nb: SettingsNetBox, fake: FakeServer
) -> None:
    sid = await linked_server(admin, fake)
    url = f"/api/v1/servers/{sid}/record"
    plan = (await admin.post(url, json={"settings": ["route:10.99.0.0/16"]})).json()
    assert plan["actions"][0]["problem"].startswith("It no longer differs")
    assert (await admin.post(url, json={})).status_code == 422  # nothing picked
