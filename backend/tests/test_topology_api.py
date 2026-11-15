"""The topology map's data: devices and circuits as nodes, documented cables as links.

The NetBox responses are real: captured from NetBox 4.7.5 (Lanvex Core) with a small sample network
(tests/fixtures/topology). Two sites, core and access switches, a patch panel, a server, an
unconnected switch, and a circuit joining both sites.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from lanvex.core.db import Database
from lanvex.servers.models import AuthMethod, Server
from lanvex.topology.router import build

FIXTURES = Path(__file__).parent / "fixtures" / "topology"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def mock_netbox(
    netbox_mock: respx.MockRouter,
    *,
    devices: dict[str, Any] | None = None,
    cables: dict[str, Any] | None = None,
) -> None:
    netbox_mock.get("dcim/devices/").respond(200, json=devices or fixture("devices"))
    netbox_mock.get("dcim/cables/").respond(200, json=cables or fixture("cables"))
    netbox_mock.get("dcim/device-roles/").respond(200, json=fixture("device-roles"))
    netbox_mock.get("dcim/sites/").respond(200, json=fixture("sites"))


def by_name(topology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in topology["nodes"]}


def test_every_cable_becomes_a_link_between_its_devices() -> None:
    nodes, links, skipped = build(
        fixture("devices")["results"], fixture("cables")["results"], fixture("device-roles")["results"], {}
    )
    names = {n.id: n.name for n in nodes}
    pairs = {(names[link.a], names[link.b]) for link in links}
    assert ("topo-core-01", "topo-core-02") in pairs
    assert ("topo-srv-01", "topo-acc-01") in pairs
    assert len(links) == 9 and skipped == 0


def test_link_kinds_ports_and_status() -> None:
    _, links, _ = build(
        fixture("devices")["results"], fixture("cables")["results"], fixture("device-roles")["results"], {}
    )
    by_cable = {link.cable_id: link for link in links}
    core = by_cable[1]
    assert (
        core.kind == "data"
        and core.a_ports == ["Gi1/0/1"]
        and core.label == "core-link"
        and core.type == "mmf"
    )
    assert by_cable[4].status and by_cable[4].status.value == "planned"
    assert by_cable[5].kind == "patch" and by_cable[5].b_ports == ["F1"]  # access switch to patch panel front
    assert by_cable[6].kind == "patch" and by_cable[6].a_ports == ["R1"]
    assert by_cable[8].kind == "circuit" and by_cable[8].b_ports == ["Side A"]


def test_circuits_are_one_node_joining_both_sites() -> None:
    nodes, links, _ = build(
        fixture("devices")["results"], fixture("cables")["results"], fixture("device-roles")["results"], {}
    )
    circuit = next(n for n in nodes if n.kind == "circuit")
    assert circuit.name == "CID-1001" and circuit.provider == "LabISP"
    ends = {link.a for link in links if link.b == circuit.id}
    assert len(ends) == 2  # the core switch at Topo Lab and the branch switch at Topo Branch


def test_devices_carry_role_colour_site_and_type() -> None:
    nodes, _, _ = build(
        fixture("devices")["results"], fixture("cables")["results"], fixture("device-roles")["results"], {}
    )
    core = next(n for n in nodes if n.name == "topo-core-01")
    assert core.color == "#1f6feb" and core.role and core.role.name == "Core switch"
    assert core.site and core.site.name == "Topo Lab" and core.device_type == "Switch 48"
    assert core.netbox_url and core.netbox_url.endswith("/dcim/devices/3/")
    spare = next(n for n in nodes if n.name == "topo-spare-01")
    assert spare.external is False  # unconnected devices are part of the map too


def test_cables_to_things_without_a_device_are_counted_not_drawn() -> None:
    cable = fixture("cables")["results"][0] | {
        "id": 99,
        "b_terminations": [
            {"object_type": "dcim.powerfeed", "object_id": 1, "object": {"id": 1, "name": "Feed A"}}
        ],
    }
    _, links, skipped = build(fixture("devices")["results"], [cable], fixture("device-roles")["results"], {})
    assert links == [] and skipped == 1


async def test_site_view_includes_devices_at_the_far_end_of_its_cables(
    admin: httpx.AsyncClient, netbox_mock: respx.MockRouter
) -> None:
    devices = fixture("devices")
    branch_only = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [d for d in devices["results"] if d["site"]["slug"] == "topo-branch"],
    }
    cables = fixture("cables")
    branch_cables = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [c for c in cables["results"] if c["id"] == 9],
    }
    mock_netbox(netbox_mock, devices=branch_only, cables=branch_cables)
    r = await admin.get("/api/v1/topology", params={"site_id": 4})
    assert r.status_code == 200
    body = r.json()
    nodes = by_name(body)
    assert set(nodes) == {"topo-br-sw-01", "CID-1001"}
    assert nodes["topo-br-sw-01"]["external"] is False
    assert body["site_id"] == 4 and [s["name"] for s in body["sites"]] == ["Topo Branch", "Topo Lab"]
    sent = [c.request for c in netbox_mock.calls if c.request.url.path.endswith("/dcim/cables/")]
    assert sent and sent[0].url.params["site_id"] == "4"


async def test_lanvex_servers_are_marked_on_their_devices(
    reader: httpx.AsyncClient, netbox_mock: respx.MockRouter, database: Database
) -> None:
    mock_netbox(netbox_mock)
    async with database.sessionmaker() as s:
        server = Server(
            name="srv-01",
            address="192.0.2.10",
            username="lanvex",
            auth_method=AuthMethod.GENERATED_KEY,
            credential_ciphertext="x",
            host_key="ssh-ed25519 AAAA",
            host_key_fingerprint="SHA256:x",
            netbox_object_type="dcim.device",
            netbox_object_id=9,
        )
        s.add(server)
        await s.commit()
    body = (await reader.get("/api/v1/topology")).json()
    nodes = by_name(body)
    assert nodes["topo-srv-01"]["server_id"] == str(server.id)
    assert nodes["topo-core-01"]["server_id"] is None
    assert body["truncated"] is False and body["skipped_cables"] == 0


async def test_signed_out_gets_nothing(client: httpx.AsyncClient, netbox_mock: respx.MockRouter) -> None:
    assert (await client.get("/api/v1/topology")).status_code == 401
