"""Scheduled reads and server history.

Facts come from real output captured on the lab's netplan host (tests/fixtures/linux/netplan), changed
the way a server changes. The SSH read itself is replaced by a stand-in that returns those facts.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.integrations.linux import adapter, transport
from lanvex.integrations.linux.adapter import Collection
from lanvex.integrations.linux.assemble import assemble
from lanvex.integrations.linux.facts import Address, HostFacts, Route
from lanvex.servers.history import changes, fingerprint
from lanvex.servers.models import Server, ServerSnapshot
from lanvex.servers.scheduler import ReadScheduler

from .conftest import login
from .test_linux_parsing import load
from .test_servers_api import body


def real_facts() -> HostFacts:
    return assemble(load("netplan"))


class FakeServer:
    """Answers reads with whatever state the test sets, or fails like an unreachable host."""

    def __init__(self) -> None:
        self.facts = real_facts()
        self.down = False
        self.error: transport.SSHError | None = None  # fail every read this way
        self.reads = 0

    async def collect(self, target: Any, connect_timeout: float, command_timeout: float) -> Collection:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.down:
            raise transport.SSHError("Connection refused.", code="unreachable")
        facts = self.facts.model_copy(update={"collected_at": datetime.now(UTC)})
        return Collection(facts=facts, raw={}, duration_ms=12)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    server = FakeServer()
    monkeypatch.setattr(adapter, "collect", server.collect)
    monkeypatch.setattr(
        "lanvex.servers.service.ServerService._check_target", lambda self, address: _no_check()
    )
    return server


async def _no_check() -> list[str]:
    return []


def iface(facts: HostFacts, name: str):  # type: ignore[no-untyped-def]
    return next(i for i in facts.interfaces if i.name == name)


# ---- what counts as a change --------------------------------------------------------------------


def test_reads_of_the_same_state_have_the_same_fingerprint() -> None:
    a, b = real_facts(), real_facts()
    b = b.model_copy(update={"collected_at": datetime.now(UTC) + timedelta(hours=1), "warnings": ["slow"]})
    first = b.interfaces[0]
    b.interfaces[0] = first.model_copy(update={"index": first.index + 40})
    assert fingerprint(a.model_dump(mode="json")) == fingerprint(b.model_dump(mode="json"))


def test_changes_are_described_in_words() -> None:
    old, new = real_facts(), real_facts()
    main = next(i for i in new.interfaces if i.addresses and i.name != "lo")
    main.addresses.append(Address(address="172.30.0.50/24", family=4))
    main.mtu = 9000
    new.interfaces = [i for i in new.interfaces if i.name != "br0"]
    new.routes = [r for r in new.routes if r.destination != "0.0.0.0/0"] + [
        Route(destination="0.0.0.0/0", family=4, gateway="172.30.0.254", interface=main.name)
    ]
    new.routes.append(Route(destination="10.20.0.0/16", family=4, gateway="172.30.0.2", interface=main.name))
    new.dns.effective_servers = ["9.9.9.9"]
    texts = [c.text for c in changes(old, new)]
    assert f"{main.name}: address 172.30.0.50/24 added" in texts
    assert f"{main.name}: MTU {iface(old, main.name).mtu} → 9000" in texts
    assert "Interface br0 disappeared" in texts
    assert "Default gateway 172.30.0.1 → 172.30.0.254" in texts
    assert f"Route 10.20.0.0/16 via 172.30.0.2 on {main.name} added" in texts
    assert any(t.startswith("DNS servers ") and t.endswith("→ 9.9.9.9") for t in texts)
    assert changes(old, real_facts()) == []


def test_link_and_switch_state() -> None:
    old, new = real_facts(), real_facts()
    eth = next(i for i in new.interfaces if i.name != "lo")
    eth.carrier = not eth.carrier
    assert [c.text for c in changes(old, new)] == [
        f"{eth.name} {'has a link again' if eth.carrier else 'lost its link'}"
    ]


def test_neighbour_tables_are_not_changes_but_lldp_neighbours_are() -> None:
    old = real_facts()
    busy = old.model_copy(update={"neighbors": [*old.neighbors[:1]]})
    assert fingerprint(busy.model_dump(mode="json")) == fingerprint(old.model_dump(mode="json"))
    # Facts read before Lanvex read LLDP have no such key; a server without neighbours keeps its fingerprint.
    before = old.model_copy(update={"lldp": []}).model_dump(mode="json")
    before.pop("lldp")
    before.pop("neighbors")
    assert fingerprint(before) == fingerprint(old.model_copy(update={"lldp": []}).model_dump(mode="json"))

    gone = old.model_copy(update={"lldp": [n for n in old.lldp if n.system_name != "lab-nm"]})
    assert fingerprint(gone.model_dump(mode="json")) != fingerprint(old.model_dump(mode="json"))
    assert [c.text for c in changes(old, gone)] == ["eth0: lab-nm (port eth0) no longer seen over LLDP"]
    assert [c.text for c in changes(gone, old)] == ["eth0: lab-nm (port eth0) seen over LLDP"]


# ---- reads and history --------------------------------------------------------------------------


async def _server(admin: httpx.AsyncClient) -> str:
    r = await admin.post("/api/v1/servers", json=body(address="192.0.2.10"))
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"]


async def test_reads_of_an_unchanged_server_confirm_instead_of_piling_up(
    admin: httpx.AsyncClient, fake: FakeServer
) -> None:
    sid = await _server(admin)
    first = (await admin.post(f"/api/v1/servers/{sid}/collect")).json()
    fake.facts.neighbors = fake.facts.neighbors[:1]  # comes and goes without being a new state
    again = (await admin.post(f"/api/v1/servers/{sid}/collect")).json()
    assert again["id"] == first["id"] and again["reads"] == 2 and again["confirmed_at"]
    assert len((await admin.get(f"/api/v1/servers/{sid}/snapshots")).json()) == 1
    # Pages built on the latest read (leases, neighbours, "read at") show that read, not the first one.
    latest = (await admin.get(f"/api/v1/servers/{sid}/snapshots/latest")).json()
    assert latest["facts"]["collected_at"] > first["facts"]["collected_at"]
    assert len(latest["facts"]["neighbors"]) == len(fake.facts.neighbors)

    eth = next(i for i in fake.facts.interfaces if i.addresses and i.name != "lo")
    eth.addresses.append(Address(address="172.30.0.77/24", family=4))
    changed = (await admin.post(f"/api/v1/servers/{sid}/collect")).json()
    assert changed["id"] != first["id"] and changed["reads"] == 1

    fake.down = True
    await admin.post(f"/api/v1/servers/{sid}/collect")
    await admin.post(f"/api/v1/servers/{sid}/collect")  # the same failure again: confirmed, not stored
    fake.down = False
    eth.addresses = [a for a in eth.addresses if a.address != "172.30.0.77/24"]
    await admin.post(f"/api/v1/servers/{sid}/collect")

    history = (await admin.get(f"/api/v1/servers/{sid}/history")).json()
    assert [h["status"] for h in history] == ["ok", "unreachable", "ok", "ok"]  # newest first
    back, failed, added, first_state = history
    assert [c["text"] for c in back["changes"]] == [f"{eth.name}: address 172.30.0.77/24 removed"]
    assert failed["reads"] == 2 and failed["message"] == "Connection refused."
    assert [c["text"] for c in added["changes"]] == [f"{eth.name}: address 172.30.0.77/24 added"]
    assert first_state["first"] is True and first_state["reads"] == 2


async def test_history_is_open_to_readers(
    admin: httpx.AsyncClient, users: dict[str, str], fake: FakeServer
) -> None:
    sid = await _server(admin)
    await admin.post(f"/api/v1/servers/{sid}/collect")
    await login(admin, "reader", users["reader"])  # the same client, now signed in as a reader
    r = await admin.get(f"/api/v1/servers/{sid}/history")
    assert r.status_code == 200 and len(r.json()) == 1


# ---- the schedule -------------------------------------------------------------------------------


async def test_interval_per_server(admin: httpx.AsyncClient) -> None:
    sid = await _server(admin)
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert (
        s["read_interval_minutes"] is None
        and s["read_interval"] == get_settings().server_read_interval_minutes
    )
    assert s["next_read_at"]
    assert (await admin.patch(f"/api/v1/servers/{sid}", json={"read_interval_minutes": 3})).status_code == 422
    s = (await admin.patch(f"/api/v1/servers/{sid}", json={"read_interval_minutes": 15})).json()
    due = datetime.fromisoformat(s["next_read_at"])
    assert s["read_interval"] == 15 and timedelta(minutes=14) < due - datetime.now(UTC) <= timedelta(
        minutes=15
    )
    s = (await admin.patch(f"/api/v1/servers/{sid}", json={"read_interval_minutes": 0})).json()
    assert s["read_interval"] == 0 and s["next_read_at"] is None
    s = (await admin.patch(f"/api/v1/servers/{sid}", json={"read_interval_minutes": None})).json()
    assert (
        s["read_interval_minutes"] is None
        and s["read_interval"] == get_settings().server_read_interval_minutes
    )


async def test_scheduler_reads_due_servers_once_even_with_two_processes(
    admin: httpx.AsyncClient, database: Database, fake: FakeServer
) -> None:
    due, off = (
        await _server(admin),
        (await admin.post("/api/v1/servers", json=body(name="web-02", address="192.0.2.11"))).json()[
            "server"
        ]["id"],
    )
    await admin.patch(f"/api/v1/servers/{off}", json={"read_interval_minutes": 0})
    later = datetime.now(UTC) + timedelta(hours=2)  # past the default interval of both
    one, two = (ReadScheduler(database, get_settings(), lambda: None) for _ in range(2))
    claimed = [await one.claim(later), await two.claim(later)]
    assert claimed == [[uuid.UUID(due)], []]  # the second process finds nothing left to do

    assert await one.run_due(later + timedelta(hours=2)) == 1
    async with database.sessionmaker() as s:
        snap = await s.scalar(select(ServerSnapshot).where(ServerSnapshot.server_id == uuid.UUID(due)))
        assert snap is not None and snap.requested_by == "(schedule)" and snap.status.value == "ok"
        assert (
            await s.scalar(
                select(func.count())
                .select_from(ServerSnapshot)
                .where(ServerSnapshot.server_id == uuid.UUID(off))
            )
            == 0
        )
        server = await s.get(Server, uuid.UUID(due))
        assert server is not None and server.last_collected_at is not None


async def test_schedule_switched_off_for_the_installation(
    admin: httpx.AsyncClient, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _server(admin)
    monkeypatch.setattr(get_settings(), "server_read_interval_minutes", 0)
    scheduler = ReadScheduler(database, get_settings(), lambda: None)
    assert await scheduler.claim(datetime.now(UTC) + timedelta(days=30)) == []
