"""Reading switches over SNMP: the protocol (v2c, v3 with authentication and privacy, failures) against
an SNMP agent in this process (tests/snmp_agent.py), and what Lanvex makes of standard and Cisco MIBs.
"""

from collections.abc import AsyncIterator

import pytest

from lanvex.integrations.snmp.client import SnmpCredentials, SnmpError, connect
from lanvex.integrations.snmp.switch import portlist, read_switch

from .snmp_agent import AUTH_KEY, COMMUNITY, PRIV_KEY, USER, Agent
from .switch_tables import ACCESS_MAC, CORE_MAC, A, B, C, D, E, F, access_switch, cisco_switch

V2 = SnmpCredentials(version="2c", community=COMMUNITY)
V3 = SnmpCredentials(
    version="3",
    username=USER,
    auth_protocol="sha",
    auth_password=AUTH_KEY,
    priv_protocol="aes",
    priv_password=PRIV_KEY,
)


@pytest.fixture
async def access() -> AsyncIterator[Agent]:
    agent = Agent(access_switch())
    yield agent
    agent.stop()


async def read(agent: Agent, creds: SnmpCredentials = V2, **kw):  # type: ignore[no-untyped-def]
    async with connect("127.0.0.1", agent.port, creds, allowed=kw.pop("allowed", []), **kw) as s:
        return await read_switch(s)


def test_credentials_are_complete_and_never_shown() -> None:
    with pytest.raises(ValueError, match="community"):
        SnmpCredentials(version="2c")
    with pytest.raises(ValueError, match="8 characters"):
        SnmpCredentials(version="3", username="u", auth_protocol="sha", auth_password="short")
    with pytest.raises(ValueError, match="Privacy needs authentication"):
        SnmpCredentials(version="3", username="u", priv_protocol="aes", priv_password="long-enough")
    assert V3.summary() == {"version": "3", "username": USER, "auth_protocol": "sha", "priv_protocol": "aes"}
    assert V2.summary() == {"version": "2c"}
    assert portlist(b"\x80\x01") == {1, 16}


async def test_reads_a_switch(access: Agent) -> None:
    f = await read(access)
    assert f.system.name == "sw-access" and f.system.description == "Example switch 8P, firmware 1.2"
    assert f.system.uptime_seconds == 3600 and f.system.bridge_mac == ACCESS_MAC
    assert f.system.lldp_chassis_id == ACCESS_MAC and f.system.lldp_system_name == "sw-access.example.net"

    ports = {p.name: p for p in f.ports}
    assert set(ports) == {*(f"Gi1/0/{i}" for i in range(1, 9)), "Po1", "Vlan10"}
    gi1 = ports["Gi1/0/1"]
    assert (gi1.kind, gi1.alias, gi1.speed_mbps, gi1.oper_up, gi1.bridge_port) == (
        "ethernet",
        "desk 1",
        1000,
        True,
        1,
    )
    assert (gi1.pvid, gi1.untagged, gi1.tagged) == (10, [10], [])
    assert ports["Gi1/0/4"].oper_up is False
    po = ports["Po1"]
    assert (po.kind, po.pvid, po.untagged, sorted(po.tagged)) == ("lag", 1, [1], [10, 20])
    assert ports["Gi1/0/7"].lag == po.index and ports["Gi1/0/1"].lag is None
    assert ports["Vlan10"].kind == "vlan"
    assert [(v.id, v.name) for v in f.vlans] == [(1, "default"), (10, "office"), (20, "voice")]

    fdb = {(e.mac, e.vlan, e.port) for e in f.fdb}
    assert fdb == {
        (A, 10, 1),
        (B, 10, 2),
        (D, 10, 3),
        (E, 20, 5),
        (F, 20, 5),
        (C, 10, 9),
        (CORE_MAC, 1, 9),
    }  # not the switch's own entry

    core, web = sorted(f.neighbors, key=lambda n: n.port or 0, reverse=True)
    assert (core.port_name, core.system_name, core.port_id, core.chassis_id) == (
        "Po1",
        "sw-core",
        "Gi1/0/24",
        CORE_MAC,
    )
    assert core.capabilities == ["Bridge", "Router"]
    assert (web.port_name, web.system_name, web.port_id, web.port_description) == (
        "Gi1/0/3",
        "web-01",
        D,
        "eth0",
    )
    assert web.capabilities == ["Station"] and web.management_addresses == ["192.0.2.10"]
    assert f.notes == []


async def test_snmpv3_with_authentication_and_privacy(access: Agent) -> None:
    f = await read(access, V3)
    assert f.system.name == "sw-access" and len(f.fdb) == 7


@pytest.mark.parametrize(
    ("creds", "status", "text"),
    [
        (V3.model_copy(update={"auth_password": "wrong-password"}), "auth_failed", "authentication password"),
        (V3.model_copy(update={"username": "nobody"}), "auth_failed", "does not know this SNMPv3 user"),
        (SnmpCredentials(version="2c", community="wrong"), "unreachable", "community"),
    ],
)
async def test_sign_in_failures(access: Agent, creds: SnmpCredentials, status: str, text: str) -> None:
    with pytest.raises(SnmpError) as e:
        await read(access, creds, timeout=0.3, retries=0)
    assert e.value.status == status and text in e.value.message


async def test_allowed_networks_apply_to_switches(access: Agent) -> None:
    with pytest.raises(SnmpError) as e:
        await read(access, allowed=["10.0.0.0/8"])
    assert e.value.status == "not_allowed"


@pytest.mark.parametrize("creds", [V2, V3])
async def test_cisco_vlans_and_per_vlan_address_tables(creds: SnmpCredentials) -> None:
    table, vlans = cisco_switch()
    agent = Agent(table, vlans)
    try:
        f = await read(agent, creds)
    finally:
        agent.stop()
    assert [(v.id, v.name) for v in f.vlans] == [(1, "default"), (10, "office")]  # not 1002-1005
    ports = {p.name: p for p in f.ports}
    assert (ports["Gi0/1"].pvid, ports["Gi0/1"].untagged, ports["Gi0/1"].trunk) == (10, [10], False)
    assert (ports["Gi0/2"].trunk, ports["Gi0/2"].untagged, ports["Gi0/2"].tagged) == (True, [1], [10])
    assert {(e.mac, e.vlan, e.port) for e in f.fdb} == {("02:00:00:00:00:99", 1, 2), (A, 10, 1)}
    assert "No VLAN tables (Q-BRIDGE-MIB): VLANs were not read." not in f.notes


async def test_a_switch_without_bridge_or_lldp_mibs() -> None:
    agent = Agent({"1.3.6.1.2.1.1.5.0": "router-1", "1.3.6.1.2.1.31.1.1.1.1.1": "ge-0/0/0"})
    try:
        f = await read(agent)
    finally:
        agent.stop()
    assert f.system.name == "router-1" and [p.name for p in f.ports] == ["ge-0/0/0"]
    assert f.fdb == [] and f.neighbors == [] and len(f.notes) == 3


@pytest.mark.parametrize("creds", [V2, V3])
async def test_cisco_without_per_vlan_tables(creds: SnmpCredentials) -> None:
    """Per-VLAN communities or contexts not set up: the read goes on without them, and says so."""
    table, _ = cisco_switch()
    table |= {f"1.3.6.1.4.1.9.9.46.1.3.1.1.2.1.{v}": 1 for v in range(20, 30)}  # many VLANs
    agent = Agent(table)
    try:
        f = await read(agent, creds, timeout=0.3, retries=0)
    finally:
        agent.stop()
    assert len(f.vlans) == 12 and f.fdb == []
    assert any("per-VLAN MAC address tables do not answer" in n for n in f.notes)


def test_vendors() -> None:
    from lanvex.core.vendors import RANDOMIZED, tidy, vendor

    assert vendor("FC:EC:DA:00:00:01") == "Ubiquiti" and vendor("3c-22-fb-00-00-00") == "Apple"
    assert vendor("02:00:00:00:00:0a") == RANDOMIZED  # phones choosing a random address
    assert vendor("52:54:00:12:34:56") == "QEMU/KVM virtual machine"
    assert vendor("01:00:5e:00:00:01") is None and vendor("") is None and vendor("not a mac") is None
    assert tidy("Shenzhen Example Co.,Ltd") == "Shenzhen Example" and tidy("Apple, Inc.") == "Apple"
