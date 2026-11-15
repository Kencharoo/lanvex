"""Recording what a server has in NetBox: preview, the exact-plan check, what is refused and why.

The server's facts are real lab output (tests/fixtures/linux/netplan: eth0 172.30.0.11/24, br0
10.11.0.1/24) plus addresses the tests add. NetBox is a small in-memory stand-in with the response
shapes of NetBox 4.7; the same flow runs against a real Lanvex Core in test_servers_lab.py.
"""

import json
from itertools import count
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from lanvex.audit.models import AuditEvent
from lanvex.core.db import Database
from lanvex.integrations.linux.facts import Address

from . import test_server_history
from .conftest import login
from .test_server_history import FakeServer
from .test_servers_api import body

DEVICE, OTHER = 7, 8
fake = test_server_history.fake  # the stand-in server, as a fixture here too


class NetBox:
    """Devices 7 (web-01, the server) and 8 (db-01), interfaces, IP addresses."""

    def __init__(self) -> None:
        self.ids = count(500)
        self.devices = {
            DEVICE: {"id": DEVICE, "name": "web-01", "primary_ip4": None},
            OTHER: {"id": OTHER, "name": "db-01", "primary_ip4": None},
        }
        self.interfaces: dict[int, dict[str, Any]] = {
            11: {"id": 11, "name": "eth0", "device": DEVICE},
            21: {"id": 21, "name": "eth0", "device": OTHER},
        }
        self.ips: dict[int, dict[str, Any]] = {}
        self.fail_on: str | None = None  # an address whose creation NetBox refuses

    def ip(self, address: str, interface: int | None, **extra: Any) -> int:
        i = next(self.ids)
        self.ips[i] = {"id": i, "address": address, "assigned_object_id": interface, **extra}
        return i

    def ser_ip(self, o: dict[str, Any]) -> dict[str, Any]:
        iface = self.interfaces.get(o.get("assigned_object_id") or 0)
        assigned = None
        if iface:
            dev = self.devices[iface["device"]]
            assigned = {
                "id": iface["id"],
                "name": iface["name"],
                "device": {"id": dev["id"], "name": dev["name"]},
            }
        return {
            **o,
            "assigned_object_type": "dcim.interface" if iface else None,
            "assigned_object": assigned,
            "status": {"value": o.get("status", "active")},
        }

    def ser_device(self, d: dict[str, Any]) -> dict[str, Any]:
        p = d["primary_ip4"]
        return {
            **d,
            "display": d["name"],
            "display_url": f"https://netbox.test/dcim/devices/{d['id']}/",
            "primary_ip4": {"id": p, "address": self.ips[p]["address"]} if p else None,
            "primary_ip6": None,
            "oob_ip": None,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        qp = request.url.params
        if request.method == "GET" and path.startswith("dcim/devices/"):
            oid = int(path.split("/")[2])
            return httpx.Response(200, json=self.ser_device(self.devices[oid]))
        if path == "dcim/interfaces/":
            if request.method == "POST":
                b = json.loads(request.content)
                assert b["device"] in self.devices and b["type"]
                i = next(self.ids)
                self.interfaces[i] = {"id": i, "name": b["name"], "device": b["device"], "type": b["type"]}
                return httpx.Response(201, json=self.interfaces[i])
            dev = int(qp["device_id"])
            rows = [i for i in self.interfaces.values() if i["device"] == dev]
            return httpx.Response(200, json={"count": len(rows), "results": rows})
        if path == "ipam/ip-addresses/":
            if request.method == "POST":
                b = json.loads(request.content)
                if b["address"] == self.fail_on:
                    return httpx.Response(
                        400, json={"address": ["Duplicate IP address found in global table"]}
                    )
                i = self.ip(b["address"], b.get("assigned_object_id"), status=b.get("status", "active"))
                return httpx.Response(201, json=self.ser_ip(self.ips[i]))
            rows = list(self.ips.values())
            if dev := qp.get("device_id"):
                rows = [
                    o
                    for o in rows
                    if (i := self.interfaces.get(o.get("assigned_object_id") or 0))
                    and i["device"] == int(dev)
                ]
            if wanted := qp.get_list("address"):
                rows = [o for o in rows if any(o["address"].split("/")[0] == w.split("/")[0] for w in wanted)]
            return httpx.Response(200, json={"count": len(rows), "results": [self.ser_ip(o) for o in rows]})
        if request.method == "PATCH" and path.startswith("ipam/ip-addresses/"):
            oid = int(path.split("/")[2])
            self.ips[oid].update(json.loads(request.content))
            return httpx.Response(200, json=self.ser_ip(self.ips[oid]))
        return httpx.Response(404, json={"detail": "Not found."})


@pytest.fixture
def nb(netbox_mock: respx.MockRouter) -> NetBox:
    state = NetBox()
    netbox_mock.route().mock(side_effect=state.handle)
    return state


async def linked_server(admin: httpx.AsyncClient, fake: FakeServer) -> str:
    r = await admin.post(
        "/api/v1/servers",
        json=body(address="192.0.2.10", netbox={"object_type": "dcim.device", "object_id": DEVICE}),
    )
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).json()["status"] == "ok"
    return sid


def scenario(nb: NetBox, fake: FakeServer) -> dict[str, int]:
    """eth0: 172.30.0.11 documented; .50 only on the server; .77 on the server but recorded on db-01;
    .98 (the primary IP) and .99 only in NetBox. br0: recorded unassigned as 10.11.0.1/16, and NetBox
    has no br0 on web-01."""
    eth = next(i for i in fake.facts.interfaces if i.name == "eth0")
    eth.addresses += [
        Address(address="172.30.0.50/24", family=4),
        Address(address="172.30.0.77/24", family=4),
    ]
    ids = {
        "11": nb.ip("172.30.0.11/24", 11),
        "77": nb.ip("172.30.0.77/24", 21),
        "98": nb.ip("172.30.0.98/24", 11),
        "99": nb.ip("172.30.0.99/24", 11),
        "br0": nb.ip("10.11.0.1/16", None),
    }
    nb.devices[DEVICE]["primary_ip4"] = ids["98"]
    return ids


PICK = ["172.30.0.50", "172.30.0.77", "172.30.0.98", "172.30.0.99", "10.11.0.1", "172.30.0.11"]


async def test_preview_then_apply_exactly_that_plan(
    admin: httpx.AsyncClient,
    nb: NetBox,
    fake: FakeServer,
    database: Database,
) -> None:
    ids = scenario(nb, fake)
    sid = await linked_server(admin, fake)
    plan = (await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": PICK})).json()
    assert [(a["kind"], a["text"], a["problem"]) for a in plan["actions"]] == [
        ("create_interface", "Add interface br0 to web-01 (type Bridge)", None),
        ("create", "Record 172.30.0.50/24 on web-01 / eth0", None),
        (
            "assign",
            "Assign 172.30.0.77/24 to web-01 / eth0",
            "It is recorded on db-01 / eth0. If it belongs to this server, move it in NetBox.",
        ),
        (
            "unassign",
            "Unassign 172.30.0.98/24 from web-01 / eth0 (the record stays in NetBox)",
            "It is the primary IPv4 of web-01. Choose another one in NetBox first.",
        ),
        ("unassign", "Unassign 172.30.0.99/24 from web-01 / eth0 (the record stays in NetBox)", None),
        ("assign", "Assign the existing record 10.11.0.1/16 to web-01 / br0, as 10.11.0.1/24", None),
    ]
    assert not plan["applied"] and len(nb.ips) == 5 and len(nb.interfaces) == 2  # a preview writes nothing

    stale = await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": PICK, "token": "f" * 32})
    assert stale.status_code == 409 and "changed since the preview" in stale.json()["error"]["message"]

    done = (
        await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": PICK, "token": plan["token"]})
    ).json()
    assert done["applied"] and done["failed"] is None
    assert [a["kind"] for a in done["done"]] == ["create_interface", "create", "unassign", "assign"]
    br0 = next(i for i in nb.interfaces.values() if i["name"] == "br0")
    assert br0 == {"id": br0["id"], "name": "br0", "device": DEVICE, "type": "bridge"}
    assert (
        nb.ips[ids["br0"]]["address"] == "10.11.0.1/24"
        and nb.ips[ids["br0"]]["assigned_object_id"] == br0["id"]
    )
    assert nb.ips[ids["99"]]["assigned_object_id"] is None
    assert nb.ips[ids["98"]]["assigned_object_id"] == 11 and nb.ips[ids["77"]]["assigned_object_id"] == 21
    new = next(o for o in nb.ips.values() if o["address"] == "172.30.0.50/24")
    assert new["assigned_object_id"] == 11 and new["status"] == "active"

    rows = {
        r["address"]: r["status"]
        for r in (await admin.get(f"/api/v1/servers/{sid}/comparison")).json()["rows"]
    }
    assert rows == {
        "172.30.0.11": "match",
        "172.30.0.50": "match",
        "10.11.0.1": "match",
        "172.30.0.77": "only_on_server",
        "172.30.0.98": "only_in_netbox",
    }
    async with database.sessionmaker() as s:
        event = await s.scalar(select(AuditEvent).where(AuditEvent.action == "server.record"))
        assert event is not None and event.outcome == "success" and len(event.detail["done"]) == 4
        assert len(event.detail["skipped"]) == 2


async def test_dhcp_addresses_are_recorded_as_dhcp(
    admin: httpx.AsyncClient, nb: NetBox, fake: FakeServer
) -> None:
    eth = next(i for i in fake.facts.interfaces if i.name == "eth0")
    eth.addresses.append(Address(address="172.30.0.120/24", family=4, dynamic=True))
    sid = await linked_server(admin, fake)
    plan = (await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": ["172.30.0.120"]})).json()
    assert [a["text"] for a in plan["actions"]] == [
        "Record 172.30.0.120/24 on web-01 / eth0 with status DHCP"
    ]
    await admin.post(
        f"/api/v1/servers/{sid}/record", json={"addresses": ["172.30.0.120"], "token": plan["token"]}
    )
    assert next(o for o in nb.ips.values() if o["address"] == "172.30.0.120/24")["status"] == "dhcp"


async def test_a_refused_write_stops_and_says_what_was_done(
    admin: httpx.AsyncClient,
    nb: NetBox,
    fake: FakeServer,
    database: Database,
) -> None:
    scenario(nb, fake)
    nb.fail_on = "172.30.0.50/24"
    sid = await linked_server(admin, fake)
    pick = ["10.11.0.1", "172.30.0.50", "172.30.0.99"]
    plan = (await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": pick})).json()
    out = (
        await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": pick, "token": plan["token"]})
    ).json()
    assert [a["kind"] for a in out["done"]] == ["create_interface", "assign"]
    assert out["failed"]["text"] == "Record 172.30.0.50/24 on web-01 / eth0"
    assert "Duplicate IP address" in out["error"]
    async with database.sessionmaker() as s:
        event = await s.scalar(select(AuditEvent).where(AuditEvent.action == "server.record"))
        assert event is not None and event.outcome == "failure"


async def test_needs_a_link_an_admin_and_addresses(
    admin: httpx.AsyncClient,
    users: dict[str, str],
    nb: NetBox,
    fake: FakeServer,
) -> None:
    r = await admin.post("/api/v1/servers", json=body(address="192.0.2.10"))
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    await admin.post(f"/api/v1/servers/{sid}/collect")
    r = await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": ["172.30.0.11"]})
    assert r.status_code == 422 and "Link this server" in r.json()["error"]["message"]
    r = await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": ["not-an-ip"]})
    assert r.status_code == 422
    await login(admin, "reader", users["reader"])
    assert (
        await admin.post(f"/api/v1/servers/{sid}/record", json={"addresses": ["172.30.0.11"]})
    ).status_code == 403
