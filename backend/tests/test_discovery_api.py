"""Discovery: hosts from servers' neighbour tables, LLDP links compared with documented cables, and
recording either in NetBox through a preview.

Server facts are real lab output (tests/fixtures/linux/netplan) with neighbours and LLDP entries set
per test; parsing those from real lldpd and systemd-networkd output is tested in test_linux_parsing.py,
and collecting them from real hosts in test_servers_lab.py. NetBox is an in-memory stand-in with the
response shapes of NetBox 4.7 (interfaces carry `cable` and `connected_endpoints`).
"""

import json
from typing import Any

import httpx
import pytest
import respx

from lanvex.integrations.linux.facts import LldpNeighbor, Neighbor

from . import test_server_history
from .conftest import login
from .test_server_history import FakeServer
from .test_servers_api import body

fake = test_server_history.fake  # the stand-in server, as a fixture here too


def lldp(iface: str, system: str, port: str) -> LldpNeighbor:
    return LldpNeighbor(
        interface=iface, source="lldpd", chassis_id=f"id-{system}", system_name=system, port_id=port
    )


def neigh(address: str, iface: str = "eth0", router: bool = False) -> Neighbor:
    return Neighbor(
        address=address,
        family=6 if ":" in address else 4,
        interface=iface,
        mac="02:00:00:00:00:01",
        state="REACHABLE",
        router=router,
    )


class NetBox:
    def __init__(self) -> None:
        self.devices = {7: "web-01", 20: "sw-01", 21: "sw-02"}
        ep = lambda dev, name: {"device": {"id": dev, "name": self.devices[dev]}, "name": name}  # noqa: E731
        self.interfaces: list[dict[str, Any]] = [
            {
                "id": 11,
                "device": 7,
                "name": "eth0",
                "cable": {"id": 1},
                "connected_endpoints": [ep(20, "Gi1/0/1")],
            },
            {"id": 12, "device": 7, "name": "eth1", "cable": None},
            {"id": 13, "device": 7, "name": "eth2", "cable": None},
            {"id": 14, "device": 7, "name": "eth3", "cable": None},
            {"id": 15, "device": 7, "name": "eth4", "cable": None},
            {
                "id": 16,
                "device": 7,
                "name": "eth5",
                "cable": {"id": 2},
                "connected_endpoints": [ep(20, "Gi1/0/2")],
            },
            {"id": 17, "device": 7, "name": "eth6", "cable": None},
            {
                "id": 18,
                "device": 7,
                "name": "eth7",
                "cable": None,
                "type": {"value": "virtual", "label": "Virtual"},
            },
            {"id": 201, "device": 20, "name": "Gi1/0/1", "cable": {"id": 1}},
            {"id": 202, "device": 20, "name": "Gi1/0/2", "cable": {"id": 2}},
            {
                "id": 211,
                "device": 21,
                "name": "Gi1/0/7",
                "label": "",
                "cable": None,
                "type": {"value": "1000base-t", "label": "1000BASE-T (1GE)"},
            },
            {
                "id": 213,
                "device": 21,
                "name": "Po1",
                "cable": None,
                "type": {"value": "lag", "label": "Link Aggregation Group (LAG)"},
            },
            {"id": 212, "device": 21, "name": "Gi1/0/8", "cable": {"id": 3}},
        ]
        self.ips = [{"id": 900, "address": "172.30.0.1/24", "assigned_object": None, "display_url": None}]
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        qp = request.url.params
        page = lambda rows: httpx.Response(200, json={"count": len(rows), "results": rows})  # noqa: E731
        if request.method == "POST":
            self.posted.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"id": 999, **json.loads(request.content)})
        if path.startswith("dcim/devices/") and path.count("/") == 3:
            oid = int(path.split("/")[2])
            return httpx.Response(
                200, json={"id": oid, "name": self.devices[oid], "display": self.devices[oid]}
            )
        if path == "dcim/devices/":
            names = set(qp.get_list("name"))
            return page([{"id": i, "name": n} for i, n in self.devices.items() if n in names])
        if path == "dcim/interfaces/":
            return page(
                [
                    {**i, "device": {"id": i["device"]}}
                    for i in self.interfaces
                    if i["device"] == int(qp["device_id"])
                ]
            )
        if path == "ipam/ip-addresses/":
            wanted = set(qp.get_list("address"))
            return page([o for o in self.ips if o["address"].split("/")[0] in wanted])
        return httpx.Response(404, json={"detail": "Not found."})


@pytest.fixture
def nb(netbox_mock: respx.MockRouter) -> NetBox:
    state = NetBox()
    netbox_mock.route().mock(side_effect=state.handle)
    return state


async def add_server(admin: httpx.AsyncClient, name: str, address: str, link: bool) -> str:
    extra = {"netbox": {"object_type": "dcim.device", "object_id": 7}} if link else {}
    r = await admin.post("/api/v1/servers", json=body(name=name, address=address, **extra))
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).json()["status"] == "ok"
    return sid


async def scenario(admin: httpx.AsyncClient, fake: FakeServer) -> tuple[str, str]:
    """web-01 (linked to NetBox device 7) and web-02 (not linked), each read once."""
    fake.facts.neighbors = [neigh("172.30.0.1", router=True), neigh("172.30.0.12"), neigh("fe80::1")]
    fake.facts.lldp = [
        lldp("eth0", "sw-01.example.net", "Gi1/0/1"),  # documented, through its short name
        lldp("eth1", "sw-02", "Gi1/0/7"),  # recordable
        lldp("eth2", "sw-02", "Gi1/0/8"),  # that port already has a cable
        lldp("eth3", "sw-09", "Gi1/0/1"),  # no such device
        lldp("eth4", "sw-01", "Gi1/0/5"),
        lldp("eth4", "sw-02", "Gi1/0/6"),  # two neighbours on one port
        lldp("eth5", "sw-02", "Gi1/0/5"),  # NetBox says sw-01 / Gi1/0/2
        lldp("br0", "sw-02", "Gi1/0/9"),  # NetBox lacks br0 on web-01
        lldp("eth6", "sw-02", "Po1"),  # NetBox cannot cable a LAG
        lldp("eth7", "sw-02", "Gi1/0/7"),  # web-01's eth7 is virtual in NetBox
    ]
    a = await add_server(admin, "web-01", "192.0.2.10", link=True)
    fake.facts.neighbors = [neigh("192.0.2.10"), neigh("172.30.0.12")]
    fake.facts.lldp = [lldp("eth0", "sw-01", "Gi1/0/3")]
    b = await add_server(admin, "web-02", "192.0.2.11", link=False)
    return a, b


async def test_hosts_seen_are_compared_with_netbox(
    admin: httpx.AsyncClient, nb: NetBox, fake: FakeServer
) -> None:
    await scenario(admin, fake)
    d = (await admin.get("/api/v1/discovery/hosts")).json()
    assert d["netbox_checked"] and d["total"] == 3 and d["undocumented"] == 2  # fe80::1 is not listed
    hosts = {h["address"]: h for h in d["hosts"]}
    assert hosts["172.30.0.1"]["netbox"]["address"] == "172.30.0.1/24" and hosts["172.30.0.1"]["router"]
    assert {s["server_name"] for s in hosts["172.30.0.12"]["seen_by"]} == {"web-01", "web-02"}
    assert hosts["172.30.0.12"]["record_as"] == "172.30.0.12/24"  # the network web-01's eth0 is on
    assert hosts["192.0.2.10"]["server"] == "web-01" and hosts["192.0.2.10"]["record_as"] == "192.0.2.10/32"
    only = (await admin.get("/api/v1/discovery/hosts", params={"undocumented": "true"})).json()
    assert sorted(h["address"] for h in only["hosts"]) == ["172.30.0.12", "192.0.2.10"]


async def test_recording_hosts(admin: httpx.AsyncClient, nb: NetBox, fake: FakeServer) -> None:
    await scenario(admin, fake)
    items = ["172.30.0.12", "192.0.2.10", "172.30.0.1"]
    plan = (await admin.post("/api/v1/discovery/hosts/record", json={"items": items})).json()
    assert [(a["text"].split(" (")[0], a["problem"]) for a in plan["actions"]] == [
        ("Record 172.30.0.12/24", None),
        (
            "Record 192.0.2.10",
            "It is web-01's own address: record it from that server's page, where it is assigned to the right interface.",
        ),
        ("Record 172.30.0.1", "Already recorded in NetBox as 172.30.0.1/24."),
    ]
    assert nb.posted == []
    r = await admin.post("/api/v1/discovery/hosts/record", json={"items": items, "token": "0" * 32})
    assert r.status_code == 409
    done = (
        await admin.post("/api/v1/discovery/hosts/record", json={"items": items, "token": plan["token"]})
    ).json()
    assert len(done["done"]) == 1 and done["failed"] is None
    [(path, sent)] = nb.posted
    assert path == "ipam/ip-addresses/" and sent["address"] == "172.30.0.12/24" and sent["status"] == "active"
    assert sent["description"].startswith("Seen by ") and "assigned_object_id" not in sent


async def test_lldp_links_against_documented_cables(
    admin: httpx.AsyncClient, nb: NetBox, fake: FakeServer
) -> None:
    await scenario(admin, fake)
    d = (await admin.get("/api/v1/discovery/links")).json()
    got = {(x["server_name"], x["interface"], x["neighbor"]["port_id"]): x for x in d["links"]}
    status = {k: (v["status"], v["problem"]) for k, v in got.items()}
    assert status[("web-01", "eth0", "Gi1/0/1")] == ("documented", None)
    assert status[("web-01", "eth1", "Gi1/0/7")] == ("undocumented", None)
    assert got[("web-01", "eth1", "Gi1/0/7")]["peer"] == "sw-02 / Gi1/0/7"
    assert status[("web-01", "eth2", "Gi1/0/8")] == (
        "undocumented",
        "sw-02 / Gi1/0/8 already has a cable in NetBox.",
    )
    assert status[("web-01", "eth3", "Gi1/0/1")] == ("undocumented", "No device named sw-09 in NetBox.")
    assert status[("web-01", "eth4", "Gi1/0/5")][0] == "several"
    assert status[("web-01", "eth5", "Gi1/0/5")] == ("different", None)
    assert got[("web-01", "eth5", "Gi1/0/5")]["documented"] == "sw-01 / Gi1/0/2"
    assert "has no interface br0" in status[("web-01", "br0", "Gi1/0/9")][1]
    assert status[("web-01", "eth6", "Po1")][1] == (
        "sw-02 / Po1 has the type Link Aggregation Group (LAG) in NetBox, which cannot have a cable. "
        "If it is a physical port, change its type there."
    )
    assert status[("web-01", "eth7", "Gi1/0/7")][1].startswith("web-01 / eth7 has the type Virtual in NetBox")
    assert status[("web-02", "eth0", "Gi1/0/3")][0] == "not_linked"


async def test_recording_a_cable(
    admin: httpx.AsyncClient, users: dict[str, str], nb: NetBox, fake: FakeServer
) -> None:
    await scenario(admin, fake)
    links = (await admin.get("/api/v1/discovery/links")).json()["links"]
    key = {
        (x["interface"], x["neighbor"]["port_id"]): x["key"] for x in links if x["server_name"] == "web-01"
    }
    items = [key[("eth1", "Gi1/0/7")], key[("eth0", "Gi1/0/1")], key[("eth5", "Gi1/0/5")]]
    plan = (await admin.post("/api/v1/discovery/links/record", json={"items": items})).json()
    assert [(a["text"], a["problem"]) for a in plan["actions"]] == [
        ("Record a cable web-01 / eth1 ↔ sw-02 / Gi1/0/7", None),
        ("Record a cable web-01 / eth0 ↔ sw-01.example.net", "Already documented in NetBox."),
        (
            "Record a cable web-01 / eth5 ↔ sw-02",
            "NetBox documents a cable to sw-01 / Gi1/0/2. Change it there if that is wrong.",
        ),
    ]
    await login(admin, "reader", users["reader"])
    assert (await admin.get("/api/v1/discovery/links")).status_code == 200
    r = await admin.post("/api/v1/discovery/links/record", json={"items": items, "token": plan["token"]})
    assert r.status_code == 403 and nb.posted == []
    await login(admin, "admin", users["admin"])
    done = (
        await admin.post("/api/v1/discovery/links/record", json={"items": items, "token": plan["token"]})
    ).json()
    assert len(done["done"]) == 1
    [(path, sent)] = nb.posted
    assert path == "dcim/cables/" and sent["status"] == "connected"
    assert sent["a_terminations"] == [{"object_type": "dcim.interface", "object_id": 12}]
    assert sent["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 211}]
