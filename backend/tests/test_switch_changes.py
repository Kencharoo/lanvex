"""Changing a switch port's VLANs: prepared and previewed without writing, approved by typing the port,
written over SNMP, read back, and undone when the switch refuses a write or does not take it.

The switch is an SNMP agent in this process (tests/snmp_agent.py) that takes SET requests into its
tables, so what Lanvex wrote can be read from them.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.integrations.snmp.switch import portlist
from lanvex.switches.changes import SwitchChangeRunner

from .snmp_agent import COMMUNITY, Agent
from .switch_tables import B, access_switch, raw
from .test_switches_api import NetBox

V2 = {"version": "2c", "community": COMMUNITY}
EGRESS, UNTAGGED, PVID = (
    "1.3.6.1.2.1.17.7.1.4.3.1.2",
    "1.3.6.1.2.1.17.7.1.4.3.1.4",
    "1.3.6.1.2.1.17.7.1.4.5.1.1",
)


@pytest.fixture
def changes_on(monkeypatch: pytest.MonkeyPatch, netbox_mock: respx.MockRouter) -> None:
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    netbox_mock.route().mock(side_effect=NetBox().handle)


@pytest.fixture
async def switch() -> AsyncIterator[Agent]:
    agent = Agent(access_switch())
    yield agent
    agent.stop()


async def add(admin: httpx.AsyncClient, agent: Agent, allow: bool = True) -> str:
    r = await admin.post(
        "/api/v1/switches",
        json={"name": "sw-access", "address": "127.0.0.1", "port": agent.port, "credentials": V2},
    )
    sid = r.json()["id"]
    assert (await admin.post(f"/api/v1/switches/{sid}/read")).json()["last_status"] == "ok"
    if allow:
        await admin.patch(f"/api/v1/switches/{sid}", json={"changes_enabled": True})
    return sid


def members(agent: Agent, table: str, vlan: int) -> set[int]:
    return portlist(bytes(agent.table[f"{table}.{vlan}"].asOctets()))


async def run(database: Database) -> None:
    assert await SwitchChangeRunner(database, get_settings(), lambda: None).run_once()


async def prepare(
    admin: httpx.AsyncClient, sid: str, port: int, untagged: int, tagged: list[int]
) -> httpx.Response:
    return await admin.post(
        f"/api/v1/switches/{sid}/ports/{port}/vlans", json={"untagged": untagged, "tagged": tagged}
    )


async def approve(admin: httpx.AsyncClient, cid: str, port: str) -> dict[str, Any]:
    r = await admin.post(f"/api/v1/switch-changes/{cid}/approve", json={"confirm_name": port})
    assert r.status_code == 200, r.text
    return r.json()


async def test_changing_a_ports_vlans(
    admin: httpx.AsyncClient, switch: Agent, database: Database, changes_on: None
) -> None:
    sid = await add(admin, switch, allow=False)
    r = await prepare(admin, sid, 2, 20, [30])
    assert r.status_code == 409 and "not allowed for this switch" in r.json()["error"]["message"]
    await admin.patch(f"/api/v1/switches/{sid}", json={"changes_enabled": True})

    r = await prepare(admin, sid, 2, 20, [30])
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["summary"] == "Gi1/0/2: untagged 10 → untagged 20, tagged 30"
    assert [w["text"] for w in c["writes"]] == [
        "VLAN 20: Gi1/0/2 untagged",
        "VLAN 30: Gi1/0/2 tagged",
        "Gi1/0/2: PVID 10 → 20",
        "VLAN 10: Gi1/0/2 removed",
    ]
    assert c["writes"][0]["oids"] == [f"{EGRESS}.20", f"{UNTAGGED}.20"]  # members before untagged members
    assert c["writes"][3]["oids"] == [f"{UNTAGGED}.10", f"{EGRESS}.10"]  # and the other way round to leave
    assert "1 device on Gi1/0/2 loses its connection until it has an address in VLAN 20." in c["warnings"]
    assert c["status"] == "pending_approval" and switch.table.writes == []  # a preview writes nothing

    assert (await prepare(admin, sid, 3, 20, [])).status_code == 409  # one open change per switch
    bad = await admin.post(f"/api/v1/switch-changes/{c['id']}/approve", json={"confirm_name": "Gi1/0/3"})
    assert bad.status_code == 422
    assert (await approve(admin, c["id"], "Gi1/0/2"))["status"] == "approved"

    await run(database)
    done = (await admin.get(f"/api/v1/switch-changes/{c['id']}")).json()
    assert done["status"] == "succeeded", done["log"]
    assert done["outcome"] == "Gi1/0/2 is untagged 20, tagged 30, read back from the switch."
    assert 2 in members(switch, EGRESS, 20) and 2 in members(switch, UNTAGGED, 20)
    assert 2 in members(switch, EGRESS, 30) and 2 not in members(switch, UNTAGGED, 30)
    assert 2 not in members(switch, EGRESS, 10) and 2 not in members(switch, UNTAGGED, 10)
    assert {1, 3, 4, 9} <= members(switch, EGRESS, 10)  # the other ports are as they were
    assert int(switch.table[f"{PVID}.2"]) == 20
    ports = (await admin.get(f"/api/v1/switches/{sid}")).json()["facts"]["ports"]
    gi2 = next(p for p in ports if p["name"] == "Gi1/0/2")
    assert (gi2["untagged"], gi2["tagged"], gi2["pvid"]) == ([20], [30], 20)  # read again afterwards

    listed = (await admin.get(f"/api/v1/switches/{sid}/changes")).json()
    assert listed["open"] is None and [x["status"] for x in listed["recent"]] == ["succeeded"]
    assert (await prepare(admin, sid, 2, 20, [30])).json()["error"][
        "message"
    ] == "Gi1/0/2 already has these VLANs."
    events = (await admin.get("/api/v1/audit-events", params={"action": "switch.change"})).json()["results"]
    assert {e["action"] for e in events} == {"switch.change.prepare", "switch.change.approve"}


async def test_what_is_refused(
    admin: httpx.AsyncClient,
    client: httpx.AsyncClient,
    switch: Agent,
    changes_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await add(admin, switch)
    message = lambda r: r.json()["error"]["message"]  # noqa: E731
    assert "part of Po1" in message(await prepare(admin, sid, 7, 20, []))
    assert "Only physical ports" in message(await prepare(admin, sid, 100, 20, []))
    assert (
        message(await prepare(admin, sid, 2, 99, []))
        == "VLAN 99 does not exist on the switch. Create it there first."
    )
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    assert (await prepare(admin, sid, 2, 20, [])).status_code == 403


async def test_the_port_lanvex_is_reached_through(admin: httpx.AsyncClient, changes_on: None) -> None:
    # The switch knows Lanvex's address (127.0.0.1 here) with B's MAC address, learned on Gi1/0/2.
    agent = Agent(
        access_switch()
        | {"1.3.6.1.2.1.4.22.1.2.100.127.0.0.1": raw(B), "1.3.6.1.2.1.4.22.1.4.100.127.0.0.1": 3}
    )
    try:
        sid = await add(admin, agent)
        r = await prepare(admin, sid, 2, 20, [])
        assert r.json()["error"]["message"] == (
            "The switch reaches Lanvex through Gi1/0/2: changing it could cut Lanvex off the switch."
        )
        ok = (await prepare(admin, sid, 3, 20, [])).json()
        assert not any("could not tell which port" in w for w in ok["warnings"])
    finally:
        agent.stop()


async def test_a_refused_write_is_undone(
    admin: httpx.AsyncClient, switch: Agent, database: Database, changes_on: None
) -> None:
    sid = await add(admin, switch)
    before = {k: bytes(v.asOctets()) for k, v in switch.table.table.items() if hasattr(v, "asOctets")}
    switch.table.refuse = {f"{PVID}.2"}
    c = (await prepare(admin, sid, 2, 20, [30])).json()
    await approve(admin, c["id"], "Gi1/0/2")
    await run(database)
    done = (await admin.get(f"/api/v1/switch-changes/{c['id']}")).json()
    assert done["status"] == "rolled_back", (done["outcome"], done["log"])
    assert done["outcome"].startswith("The switch refused the change") and "were undone" in done["outcome"]
    after = {k: bytes(v.asOctets()) for k, v in switch.table.table.items() if hasattr(v, "asOctets")}
    assert after == before  # VLANs 20 and 30 as they were
    alerts = (await admin.get("/api/v1/alerts")).json()["alerts"]
    assert [(a["kind"], a["switch_id"]) for a in alerts] == [("change_failed", sid)]


async def test_a_change_the_switch_does_not_take_is_undone(
    admin: httpx.AsyncClient, switch: Agent, database: Database, changes_on: None
) -> None:
    sid = await add(admin, switch)
    switch.table.ignore = {f"{UNTAGGED}.20"}  # says yes, keeps the port tagged
    c = (await prepare(admin, sid, 2, 20, [])).json()
    await approve(admin, c["id"], "Gi1/0/2")
    await run(database)
    done = (await admin.get(f"/api/v1/switch-changes/{c['id']}")).json()
    assert done["status"] == "rolled_back" and "did not take the change as planned" in done["outcome"]
    assert 2 in members(switch, EGRESS, 10) and 2 in members(switch, UNTAGGED, 10)
    assert 2 not in members(switch, EGRESS, 20) and int(switch.table[f"{PVID}.2"]) == 10


async def test_a_change_made_on_the_switch_since_the_preview(
    admin: httpx.AsyncClient, switch: Agent, database: Database, changes_on: None
) -> None:
    sid = await add(admin, switch)
    c = (await prepare(admin, sid, 2, 20, [])).json()
    switch.table.table[tuple(int(x) for x in f"{EGRESS}.20".split("."))] = switch.table[
        f"{EGRESS}.10"
    ]  # someone else
    await approve(admin, c["id"], "Gi1/0/2")
    writes = len(switch.table.writes)
    await run(database)
    done = (await admin.get(f"/api/v1/switch-changes/{c['id']}")).json()
    assert done["status"] == "failed" and "changed since the preview" in done["outcome"]
    assert len(switch.table.writes) == writes


async def test_a_switch_that_takes_no_writes(
    admin: httpx.AsyncClient, database: Database, changes_on: None
) -> None:
    agent = Agent(access_switch(), writable=False)
    try:
        sid = await add(admin, agent)
        c = (await prepare(admin, sid, 2, 20, [])).json()
        await approve(admin, c["id"], "Gi1/0/2")
        await run(database)
        done = (await admin.get(f"/api/v1/switch-changes/{c['id']}")).json()
        assert done["status"] == "failed" and done["outcome"].endswith("Nothing was changed.")
    finally:
        agent.stop()


async def test_readers_and_cancelling(
    admin: httpx.AsyncClient,
    client: httpx.AsyncClient,
    users: dict[str, str],
    switch: Agent,
    changes_on: None,
) -> None:
    from .conftest import login

    sid = await add(admin, switch)
    c = (await prepare(admin, sid, 2, 20, [])).json()
    await login(client, "reader", users["reader"])
    assert (await client.get(f"/api/v1/switch-changes/{c['id']}")).status_code == 200
    assert (
        await client.post(f"/api/v1/switch-changes/{c['id']}/approve", json={"confirm_name": "Gi1/0/2"})
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/switches/{sid}/ports/3/vlans", json={"untagged": 20})
    ).status_code == 403
    await login(client, "admin", users["admin"])  # the same client
    r = await admin.post(f"/api/v1/switch-changes/{c['id']}/cancel")
    assert r.json()["status"] == "cancelled" and switch.table.writes == []
