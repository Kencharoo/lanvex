"""Switches through the API: adding with SNMP credentials that are never shown again, reading, scheduled
reads, monitoring, and what is connected to each port compared with the documented cables.

The switches are SNMP agents in this process (tests/snmp_agent.py) answering tests/switch_tables.py.
NetBox is an in-memory stand-in with the response shapes of NetBox 4.7 (interfaces carry `cable`,
`connected_endpoints` and `mac_addresses`).
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.discovery.service import _switch_port
from lanvex.monitoring.notify import Monitor
from lanvex.switches.models import Switch
from lanvex.switches.ports import port_map
from lanvex.switches.scheduler import SwitchReadScheduler

from .snmp_agent import AUTH_KEY, COMMUNITY, PRIV_KEY, USER, Agent
from .switch_tables import A, B, C, D, E, F, access_switch, core_switch, oid, qfdb

V2 = {"version": "2c", "community": COMMUNITY}
V3 = {
    "version": "3",
    "username": USER,
    "auth_protocol": "sha",
    "auth_password": AUTH_KEY,
    "priv_protocol": "aes",
    "priv_password": PRIV_KEY,
}


class NetBox:
    """Devices: the two switches, the hosts on their ports. Cables: pc-a to sw-access Gi1/0/1 (as seen),
    sw-core Gi1/0/1 to the router's ge-0/0/1 (but the router's MAC is on ge-0/0/0)."""

    def __init__(self) -> None:
        self.devices = {
            30: "sw-access",
            31: "sw-core",
            7: "web-01",
            8: "pc-a",
            9: "phone",
            10: "pc-f",
            12: "router",
        }
        self.interfaces: list[dict[str, Any]] = []
        for dev, name, iid, mac in (
            (30, "Gi1/0/1", 301, None),
            (30, "Gi1/0/2", 302, None),
            (30, "Gi1/0/3", 303, None),
            (30, "Gi1/0/5", 305, None),
            (31, "Gi1/0/1", 311, None),
            (31, "Gi1/0/24", 324, None),
            (7, "eth0", 701, D),
            (8, "eth0", 801, A),
            (9, "eth0", 901, E),
            (10, "eth0", 1001, F),
            (12, "ge-0/0/0", 1200, C),
            (12, "ge-0/0/1", 1201, None),
        ):
            self.interfaces.append(
                {
                    "id": iid,
                    "device": dev,
                    "name": name,
                    "cable": None,
                    "type": {"value": "1000base-t", "label": "1000BASE-T (1GE)"},
                    "mac_addresses": [{"mac_address": mac.upper()}] if mac else [],
                }
            )
        self.interfaces.append(
            {"id": 309, "device": 30, "name": "Po1", "cable": None, "type": {"value": "lag", "label": "LAG"}}
        )
        self.cable(301, 801, 1)
        self.cable(311, 1201, 2)
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def iface(self, iid: int) -> dict[str, Any]:
        return next(i for i in self.interfaces if i["id"] == iid)

    def cable(self, a: int, b: int, cid: int) -> None:
        for x, y in ((a, b), (b, a)):
            peer = self.iface(y)
            self.iface(x)["cable"] = {"id": cid}
            self.iface(x)["connected_endpoints"] = [
                {
                    "id": y,
                    "name": peer["name"],
                    "device": {"id": peer["device"], "name": self.devices[peer["device"]]},
                }
            ]

    def out(self, i: dict[str, Any]) -> dict[str, Any]:
        return {
            **i,
            "device": {
                "id": i["device"],
                "name": self.devices[i["device"]],
                "display_url": f"/dcim/devices/{i['device']}/",
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        qp = request.url.params
        page = lambda rows: httpx.Response(200, json={"count": len(rows), "results": rows})  # noqa: E731
        if request.method == "POST":
            self.posted.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"id": 999, **json.loads(request.content)})
        if path.startswith("dcim/devices/") and path.count("/") == 3:
            oid = int(path.split("/")[2])
            if oid not in self.devices:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(
                200, json={"id": oid, "name": self.devices[oid], "display_url": f"/dcim/devices/{oid}/"}
            )
        if path == "dcim/devices/":
            names = {n.lower() for n in qp.get_list("name__ie")}
            rows = [
                {
                    "id": i,
                    "name": n,
                    "role": None,
                    "site": None,
                    "status": {"value": "active", "label": "Active"},
                }
                for i, n in self.devices.items()
                if not names or n.lower() in names
            ]
            return page(rows)
        if path == "dcim/interfaces/":
            if "device_id" in qp:
                return page([self.out(i) for i in self.interfaces if i["device"] == int(qp["device_id"])])
            macs = {m.upper() for m in qp.get_list("mac_address")}
            return page(
                [
                    self.out(i)
                    for i in self.interfaces
                    if {m["mac_address"] for m in i.get("mac_addresses", [])} & macs
                ]
            )
        if path in ("virtualization/interfaces/", "dcim/cables/", "dcim/device-roles/", "dcim/sites/"):
            return page([])
        return httpx.Response(404, json={"detail": "Not found."})


@pytest.fixture
def nb(netbox_mock: respx.MockRouter) -> NetBox:
    state = NetBox()
    netbox_mock.route().mock(side_effect=state.handle)
    return state


@pytest.fixture
async def agents() -> AsyncIterator[dict[str, Agent]]:
    found = {"access": Agent(access_switch()), "core": Agent(core_switch())}
    yield found
    for a in found.values():
        a.stop()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switches that do not answer give up quickly."""
    s = get_settings()
    monkeypatch.setattr(s, "snmp_timeout_seconds", 0.3)
    monkeypatch.setattr(s, "snmp_retries", 0)


async def add(
    admin: httpx.AsyncClient, name: str, port: int, creds: dict[str, Any] = V2, **extra: Any
) -> str:
    r = await admin.post(
        "/api/v1/switches",
        json={"name": name, "address": "127.0.0.1", "port": port, "credentials": creds, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def read(admin: httpx.AsyncClient, sid: str) -> dict[str, Any]:
    r = await admin.post(f"/api/v1/switches/{sid}/read")
    assert r.status_code == 200, r.text
    return r.json()


async def test_adding_and_reading_a_switch(
    admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent]
) -> None:
    port = agents["access"].port
    r = await admin.post(
        "/api/v1/switches/test", json={"address": "127.0.0.1", "port": port, "credentials": V3}
    )
    assert r.json() == {
        "status": "ok",
        "message": "Connected to sw-access.",
        "system": r.json()["system"],
    } and r.json()["system"]["description"].startswith("Example switch")

    sid = await add(admin, "Access 1", port, V3, netbox_device_id=30)
    listed = await admin.get("/api/v1/switches")
    assert listed.json()[0]["snmp"] == {
        "version": "3",
        "username": USER,
        "auth_protocol": "sha",
        "priv_protocol": "aes",
    }
    assert AUTH_KEY not in listed.text and PRIV_KEY not in listed.text
    assert listed.json()[0]["last_status"] is None and listed.json()[0]["read_interval"] == 15

    d = await read(admin, sid)
    assert (d["last_status"], d["sys_name"], d["ports_total"], d["ports_up"], d["mac_count"]) == (
        "ok",
        "sw-access",
        8,
        7,
        7,
    )
    assert d["netbox"]["name"] == "sw-access" and d["facts"]["system"]["location"] == "Rack 1"
    assert len(d["facts"]["ports"]) == 10
    assert AUTH_KEY not in json.dumps(d)

    r = await admin.post(
        "/api/v1/switches", json={"name": "access 1", "address": "127.0.0.1", "credentials": V2}
    )
    assert r.status_code == 409
    r = await admin.post(
        "/api/v1/switches",
        json={"name": "x", "address": "127.0.0.1", "credentials": V2, "netbox_device_id": 404},
    )
    assert r.status_code == 404

    r = await admin.patch(f"/api/v1/switches/{sid}", json={"credentials": V2, "read_interval_minutes": 0})
    assert r.json()["snmp"] == {
        "version": "2c",
        "username": None,
        "auth_protocol": None,
        "priv_protocol": None,
    }
    assert r.json()["read_interval"] == 0 and r.json()["next_read_at"] is None
    assert (await admin.patch(f"/api/v1/switches/{sid}", json={"netbox_device_id": None})).json()[
        "netbox"
    ] is None

    events = (await admin.get("/api/v1/audit-events", params={"action": "switch."})).json()
    actions = [e["action"] for e in events["results"]]
    assert {"switch.test", "switch.create", "switch.read", "switch.update"} <= set(actions)
    assert COMMUNITY not in json.dumps(events) and AUTH_KEY not in json.dumps(events)


async def test_readers_see_switches_but_cannot_change_them(
    client: httpx.AsyncClient, users: dict[str, str], nb: NetBox, agents: dict[str, Agent]
) -> None:
    from .conftest import login

    await login(client, "admin", users["admin"])
    sid = await add(client, "Access 1", agents["access"].port)
    await login(client, "reader", users["reader"])
    assert (await client.get(f"/api/v1/switches/{sid}")).status_code == 200
    for method, path in (
        ("POST", "/api/v1/switches"),
        ("POST", f"/api/v1/switches/{sid}/read"),
        ("PATCH", f"/api/v1/switches/{sid}"),
        ("DELETE", f"/api/v1/switches/{sid}"),
        ("POST", "/api/v1/switches/test"),
    ):
        r = await client.request(method, path, json={})
        assert r.status_code == 403, (method, path, r.status_code)


async def test_what_is_connected_to_each_port(
    admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent], database: Database
) -> None:
    access = await add(admin, "sw-access", agents["access"].port, netbox_device_id=30)
    core = await add(admin, "sw-core", agents["core"].port, netbox_device_id=31)
    await read(admin, access)
    await read(admin, core)

    ports = {
        (p["switch_name"], p["port"]): p for p in (await admin.get("/api/v1/discovery/ports")).json()["ports"]
    }
    assert set(ports) == {
        ("sw-access", "Gi1/0/1"),
        ("sw-access", "Gi1/0/2"),
        ("sw-access", "Gi1/0/3"),
        ("sw-access", "Gi1/0/5"),
        ("sw-access", "Po1"),
        ("sw-core", "Gi1/0/1"),
        ("sw-core", "Gi1/0/24"),
    }  # every MAC on the one port it is plugged into, not on the uplinks
    status = {k: (p["status"], p["uplink"]) for k, p in ports.items()}
    assert status == {
        ("sw-access", "Gi1/0/1"): ("documented", False),
        ("sw-access", "Gi1/0/2"): ("unidentified", False),
        ("sw-access", "Gi1/0/3"): ("undocumented", False),
        ("sw-access", "Gi1/0/5"): ("shared", False),
        ("sw-access", "Po1"): ("undocumented", True),
        ("sw-core", "Gi1/0/1"): ("different", False),
        ("sw-core", "Gi1/0/24"): ("undocumented", True),
    }
    gi1 = ports[("sw-access", "Gi1/0/1")]
    assert gi1["alias"] == "desk 1" and gi1["documented"] == "pc-a / eth0"
    (ep,) = gi1["endpoints"]
    assert (ep["kind"], ep["name"], ep["interface"], ep["mac"], ep["vlan"], ep["addresses"]) == (
        "device",
        "pc-a",
        "eth0",
        A,
        10,
        ["10.10.0.5"],  # from sw-core's ARP table
    )
    assert [e["mac"] for e in ports[("sw-access", "Gi1/0/2")]["endpoints"]] == [B]
    web = ports[("sw-access", "Gi1/0/3")]
    assert [(e["via"], e["name"], e["mac"], e["vlan"]) for e in web["endpoints"]] == [
        ("lldp", "web-01", D, 10)
    ]
    assert web["peer"] == "web-01 / eth0" and web["problem"] is None
    assert "phone" in ports[("sw-access", "Gi1/0/5")]["problem"]
    router = ports[("sw-core", "Gi1/0/1")]
    assert router["documented"] == "router / ge-0/0/1" and router["peer"] == "router / ge-0/0/0"
    uplink = ports[("sw-core", "Gi1/0/24")]
    assert uplink["peer"] == "sw-access / Po1" and "cannot have a cable" in uplink["problem"]
    assert [(e["kind"], e["name"]) for e in uplink["endpoints"]] == [("switch", "sw-access.example.net")]
    assert ports[("sw-access", "Po1")]["problem"] == (
        "Po1 is a link aggregation: record the cables of its member ports (Gi1/0/7, Gi1/0/8) in NetBox."
    )

    # Hosts in Discovery show the switch port their MAC address was learned on.
    async with database.sessionmaker() as s:
        pm = await port_map(s)
    where = _switch_port(pm, A.upper())
    assert where is not None and (where.switch_name, where.port, where.vlan, where.current) == (
        "sw-access",
        "Gi1/0/1",
        10,
        True,
    )
    assert _switch_port(pm, C).port == "Gi1/0/1" and _switch_port(pm, C).switch_name == "sw-core"  # type: ignore[union-attr]

    detail = (await admin.get(f"/api/v1/switches/{access}")).json()
    assert {p["port"] for p in detail["findings"]} == {"Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/5", "Po1"}

    # Recording: a preview first, then exactly that cable.
    items = [web["key"], gi1["key"], ports[("sw-access", "Gi1/0/5")]["key"]]
    plan = (await admin.post("/api/v1/discovery/ports/record", json={"items": items})).json()
    assert [a["problem"] is None for a in plan["actions"]] == [True, False, False]
    assert plan["actions"][0]["text"] == "Record a cable sw-access / Gi1/0/3 ↔ web-01 / eth0"
    assert nb.posted == []
    done = (
        await admin.post("/api/v1/discovery/ports/record", json={"items": items, "token": plan["token"]})
    ).json()
    assert len(done["done"]) == 1
    ((path, cable),) = nb.posted
    assert path == "dcim/cables/"
    assert cable["a_terminations"] == [{"object_type": "dcim.interface", "object_id": 303}]
    assert cable["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 701}]
    assert cable["description"].startswith("Seen by Lanvex on sw-access / Gi1/0/3 on ")

    # The topology shows what is seen but not cabled, apart from the cables.
    topo = (await admin.get("/api/v1/topology")).json()
    switch_nodes = {n["name"]: n["switch_id"] for n in topo["nodes"] if n["switch_id"]}
    assert switch_nodes == {"sw-access": access, "sw-core": core}
    seen = {(s["a"], s["b"], s["a_port"], s["b_port"], s["status"]) for s in topo["seen"]}
    assert ("device:31", "device:12", "Gi1/0/1", "ge-0/0/0", "different") in seen
    assert ("device:30", "device:7", "Gi1/0/3", "eth0", "undocumented") in seen


async def test_switch_not_linked_to_the_documentation(
    admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent]
) -> None:
    sid = await add(admin, "sw-access", agents["access"].port)
    await read(admin, sid)
    found = (await admin.get(f"/api/v1/switches/{sid}")).json()["findings"]
    assert {p["status"] for p in found} == {"not_linked"}
    plan = (await admin.post("/api/v1/discovery/ports/record", json={"items": [found[0]["key"]]})).json()
    assert "Link sw-access to its device" in plan["actions"][0]["problem"]


async def test_failed_reads_and_monitoring(
    admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent], database: Database, quick: None
) -> None:
    good = await add(admin, "good", agents["access"].port)
    bad_password = await add(
        admin, "bad-password", agents["access"].port, {**V3, "auth_password": "not-the-password"}
    )
    silent = await add(admin, "silent", agents["access"].port, {"version": "2c", "community": "wrong"})

    d = await read(admin, bad_password)
    assert d["last_status"] == "auth_failed" and d["facts"] is None
    assert "authentication password" in d["last_status_message"]
    d = await read(admin, silent)
    assert d["last_status"] == "unreachable"
    alerts = (await admin.get("/api/v1/alerts")).json()["alerts"]
    assert [(a["kind"], a["switch_id"], a["server_id"], a["title"]) for a in alerts] == [
        ("read_failed", bad_password, None, "bad-password: SNMP sign-in rejected")
    ]  # no answer at all is the probe's alert

    monitor = Monitor(database, get_settings(), lambda: None)
    for _ in range(3):
        await monitor.probe_switches()
    alerts = {(a["kind"], a["server_name"]) for a in (await admin.get("/api/v1/alerts")).json()["alerts"]}
    assert alerts == {
        ("read_failed", "bad-password"),
        ("unreachable", "bad-password"),
        ("unreachable", "silent"),
    }
    assert ("unreachable", "good") not in alerts

    # The password is fixed: the next read ends the alert.
    await admin.patch(f"/api/v1/switches/{bad_password}", json={"credentials": V3})
    assert (await read(admin, bad_password))["last_status"] == "ok"
    await monitor.probe_switches()
    left = {(a["kind"], a["server_name"]) for a in (await admin.get("/api/v1/alerts")).json()["alerts"]}
    assert left == {("unreachable", "silent")}
    assert (await admin.get(f"/api/v1/switches/{good}")).json()["last_status"] is None  # never read


async def test_scheduled_reads(
    admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent], database: Database
) -> None:
    due = await add(admin, "due", agents["access"].port)
    later = await add(admin, "later", agents["core"].port)
    off = await add(admin, "off", agents["core"].port, read_interval_minutes=0)
    async with database.sessionmaker() as s:
        (await s.get(Switch, due)).next_read_at = datetime.now(UTC) - timedelta(minutes=1)  # type: ignore[union-attr]
        await s.commit()
    scheduler = SwitchReadScheduler(database, get_settings(), lambda: None)
    assert await scheduler.run_due() == 1
    listed = {s["name"]: s for s in (await admin.get("/api/v1/switches")).json()}
    assert listed["due"]["last_status"] == "ok" and listed["later"]["last_status"] is None
    assert listed["off"]["next_read_at"] is None and listed["off"]["read_interval"] == 0
    assert datetime.fromisoformat(listed["due"]["next_read_at"]) > datetime.now(UTC) + timedelta(minutes=14)
    assert await scheduler.run_due() == 0
    del later, off


async def test_deleting_a_switch(admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent]) -> None:
    sid = await add(admin, "sw-access", agents["access"].port)
    await read(admin, sid)
    assert (await admin.delete(f"/api/v1/switches/{sid}")).status_code == 204
    assert (await admin.get(f"/api/v1/switches/{sid}")).status_code == 404
    assert (await admin.get("/api/v1/discovery/ports")).json()["ports"] == []


async def test_switch_alerts(admin: httpx.AsyncClient, nb: NetBox, agents: dict[str, Agent]) -> None:
    """Ports that led somewhere going down, devices new on a port, documented devices moving."""
    port = agents["access"].port
    access = await add(admin, "sw-access", port, netbox_device_id=30)
    core = await add(admin, "sw-core", agents["core"].port, netbox_device_id=31)
    await read(admin, access)
    await read(admin, core)
    alerts = lambda: admin.get("/api/v1/alerts", params={"state": "open"})  # noqa: E731
    assert (await alerts()).json()["alerts"] == []  # what the first read finds (B) is the starting point

    cfg = (await admin.get("/api/v1/monitoring")).json()["settings"]
    r = await admin.put("/api/v1/monitoring", json={**cfg, "unknown_device_ignore": ["vlan:20"]})
    assert r.status_code == 200, r.text
    bad = await admin.put("/api/v1/monitoring", json={**cfg, "unknown_device_ignore": ["vlan:5000"]})
    assert bad.status_code == 422

    # Po1 (the uplink) and Gi1/0/3 (web-01) go down, Gi1/0/4 stays down (nothing was on it), pc-a moves
    # from Gi1/0/1 to Gi1/0/6, two new devices appear: one in VLAN 10, one in the ignored VLAN 20.
    t = access_switch()
    t["1.3.6.1.2.1.2.2.1.8.9"] = 2
    t["1.3.6.1.2.1.2.2.1.8.3"] = 2
    t[f"1.3.6.1.2.1.17.7.1.2.2.1.2.10.{oid(A)}"] = 6
    t |= qfdb(10, "fc:ec:da:11:22:33", 6) | qfdb(20, "fc:ec:da:11:22:44", 5)
    agents["access"].stop()
    agents["access"] = Agent(t, port=port)
    await read(admin, access)

    found = {a["title"]: a for a in (await alerts()).json()["alerts"]}
    assert {(a["kind"], t, a["severity"]) for t, a in found.items()} == {
        ("port_down", "sw-access Po1 is down", "critical"),
        ("port_down", "sw-access Gi1/0/3 is down", "warning"),
        ("unknown_device", "Unknown device fc:ec:da:11:22:33 (Ubiquiti) on sw-access Gi1/0/6", "info"),
        ("device_moved", "pc-a / eth0 moved to sw-access Gi1/0/6", "info"),
    }
    assert found["sw-access Po1 is down"]["detail"] == ["It led to sw-core."]
    assert found["sw-access Gi1/0/3 is down"]["detail"] == ["It led to web-01."]
    assert found["pc-a / eth0 moved to sw-access Gi1/0/6"]["detail"] == ["Was on sw-access Gi1/0/1."]
    assert all(a["switch_id"] == access for a in found.values())
    ports = (await admin.get(f"/api/v1/switches/{access}")).json()["findings"]
    new = next(e for p in ports for e in p["endpoints"] if e["mac"] == "fc:ec:da:11:22:33")
    assert new["vendor"] == "Ubiquiti"

    # Gi1/0/3 was unplugged on purpose: acknowledging ends the alert, and the port is no longer watched.
    gi3 = found["sw-access Gi1/0/3 is down"]
    assert (await admin.post(f"/api/v1/alerts/{gi3['id']}/acknowledge")).status_code == 200
    await read(admin, access)
    titles = {a["title"] for a in (await alerts()).json()["alerts"]}
    assert "sw-access Gi1/0/3 is down" not in titles and "sw-access Po1 is down" in titles

    # The uplink is back: its alert ends; the events stay until acknowledged.
    agents["access"].stop()
    agents["access"] = Agent({**t, "1.3.6.1.2.1.2.2.1.8.9": 1}, port=port)
    await read(admin, access)
    open_kinds = sorted(a["kind"] for a in (await alerts()).json()["alerts"])
    assert open_kinds == ["device_moved", "unknown_device"]
