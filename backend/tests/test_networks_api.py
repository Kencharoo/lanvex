"""A server's networks: documented in Lanvex Core, compared with the server, and the change in between.

Lanvex Core is a small in-memory stand-in with NetBox 4.7's response shapes for the calls Lanvex makes;
the server and its helper are the scripted fakes from test_changes_api (NetworkManager facts of lab-nm).
The real helper runs on Fedora in test_networks_lab.py.
"""

import ipaddress
import json
from itertools import count
from typing import Any

import httpx
import pytest
import respx

from lanvex.changes.runner import ChangeRunner
from lanvex.integrations.linux.assemble import assemble
from lanvex.integrations.linux.commands import Cmd
from lanvex.integrations.linux.facts import DhcpFacts, DhcpLease, DhcpPool, DhcpReservation

from . import test_changes_api
from .test_changes_api import NM_ADDRESS, FakeServer, approve_and_run, with_lanvex_vlan
from .test_linux_parsing import load
from .test_servers_api import body as server_body

DEVICE, SITE = 7, 1
# The scripted server, clock, runner and settings from the change tests, as fixtures here too.
fake, clock, runner, automation = (
    test_changes_api.fake,
    test_changes_api.clock,
    test_changes_api.runner,
    test_changes_api.automation,
)


class CoreStandIn:
    """Device 7 (router-01, site 1) with eth0 and eth1; VLANs, prefixes, interfaces, IP addresses, IP ranges
    and tags, with the custom fields Lanvex Core defines."""

    def __init__(self) -> None:
        self.ids = count(100)
        self.tables: dict[str, dict[int, dict[str, Any]]] = {
            "dcim/interfaces": {
                11: {"id": 11, "name": "eth0", "device": DEVICE, "type": "1000base-t"},
                12: {"id": 12, "name": "eth1", "device": DEVICE, "type": "1000base-t"},
            },
            "ipam/ip-addresses": {},
            "ipam/ip-ranges": {},
            "ipam/vlans": {},
            "ipam/prefixes": {},
            "extras/tags": {},
        }
        self.calls: list[tuple[str, str, Any]] = []

    def t(self, kind: str) -> dict[int, dict[str, Any]]:
        return self.tables[kind]

    def ser(self, kind: str, o: dict[str, Any]) -> dict[str, Any]:
        out = dict(o)
        if kind == "dcim/interfaces":
            out["type"] = {"value": o["type"]}
            parent = self.t(kind).get(o.get("parent") or 0)
            out["parent"] = {"id": parent["id"], "name": parent["name"]} if parent else None
            vlan = self.t("ipam/vlans").get(o.get("untagged_vlan") or 0)
            out["untagged_vlan"] = (
                {"id": vlan["id"], "vid": vlan["vid"], "name": vlan["name"]} if vlan else None
            )
            out["tagged_vlans"] = []
        if kind == "ipam/ip-addresses":
            iface = self.t("dcim/interfaces").get(o.get("assigned_object_id") or 0)
            out["assigned_object"] = {"id": iface["id"], "name": iface["name"]} if iface else None
            out["custom_fields"] = {"mac_address": o.get("custom_fields", {}).get("mac_address")}
            out["tags"] = [self.t("extras/tags")[i] for i in o.get("tags", [])]
            out["dns_name"] = o.get("dns_name", "")
        if kind == "ipam/ip-ranges":
            out["custom_fields"] = {"dhcp_lease_time": o.get("custom_fields", {}).get("dhcp_lease_time")}
            out["tags"] = [self.t("extras/tags")[i] for i in o.get("tags", [])]
        return out

    def match(self, kind: str, o: dict[str, Any], qp: httpx.QueryParams) -> bool:
        if (dev := qp.get("device_id")) and kind == "dcim/interfaces" and o["device"] != int(dev):
            return False
        if (dev := qp.get("device_id")) and kind == "ipam/ip-addresses":
            iface = self.t("dcim/interfaces").get(o.get("assigned_object_id") or 0)
            if not iface or iface["device"] != int(dev):
                return False
        if (iid := qp.get("interface_id")) and o.get("assigned_object_id") != int(iid):
            return False
        if wanted := qp.get_list("address"):
            if not any(o["address"].split("/")[0] == w for w in wanted):
                return False
        if parents := qp.get_list("parent"):
            nets = [ipaddress.ip_network(p) for p in parents]
            hosts = [o["address"]] if "address" in o else [o["start_address"], o["end_address"]]
            if not all(any(ipaddress.ip_interface(h).ip in n for n in nets) for h in hosts):
                return False
        if (slug := qp.get("tag")) and not any(
            self.t("extras/tags")[i]["slug"] == slug for i in o.get("tags", [])
        ):
            return False
        if (slug := qp.get("slug")) and o.get("slug") != slug:
            return False
        if (vid := qp.get("vid")) and o.get("vid") != int(vid):
            return False
        if (prefix := qp.get("prefix")) and o.get("prefix") != prefix:
            return False
        return True

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/").rstrip("/")
        self.calls.append((request.method, path, json.loads(request.content) if request.content else None))
        if path == f"dcim/devices/{DEVICE}":
            return httpx.Response(
                200,
                json={
                    "id": DEVICE,
                    "name": "router-01",
                    "display": "router-01",
                    "display_url": f"https://core.test/dcim/devices/{DEVICE}/",
                    "site": {"id": SITE, "name": "Schule"},
                    "primary_ip4": None,
                    "primary_ip6": None,
                    "oob_ip": None,
                },
            )
        kind, _, oid = path.rpartition("/") if path.rsplit("/", 1)[-1].isdigit() else (path, "", "")
        if kind not in self.tables:
            return httpx.Response(404, json={"detail": "Not found."})
        table = self.t(kind)
        if oid:
            o = table[int(oid)]
            if request.method == "DELETE":
                del table[int(oid)]
                return httpx.Response(204)
            if request.method == "PATCH":
                patch = json.loads(request.content)
                if "custom_fields" in patch:
                    o["custom_fields"] = {**o.get("custom_fields", {}), **patch.pop("custom_fields")}
                o.update(patch)
            return httpx.Response(200, json=self.ser(kind, o))
        if request.method == "POST":
            b = json.loads(request.content)
            i = next(self.ids)
            table[i] = {"id": i, **b}
            return httpx.Response(201, json=self.ser(kind, table[i]))
        if kind == "ipam/ip-ranges" and (slug := request.url.params.get("tag")):
            if not any(t["slug"] == slug for t in self.t("extras/tags").values()):
                return httpx.Response(
                    400, json={"tag": [f"Select a valid choice. {slug} is not one of the available choices."]}
                )
        rows = [self.ser(kind, o) for o in table.values() if self.match(kind, o, request.url.params)]
        return httpx.Response(200, json={"count": len(rows), "next": None, "previous": None, "results": rows})

    def posted(self, kind: str) -> list[Any]:
        return [b for m, p, b in self.calls if m == "POST" and p == kind]


@pytest.fixture
def core(netbox_mock: respx.MockRouter) -> CoreStandIn:
    state = CoreStandIn()
    netbox_mock.route().mock(side_effect=state.handle)
    return state


async def router_01(admin: httpx.AsyncClient, fake: FakeServer, linked: bool = True) -> str:
    fake.facts = assemble(load("nm"))
    extra = {"netbox": {"object_type": "dcim.device", "object_id": DEVICE}} if linked else {}
    r = await admin.post(
        "/api/v1/servers", json=server_body(name="router-01", address=NM_ADDRESS, port=22, **extra)
    )
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).status_code == 200
    assert (await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")).status_code == 200
    assert (
        await admin.put(f"/api/v1/servers/{sid}/changes/enabled", json={"enabled": True})
    ).status_code == 200
    return sid


NEW = {
    "name": "vlan50",
    "parent": "eth0",
    "vlan_id": 50,
    "vlan_name": "Klasse 5",
    "address": "192.168.50.1/24",
    "pool_start": "192.168.50.100",
    "pool_end": "192.168.50.199",
    "lease": "2h",
}


async def test_a_new_network_is_documented_then_brought_to_the_server(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    assert view["linked"] is True and view["supported"] is True and view["networks"] == []
    assert view["in_sync"] is True and view["change"] is None

    r = await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)
    assert r.status_code == 201, r.text
    view = r.json()
    (n,) = view["networks"]
    assert (n["name"], n["parent"], n["vlan_id"], n["vlan_name"]) == ("vlan50", "eth0", 50, "Klasse 5")
    assert n["addresses"] == ["192.168.50.1/24"] and n["network"] == "192.168.50.0/24"
    assert n["status"] == "missing" and n["dhcp"] == "missing"
    assert n["pool"] | {"ip_range_id": None} == {
        "start": "192.168.50.100",
        "end": "192.168.50.199",
        "lease": "2h",
        "ip_range_id": None,
    }
    # In Lanvex Core: the VLAN, the prefix on it, a virtual interface on eth0, the gateway, the tagged pool.
    (vlan,) = core.posted("ipam/vlans")
    assert vlan == {"vid": 50, "name": "Klasse 5", "status": "active", "site": SITE}
    (prefix,) = core.posted("ipam/prefixes")
    assert prefix["prefix"] == "192.168.50.0/24" and prefix["scope_id"] == SITE
    (iface,) = core.posted("dcim/interfaces")
    assert iface | {"untagged_vlan": None} == {
        "device": DEVICE,
        "name": "vlan50",
        "type": "virtual",
        "parent": 11,
        "mode": "access",
        "untagged_vlan": None,
        "description": "Network added in Lanvex",
    }
    (pool,) = core.posted("ipam/ip-ranges")
    assert pool["start_address"] == "192.168.50.100/24" and pool["custom_fields"] == {"dhcp_lease_time": "2h"}
    assert {t["slug"] for t in core.posted("extras/tags")} == {"lanvex-gateway", "lanvex-dhcp"}

    # A fixed address for the printer.
    r = await admin.put(
        f"/api/v1/servers/{sid}/networks/vlan50/fixed",
        json={"ip": "192.168.50.20", "mac": "AA-BB-CC-00-00-02", "name": "drucker"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["networks"][0]["fixed"][0] | {"ip_id": None} == {
        "ip": "192.168.50.20",
        "mac": "aa:bb:cc:00:00:02",
        "name": "drucker",
        "ip_id": None,
    }
    for bad, field in (({"ip": "192.168.51.20"}, "Not in"), ({"ip": "192.168.50.1"}, "own address")):
        r = await admin.put(
            f"/api/v1/servers/{sid}/networks/vlan50/fixed", json={"mac": "aa:bb:cc:00:00:03"} | bad
        )
        assert r.status_code == 422 and field in r.text, r.text

    change = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()["change"]
    assert change["vlans"] == [
        {"name": "vlan50", "parent": "eth0", "id": 50, "addresses": ["192.168.50.1/24"]}
    ]
    assert change["dhcp"]["pools"] == [
        {
            "interface": "vlan50",
            "start": "192.168.50.100",
            "end": "192.168.50.199",
            "lease": "2h",
            "router": "192.168.50.1",
            "dns": ["192.168.50.1"],
            "reservations": [{"mac": "aa:bb:cc:00:00:02", "ip": "192.168.50.20", "name": "drucker"}],
        }
    ]
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change")
    assert r.status_code == 201, r.text
    prepared = r.json()
    assert prepared["kind"] == "networks"
    assert (
        prepared["summary"]
        == "Networks: add vlan50 (VLAN 50 on eth0, 192.168.50.1/24) · DHCP on vlan50 (1 fixed address)"
    )
    plan = next(p for c, p in fake.calls if c == Cmd.HELPER_PLAN)
    assert plan == change | {"kind": "networks"}
    done = await approve_and_run(admin, runner, prepared)
    assert done["status"] == "succeeded", done["log"]


async def test_the_server_in_line_with_its_leases(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)
    await admin.put(
        f"/api/v1/servers/{sid}/networks/vlan50/fixed",
        json={"ip": "192.168.50.20", "mac": "aa:bb:cc:00:00:02", "name": "drucker"},
    )
    # The server as the change left it: Lanvex's VLAN, its pool, and two devices with leases.
    facts = with_lanvex_vlan(assemble(load("nm")), "192.168.50.1")
    fake.facts = facts.model_copy(
        update={
            "dhcp": DhcpFacts(
                pools=[
                    DhcpPool(
                        interface="vlan50",
                        start="192.168.50.100",
                        end="192.168.50.199",
                        netmask="255.255.255.0",
                        lease="2h",
                        router="192.168.50.1",
                        dns=["192.168.50.1"],
                        reservations=[
                            DhcpReservation(mac="aa:bb:cc:00:00:02", ip="192.168.50.20", name="drucker")
                        ],
                    )
                ],
                leases=[
                    DhcpLease(ip="192.168.50.20", mac="aa:bb:cc:00:00:02", hostname="drucker"),
                    DhcpLease(ip="192.168.50.150", mac="aa:bb:cc:00:00:07", hostname="laptop-7"),
                    DhcpLease(ip="10.9.9.9", mac="aa:bb:cc:00:00:09"),
                ],
            )
        }
    )
    await admin.post(f"/api/v1/servers/{sid}/collect")
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    (n,) = view["networks"]
    assert n["status"] == "in_sync" and n["dhcp"] == "in_sync", n
    assert view["in_sync"] is True and view["change"] is None
    assert [(x["ip"], x["fixed"]) for x in n["leases"]] == [
        ("192.168.50.20", True),
        ("192.168.50.150", False),
    ]
    assert [x["ip"] for x in view["other_leases"]] == ["10.9.9.9"]
    # Devices with a lease are discovered hosts, seen by the server that gave them their address.
    hosts = {h["address"]: h for h in (await admin.get("/api/v1/discovery/hosts")).json()["hosts"]}
    laptop = hosts["192.168.50.150"]
    assert [x["server_name"] for x in laptop["seen_by"]] == ["router-01"]
    assert laptop["seen_by"][0]["interface"] == "vlan50" and laptop["seen_by"][0]["mac"] == "aa:bb:cc:00:00:07"
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_change"

    # Making the laptop's lease a fixed address changes the documentation, then the server's DHCP.
    await admin.put(
        f"/api/v1/servers/{sid}/networks/vlan50/fixed",
        json={"ip": "192.168.50.150", "mac": "aa:bb:cc:00:00:07", "name": "laptop-7"},
    )
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    assert view["networks"][0]["status"] == "in_sync" and view["networks"][0]["dhcp"] == "differs"
    assert len(view["change"]["dhcp"]["pools"][0]["reservations"]) == 2
    view = (await admin.delete(f"/api/v1/servers/{sid}/networks/vlan50/fixed/192.168.50.150")).json()
    assert view["in_sync"] is True

    # A protected name: Lanvex leaves that VLAN exactly as it is, it never removes it.
    await admin.patch(f"/api/v1/servers/{sid}", json={"protected_interfaces": ["vlan*"]})
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    assert view["networks"][0]["status"] == "protected" and view["in_sync"] is True
    await admin.patch(f"/api/v1/servers/{sid}", json={"protected_interfaces": []})

    # Removing the network from the documentation: the change removes it from the server.
    r = await admin.delete(f"/api/v1/servers/{sid}/networks/vlan50")
    assert r.status_code == 200, r.text
    view = r.json()
    (n,) = view["networks"]
    assert n["status"] == "removed" and n["dhcp"] == "removed"
    assert view["change"] == {"kind": "networks", "vlans": [], "dhcp": {"pools": []}}
    assert not core.t("dcim/interfaces").get(n.get("netbox_interface_id") or 0)
    assert not core.t("ipam/ip-ranges")
    prepared = (await admin.post(f"/api/v1/servers/{sid}/networks/change")).json()
    assert prepared["summary"] == "Networks: remove vlan50 · no DHCP"
    assert any("disconnects every device" in w for w in prepared["preview"]["warnings"])


async def test_what_lanvex_refuses_to_document(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    for over, text in (
        ({"parent": "eno2"}, "does not document eno2"),
        ({"name": "eth1"}, "already has an interface eth1"),
        ({"pool_start": "192.168.50.1"}, "own address 192.168.50.1 is inside the pool"),
        ({"pool_end": None}, "or neither"),
        ({"address": "192.168.50.0/24"}, "network or broadcast address"),
        ({"lease": "forever"}, "lease time"),
    ):
        r = await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW | over)
        assert r.status_code == 422 and text in r.text, (over, r.text)
    assert core.posted("ipam/vlans") == []  # nothing was written

    r = await admin.patch(f"/api/v1/servers/{sid}", json={"unlink_netbox": True})
    assert r.status_code == 200
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    assert view["linked"] is False and view["change"] is None
    r = await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)
    assert r.status_code == 422 and "Link router-01 to its device" in r.text
