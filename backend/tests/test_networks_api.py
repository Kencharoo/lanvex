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

DEVICE, SITE, NAS = 7, 1, 8
DEVICES = {DEVICE: "router-01", NAS: "nas"}
# The scripted server, clock, runner and settings from the change tests, as fixtures here too.
fake, clock, runner, automation = (
    test_changes_api.fake,
    test_changes_api.clock,
    test_changes_api.runner,
    test_changes_api.automation,
)


class CoreStandIn:
    """Device 7 (router-01, site 1) with eth0 and eth1, device 8 (nas) with eth0 and its MAC address; VLANs,
    prefixes, interfaces, IP addresses, IP ranges (no two overlapping, as NetBox has it) and tags, with the
    custom fields Lanvex Core defines."""

    def __init__(self) -> None:
        self.ids = count(100)
        self.tables: dict[str, dict[int, dict[str, Any]]] = {
            "dcim/interfaces": {
                11: {"id": 11, "name": "eth0", "device": DEVICE, "type": "1000base-t"},
                12: {"id": 12, "name": "eth1", "device": DEVICE, "type": "1000base-t"},
                21: {
                    "id": 21,
                    "name": "eth0",
                    "device": NAS,
                    "type": "1000base-t",
                    "mac_addresses": [{"id": 1, "mac_address": "AA:BB:CC:00:00:05"}],
                },
            },
            "virtualization/interfaces": {},
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
            out["device"] = {"id": o["device"], "name": DEVICES[o["device"]]}
            out["mac_addresses"] = o.get("mac_addresses", [])
            out["type"] = {"value": o["type"]}
            parent = self.t(kind).get(o.get("parent") or 0)
            out["parent"] = {"id": parent["id"], "name": parent["name"]} if parent else None
            vlan = self.t("ipam/vlans").get(o.get("untagged_vlan") or 0)
            out["untagged_vlan"] = (
                {"id": vlan["id"], "vid": vlan["vid"], "name": vlan["name"]} if vlan else None
            )
            out["tagged_vlans"] = []
            out["custom_fields"] = {"lanvex_access": o.get("custom_fields", {}).get("lanvex_access")}
        if kind == "ipam/ip-addresses":
            iface = self.t("dcim/interfaces").get(o.get("assigned_object_id") or 0)
            out["assigned_object"] = (
                {
                    "id": iface["id"],
                    "name": iface["name"],
                    "device": {"id": iface["device"], "name": DEVICES[iface["device"]]},
                }
                if iface
                else None
            )
            out["assigned_object_type"] = "dcim.interface" if iface else None
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
        if (mac := qp.get("mac_address")) and not any(
            m["mac_address"].lower() == mac.lower() for m in o.get("mac_addresses", [])
        ):
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
            if kind == "ipam/ip-ranges":
                lo, hi = (ipaddress.ip_interface(b[k]).ip for k in ("start_address", "end_address"))
                for r in table.values():
                    r_lo, r_hi = (ipaddress.ip_interface(r[k]).ip for k in ("start_address", "end_address"))
                    if r_lo <= hi and lo <= r_hi:
                        return httpx.Response(
                            400,
                            json={
                                "__all__": [
                                    f"Defined addresses overlap with range {r['start_address']}-{r_hi}"
                                ]
                            },
                        )
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
        "device": None,
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
    assert (
        laptop["seen_by"][0]["interface"] == "vlan50" and laptop["seen_by"][0]["mac"] == "aa:bb:cc:00:00:07"
    )
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
    assert view["change"] == {
        "kind": "networks",
        "vlans": [],
        "dhcp": {"pools": []},
        "access": {"rules": []},
        "takeover": None,
    }
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


async def test_access_rules_are_documented_compared_and_brought_to_the_server(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)
    rule = {
        "internet": True,
        "destinations": [
            {"network": "192.168.178.3", "ports": ["443/tcp", "80/tcp"]},
            {"network": "10.60.0.9/16"},
        ],
        "services": ["22/tcp"],
    }
    r = await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/access", json=rule)
    assert r.status_code == 200, r.text
    (n,) = r.json()["networks"]
    stored = {
        "internet": True,
        "destinations": [
            {"network": "10.60.0.0/16", "ports": []},
            {"network": "192.168.178.3/32", "ports": ["80/tcp", "443/tcp"]},
        ],
        "services": ["22/tcp"],
    }
    assert (
        n["access"] == stored | {"dns_filter": None} and n["access_status"] is None
    )  # comes with the network
    iface = next(i for i in core.t("dcim/interfaces").values() if i["name"] == "vlan50")
    assert iface["custom_fields"]["lanvex_access"] == stored  # documented in Lanvex Core
    change = r.json()["change"]
    assert change["access"] == {"rules": [{"interface": "vlan50", "dns_filter": None} | stored]}
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change")
    assert r.status_code == 201, r.text
    assert (
        "access vlan50: Internet, 10.60.0.0/16, 192.168.178.3/32 (80/tcp, 443/tcp), server 22/tcp"
        in r.json()["summary"]
    )
    await admin.post(f"/api/v1/changes/{r.json()['id']}/cancel")

    # The server has the network, with other rules applied: they differ; with these, in line.
    facts = with_lanvex_vlan(assemble(load("nm")), "192.168.50.1")
    fake.facts = facts.model_copy(update={"lanvex_access": [{"interface": "vlan50", "internet": True}]})
    await admin.post(f"/api/v1/servers/{sid}/collect")
    (n,) = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()["networks"]
    assert n["access_status"] == "differs" and n["access_on_server"]["internet"] is True
    fake.facts = facts.model_copy(update={"lanvex_access": [{"interface": "vlan50"} | stored]})
    await admin.post(f"/api/v1/servers/{sid}/collect")
    (n,) = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()["networks"]
    assert n["access_status"] == "in_sync"

    # Back to the default: the custom field is emptied.
    r = await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/access", json={})
    assert r.status_code == 200 and iface["custom_fields"]["lanvex_access"] is None
    assert r.json()["networks"][0]["access_status"] == "differs"

    # Rules Lanvex cannot use (edited by hand in Lanvex Core) are pointed out; the server keeps its rules.
    iface["custom_fields"]["lanvex_access"] = {"internet": "maybe"}
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    (n,) = view["networks"]
    assert n["access_status"] == "invalid" and "cannot be used" in n["access_problem"]
    assert view["change"] is None or view["change"]["access"]["rules"][0]["internet"] is True

    for bad, message in (
        ({"destinations": [{"network": "192.168.50.7"}]}, "own network"),
        ({"services": ["ssh"]}, "not a port"),
        ({"destinations": [{"network": "0.0.0.0/0"}]}, "Internet"),
    ):
        r = await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/access", json=bad)
        assert r.status_code == 422 and message in r.text, r.text


async def test_a_dns_filter_is_documented_with_the_access_rules(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)
    rule = {
        "internet": True,
        "dns_filter": {"block": ["TikTok.com", "*.instagram.com"], "allow": ["edu.tiktok.com"]},
    }
    r = await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/access", json=rule)
    assert r.status_code == 200, r.text
    (n,) = r.json()["networks"]
    assert n["access"]["dns_filter"] == {
        "block": ["instagram.com", "tiktok.com"],
        "allow": ["edu.tiktok.com"],
    }
    assert r.json()["change"]["access"]["rules"][0]["dns_filter"] == n["access"]["dns_filter"]
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change")
    assert r.status_code == 201 and "DNS filter (2 blocked)" in r.json()["summary"], r.text
    await admin.post(f"/api/v1/changes/{r.json()['id']}/cancel")

    # Without DHCP there, devices would never learn the filter.
    await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/pool", json={"start": None, "end": None})
    r = await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/access", json=rule)
    assert r.status_code == 422 and "needs a DHCP pool" in r.text, r.text
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    (n,) = view["networks"]
    assert "stays off until there is one" in n["access_problem"]
    assert "dns_filter" not in (
        view["change"]["access"]["rules"][0] if view["change"]["access"]["rules"] else {}
    ) or (view["change"]["access"]["rules"][0]["dns_filter"] is None)


NV_FOUND = {
    "ok": True,
    "found": True,
    "vlans": [
        {
            "name": "gast",
            "vlan_id": 50,
            "address": "192.168.50.1/24",
            "description": "Gaeste",
            "parent": "eth0",
            "dhcp": {"enabled": True, "start": "192.168.50.100", "end": "192.168.50.199", "lease": "12h"},
            "reservations": [{"mac": "aa:bb:cc:00:00:02", "ip": "192.168.50.20", "name": "drucker"}],
            "access": {
                "internet": True,
                "haupt_lan": True,
                "switch_gui": True,
                "vlans": ["bw"],
                "services": ["dhcp", "dns", "ssh", "samba", "minecraft"],
                "ports": ["8080/tcp"],
            },
            "dns_filter": {"enabled": True, "lists": ["kinder"], "block": ["tiktok.com"], "allow": []},
        },
        {
            "name": "bw",
            "vlan_id": 60,
            "address": "192.168.60.1/24",
            "parent": "eth0",
            "dhcp": {"enabled": False},
            "access": {"internet": False, "services": ["dhcp", "dns"]},
        },
    ],
    "policies": {
        "nv-gast-egress": '<policy target="ACCEPT"><rule family="ipv4" priority="-100">'
        '<destination address="192.168.178.0/24"/><accept/></rule><rule family="ipv4" priority="0">'
        '<destination address="10.0.0.0/8"/><drop/></rule></policy>',
        "nv-gast-switch": '<policy target="REJECT"><masquerade/><rule family="ipv4"><destination '
        'address="192.168.178.3/32"/><port port="443" protocol="tcp"/><accept/></rule><rule family="ipv4">'
        '<destination address="192.168.178.3/32"/><port port="80" protocol="tcp"/><accept/></rule>'
        '<rule family="ipv4"><destination address="192.168.178.3/32"/><protocol value="icmp"/><accept/></rule>'
        "</policy>",
    },
    "files": [],
    "profiles": ["vlan-gast", "vlan-bw"],
    "units": {},
}


async def test_netzverwaltung_is_documented_then_taken_over(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await router_01(admin, fake)
    r = await admin.post(f"/api/v1/servers/{sid}/networks/import", json={"source": "netzverwaltung"})
    assert r.status_code == 200 and r.json()["found"] is False  # not installed: nothing to do

    fake.inspected = NV_FOUND
    r = await admin.post(f"/api/v1/servers/{sid}/networks/import", json={"source": "netzverwaltung"})
    assert r.status_code == 200, r.text
    report = {n["name"]: n for n in r.json()["networks"]}
    assert report["gast"]["result"] == "documented" and report["bw"]["result"] == "documented"
    assert any("minecraft" in x for x in report["gast"]["notes"])  # what Lanvex could not carry over
    assert any("kinder" in x for x in report["gast"]["notes"])
    gast = next(n for n in r.json()["view"]["networks"] if n["name"] == "gast")
    assert gast["pool"]["start"] == "192.168.50.100" and gast["fixed"][0]["mac"] == "aa:bb:cc:00:00:02"
    assert gast["access"] == {
        "internet": True,
        "destinations": [
            {"network": "192.168.60.0/24", "ports": []},  # bw
            {"network": "192.168.178.0/24", "ports": []},  # the main LAN
            {"network": "192.168.178.3/32", "ports": ["80/tcp", "443/tcp"]},  # the switch's web interface
        ],
        "services": ["22/tcp", "137-138/udp", "139/tcp", "445/tcp", "8080/tcp"],
        "dns_filter": {"block": ["tiktok.com"], "allow": []},
    }
    again = (await admin.post(f"/api/v1/servers/{sid}/networks/import", json={})).json()
    assert {n["result"] for n in again["networks"]} == {"already documented"}

    # On the server, both are Netzverwaltung's: Lanvex leaves them alone unless it takes them over.
    facts = with_lanvex_vlan(assemble(load("nm")), "192.168.50.1")
    vlan = facts.interfaces[-1].model_copy(update={"name": "gast", "manager_profile": "vlan-gast"})
    bw = vlan.model_copy(update={"name": "bw", "vlan_id": 60, "manager_profile": "vlan-bw", "addresses": []})
    fake.facts = facts.model_copy(update={"interfaces": [*facts.interfaces[:-1], vlan, bw]})
    await admin.post(f"/api/v1/servers/{sid}/collect")
    view = (await admin.get(f"/api/v1/servers/{sid}/networks")).json()
    assert {n["name"]: n["status"] for n in view["networks"]} == {"bw": "foreign", "gast": "foreign"}
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change?takeover=netzverwaltung")
    assert r.status_code == 201, r.text
    assert r.json()["summary"].startswith("Networks: take over from Netzverwaltung · add bw")
    plan = [p for c, p in fake.calls if c == Cmd.HELPER_PLAN][-1]
    assert plan["takeover"] == "netzverwaltung" and [v["name"] for v in plan["vlans"]] == ["bw", "gast"]
    assert plan["access"]["rules"][0]["interface"] == "gast"


def nv_found_with_nas() -> dict[str, Any]:
    """Netzverwaltung's setup with gast's fixed address for the NAS (documented on its eth0 in Lanvex Core)."""
    found = json.loads(json.dumps(NV_FOUND))
    found["vlans"][0]["reservations"] = [{"mac": "AA:BB:CC:00:00:05", "ip": "192.168.50.20", "name": "nas"}]
    return found


async def test_netzverwaltung_into_networks_documented_before(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    """As on a server documented in Lanvex Core before Lanvex served networks: gast is there without a pool,
    fixed address or access rules, its pool is a plain IP range, and the NAS has its address on its eth0."""
    sid = await router_01(admin, fake)
    facts = with_lanvex_vlan(assemble(load("nm")), "192.168.50.1")
    vlan = facts.interfaces[-1].model_copy(update={"name": "gast", "manager_profile": "vlan-gast"})
    bw = vlan.model_copy(update={"name": "bw", "vlan_id": 60, "manager_profile": "vlan-bw", "addresses": []})
    fake.facts = facts.model_copy(update={"interfaces": [*facts.interfaces[:-1], vlan, bw]})
    await admin.post(f"/api/v1/servers/{sid}/collect")
    r = await admin.post(
        f"/api/v1/servers/{sid}/networks",
        json=NEW | {"name": "gast", "pool_start": None, "pool_end": None, "lease": "12h"},
    )
    assert r.status_code == 201, r.text
    core.t("ipam/ip-ranges")[61] = {
        "id": 61,
        "start_address": "192.168.50.100/24",
        "end_address": "192.168.50.199/24",
        "status": "active",
        "tags": [],
    }
    core.t("ipam/ip-addresses")[62] = {
        "id": 62,
        "address": "192.168.50.20/24",
        "status": "active",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": 21,
    }
    fake.inspected = nv_found_with_nas()

    # Taking over now would leave gast without DHCP, the NAS without its address and nobody on the Internet.
    r = await admin.post(f"/api/v1/servers/{sid}/networks/change?takeover=netzverwaltung")
    assert r.status_code == 422 and r.json()["error"]["code"] == "takeover_incomplete", r.text
    for gap in (
        "bw is not documented",
        "gast: Netzverwaltung serves DHCP (192.168.50.100 to 192.168.50.199); Lanvex Core documents no pool.",
        "the fixed address 192.168.50.20 for aa:bb:cc:00:00:05 (nas) is not documented",
        "gast: Netzverwaltung lets its devices reach the Internet;",
    ):
        assert gap in r.json()["error"]["message"], r.text

    # A pool overlapping the plain IP range is refused by name, not by NetBox's message.
    r = await admin.put(
        f"/api/v1/servers/{sid}/networks/gast/pool", json={"start": "192.168.50.150", "end": "192.168.50.250"}
    )
    assert r.status_code == 422 and "has the IP range 192.168.50.100 to 192.168.50.199" in r.text, r.text

    r = await admin.post(f"/api/v1/servers/{sid}/networks/import", json={"source": "netzverwaltung"})
    assert r.status_code == 200, r.text
    report = {n["name"]: n for n in r.json()["networks"]}
    assert report["bw"]["result"] == "documented"
    assert report["gast"]["result"] == "completed"
    for note in (
        "DHCP pool 192.168.50.100 to 192.168.50.199 added.",
        "Fixed address 192.168.50.20 for aa:bb:cc:00:00:05 added.",
        "Access rules added.",
    ):
        assert note in report["gast"]["notes"], report["gast"]
    # The range Lanvex Core had became the pool; the NAS's address stays as documented, with its MAC.
    assert not core.posted("ipam/ip-ranges")
    assert [t["slug"] for t in core.ser("ipam/ip-ranges", core.t("ipam/ip-ranges")[61])["tags"]] == [
        "lanvex-dhcp"
    ]
    nas = core.t("ipam/ip-addresses")[62]
    assert nas["status"] == "active" and nas["assigned_object_id"] == 21
    assert nas["custom_fields"]["mac_address"] == "aa:bb:cc:00:00:05" and nas["dns_name"] == "nas"
    gast = next(n for n in r.json()["view"]["networks"] if n["name"] == "gast")
    assert gast["pool"]["ip_range_id"] == 61
    assert gast["fixed"] == [
        {"ip": "192.168.50.20", "mac": "aa:bb:cc:00:00:05", "name": "nas", "ip_id": 62, "device": "nas eth0"}
    ]
    again = (
        await admin.post(f"/api/v1/servers/{sid}/networks/import", json={"source": "netzverwaltung"})
    ).json()
    assert {n["result"] for n in again["networks"]} == {"already documented"}

    r = await admin.post(f"/api/v1/servers/{sid}/networks/change?takeover=netzverwaltung")
    assert r.status_code == 201, r.text
    plan = [p for c, p in fake.calls if c == Cmd.HELPER_PLAN][-1]
    (pool,) = plan["dhcp"]["pools"]
    assert pool["interface"] == "gast" and pool["reservations"][0]["mac"] == "aa:bb:cc:00:00:05"


async def test_fixed_addresses_on_documented_devices(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, automation: None
) -> None:
    sid = await router_01(admin, fake)
    assert (await admin.post(f"/api/v1/servers/{sid}/networks", json=NEW)).status_code == 201
    core.t("dcim/interfaces")[12]["mac_addresses"] = [{"id": 2, "mac_address": "aa:bb:cc:00:00:07"}]
    core.t("dcim/interfaces")[22] = {"id": 22, "name": "eth1", "device": NAS, "type": "1000base-t"}
    for i, (ip, iface) in enumerate((("192.168.50.30", 21), ("192.168.50.31", 12), ("192.168.50.34", 22))):
        core.t("ipam/ip-addresses")[70 + i] = {
            "id": 70 + i,
            "address": f"{ip}/24",
            "status": "active",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface,
        }

    async def fixed(ip: str, mac: str) -> httpx.Response:
        return await admin.put(f"/api/v1/servers/{sid}/networks/vlan50/fixed", json={"ip": ip, "mac": mac})

    r = await fixed("192.168.50.30", "aa:bb:cc:00:00:09")
    assert (
        r.status_code == 422 and "nas eth0, whose MAC address in" in r.text and "aa:bb:cc:00:00:05" in r.text
    )
    r = await fixed("192.168.50.31", "aa:bb:cc:00:00:09")
    assert r.status_code == 422 and "router-01's own address on eth1" in r.text, r.text
    r = await fixed("192.168.50.34", "aa:bb:cc:00:00:07")
    assert r.status_code == 422 and "aa:bb:cc:00:00:07 is router-01 eth1" in r.text, r.text
    r = await fixed("192.168.50.30", "AA:BB:CC:00:00:05")
    assert r.status_code == 200, r.text
    (f,) = r.json()["networks"][0]["fixed"]
    assert f["ip"] == "192.168.50.30" and f["device"] == "nas eth0"
    assert core.t("ipam/ip-addresses")[70]["status"] == "active"  # as documented
    r = await fixed("192.168.50.34", "aa:bb:cc:00:00:08")  # nas eth1 has no MAC address in Lanvex Core
    assert r.status_code == 200, r.text


def test_what_a_takeover_would_drop() -> None:
    from lanvex.changes.schemas import NetworksRequest
    from lanvex.networks.service import takeover_gaps

    found = nv_found_with_nas()
    gast = {
        "internet": True,
        "destinations": [{"network": "192.168.60.0/24"}, {"network": "192.168.178.0/24"}],
        "services": ["137-138/udp", "139/tcp", "445/tcp", "8080/tcp"],
    }
    req = NetworksRequest.model_validate(
        {
            "kind": "networks",
            "takeover": "netzverwaltung",
            "vlans": [
                {"name": "gast", "parent": "eth0", "id": 50, "addresses": ["192.168.50.1/24"]},
                {"name": "bw", "parent": "eth0", "id": 60, "addresses": ["192.168.60.1/24"]},
            ],
            "dhcp": {
                "pools": [
                    {
                        "interface": "gast",
                        "start": "192.168.50.100",
                        "end": "192.168.50.199",
                        "reservations": [{"mac": "aa:bb:cc:00:00:05", "ip": "192.168.50.20"}],
                    }
                ]
            },
            "access": {"rules": [{"interface": "gast", **gast}]},
        }
    )
    missing, losses = takeover_gaps(found, req)
    assert missing == []
    assert losses == [
        "Devices on gast no longer reach 192.168.178.3/32 on 80/tcp, 443/tcp; 22/tcp on the server: Lanvex Core "
        "documents less than Netzverwaltung allows.",
        "DNS on gast is no longer filtered: Lanvex Core documents no DNS filter for it.",
    ]
    # A fixed address with another MAC address is missing, not kept.
    req.dhcp.pools[0].reservations[0].mac = "aa:bb:cc:00:00:06"  # type: ignore[union-attr]
    missing, _ = takeover_gaps(found, req)
    assert missing == ["gast: the fixed address 192.168.50.20 for aa:bb:cc:00:00:05 (nas) is not documented."]
