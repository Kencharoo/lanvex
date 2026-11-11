"""Linux adapter parsing.

Most tests use REAL output captured from the lab hosts (tests/fixtures/linux). Tests marked
"constructed" use hand-written samples in iproute2's documented JSON shape for cases the lab kernel
cannot produce (no IPv6, VLAN, or bond support); they are not proof of compatibility on their own.
"""

import json
from pathlib import Path

import pytest

from lanvex.integrations.linux import parsers as P
from lanvex.integrations.linux.assemble import CommandResult, Results, assemble
from lanvex.integrations.linux.commands import COMMANDS, Cmd

FIXTURES = Path(__file__).parent / "fixtures" / "linux"


def load(host: str) -> Results:
    meta = json.loads((FIXTURES / host / "_meta.json").read_text())
    out: Results = {}
    for cmd in Cmd:
        m = meta[cmd.value]
        out[cmd] = CommandResult(
            stdout=(FIXTURES / host / f"{cmd.value}.stdout").read_text(),
            stderr=m["stderr"],
            exit_status=m["exit_status"],
        )
    return out


def test_commands_are_constant_read_only_strings() -> None:
    assert set(COMMANDS) == set(Cmd)
    forbidden = (
        ">",
        "rm ",
        "tee",
        "sudo",
        "ip link set",
        "ip addr add",
        "ip route add",
        "nmcli con",
        "systemctl restart",
        "systemctl stop",
        "netplan apply",
        "ifup",
        "ifdown",
        "$(",
        "`",
    )
    for line in COMMANDS.values():
        body = line.replace("2>/dev/null", "")
        assert not any(f in body for f in forbidden), line


# ---- real captures ------------------------------------------------------------------------


def test_netplan_networkd_host() -> None:
    f = assemble(load("netplan"))
    assert f.system.hostname == "lab-netplan"
    assert f.system.os.id == "ubuntu" and f.system.os.version_id == "24.04" and f.system.os.supported
    assert f.system.iproute2_version == "6.1.0"
    assert f.stack.key == "netplan-networkd" and f.stack.netplan_files == ["50-lab.yaml"]
    by = {i.name: i for i in f.interfaces}
    assert set(by) == {"lo", "eth0", "br0"}
    assert by["eth0"].managed_by == "systemd-networkd" and by["eth0"].manager_state == "configured"
    assert [a.address for a in by["eth0"].addresses] == ["172.30.0.11/24"]
    assert by["br0"].kind == "bridge" and by["br0"].oper_state == "down" and not by["br0"].carrier
    assert by["lo"].kind == "loopback" and by["lo"].mac is None and by["lo"].managed_by is None
    defaults = [r for r in f.routes if r.is_default]
    assert {(r.gateway, r.interface) for r in defaults} == {("172.30.0.1", "eth0")}
    assert next(r for r in f.routes if r.destination == "10.11.0.0/24").flags == ["linkdown"]
    # resolved is active, but resolv.conf is not its stub here, so resolv.conf is what applications use.
    assert f.dns.resolved is not None
    eth0 = next(s for s in f.dns.resolved if s.scope == "eth0")
    assert eth0.servers == ["172.30.0.1", "9.9.9.9"] and eth0.domains == ["lab.example.net"]
    assert f.dns.uses_resolved_stub is False and f.dns.source == "/etc/resolv.conf"
    assert f.warnings == []


def test_networkmanager_host_reports_externally_connected_interface() -> None:
    f = assemble(load("nm"))
    assert f.stack.key == "networkmanager" and f.stack.units["NetworkManager"] == "active"
    by = {i.name: i for i in f.interfaces}
    assert by["eth0"].managed_by == "NetworkManager" and by["eth0"].manager_profile == "lab-uplink"
    assert by["eth0"].manager_state == "connected"
    # eth1 came up before NetworkManager saw it: NetworkManager tracks it but its own profile is not active.
    assert by["eth1"].manager_state == "connected (externally)" and by["eth1"].manager_profile == "eth1"
    assert [a.address for a in by["eth1"].addresses] == ["172.31.0.12/24"]
    assert f.dns.resolved is None  # systemd-resolved not installed
    assert not any(r.destination == "10.50.0.0/16" for r in f.routes)  # the inactive profile's route


def test_ifupdown_host() -> None:
    f = assemble(load("ifup"))
    assert f.stack.key == "ifupdown" and f.stack.ifupdown_interfaces == ["eth0", "br0"]
    by = {i.name: i for i in f.interfaces}
    assert by["eth0"].managed_by == "ifupdown" and by["br0"].managed_by == "ifupdown"
    assert next(r for r in f.routes if r.is_default).flags == ["onlink"]
    assert f.stack.netplan_files == []


def test_failed_commands_become_warnings_not_errors() -> None:
    results = load("netplan")
    results[Cmd.ADDRESSES] = CommandResult(
        stdout="", stderr="env: 'ip': No such file or directory\n", exit_status=127
    )
    results[Cmd.ROUTES_V4] = CommandResult(stdout="", stderr="timed out after 15s", exit_status=None)
    f = assemble(results)
    assert f.interfaces == [] and all(r.family == 6 for r in f.routes)
    assert any("No such file" in w for w in f.warnings) and any("did not complete" in w for w in f.warnings)


def test_systemctl_unavailable() -> None:
    results = load("ifup")
    results[Cmd.UNIT_STATES] = CommandResult(
        stdout="", stderr="System has not been booted with systemd", exit_status=1
    )
    f = assemble(results)
    assert set(f.stack.units.values()) == {"unknown"}
    assert f.stack.key == "ifupdown"  # still recognised from /etc/network/interfaces
    assert any("systemctl unavailable" in e for e in f.stack.evidence)


# ---- os-release classification --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,supported,fragment",
    [
        ('ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.4 LTS"', True, "Ubuntu Server 22.04"),
        ('ID=ubuntu\nVERSION_ID="18.04"', False, "older than the supported minimum (20.04)"),
        ('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"', True, "Debian 12"),
        ('ID=debian\nVERSION_ID="10"', False, "older than"),
        ("ID=fedora\nVERSION_ID=41\nVARIANT_ID=server", True, "Fedora Server 41"),
        ("ID=fedora\nVERSION_ID=41\nVARIANT_ID=workstation", True, "variant 'workstation'"),
        ('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="21.3"', False, "Derived from Ubuntu Server"),
        ('ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"', False, "Derived from Fedora Server"),
        ("ID=alpine\nVERSION_ID=3.20.0", False, "Not a supported distribution"),
        ("", False, "Not a supported distribution"),
    ],
)
def test_classify_os(text: str, supported: bool, fragment: str) -> None:
    info = P.classify_os(P.parse_os_release(text))
    assert info.supported is supported and fragment in info.support_note, info.support_note


# ---- constructed samples (not captured live) ------------------------------------------------


def test_constructed_vlan_bond_ipv6_dhcp() -> None:
    sample = [
        {
            "ifindex": 2,
            "ifname": "enp1s0",
            "flags": ["BROADCAST", "MULTICAST", "SLAVE", "UP", "LOWER_UP"],
            "mtu": 1500,
            "operstate": "UP",
            "link_type": "ether",
            "address": "52:54:00:12:34:56",
            "master": "bond0",
            "linkinfo": {"info_slave_kind": "bond"},
            "addr_info": [],
        },
        {
            "ifindex": 4,
            "ifname": "bond0",
            "flags": ["BROADCAST", "MULTICAST", "MASTER", "UP", "LOWER_UP"],
            "mtu": 1500,
            "operstate": "UP",
            "link_type": "ether",
            "address": "52:54:00:12:34:56",
            "linkinfo": {"info_kind": "bond", "info_data": {"mode": "802.3ad"}},
            "addr_info": [
                {
                    "family": "inet",
                    "local": "192.0.2.10",
                    "prefixlen": 24,
                    "scope": "global",
                    "dynamic": True,
                    "valid_life_time": 86000,
                    "preferred_life_time": 86000,
                },
                {
                    "family": "inet6",
                    "local": "2001:db8:1::10",
                    "prefixlen": 64,
                    "scope": "global",
                    "valid_life_time": 4294967295,
                    "preferred_life_time": 4294967295,
                },
                {
                    "family": "inet6",
                    "local": "fe80::5054:ff:fe12:3456",
                    "prefixlen": 64,
                    "scope": "link",
                    "valid_life_time": 4294967295,
                    "preferred_life_time": 4294967295,
                },
            ],
        },
        {
            "ifindex": 5,
            "ifname": "bond0.100",
            "link": "bond0",
            "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
            "mtu": 1500,
            "operstate": "UP",
            "link_type": "ether",
            "address": "52:54:00:12:34:56",
            "linkinfo": {
                "info_kind": "vlan",
                "info_data": {"protocol": "802.1Q", "id": 100, "flags": ["REORDER_HDR"]},
            },
            "addr_info": [
                {
                    "family": "inet",
                    "local": "198.51.100.10",
                    "prefixlen": 24,
                    "scope": "global",
                    "valid_life_time": 4294967295,
                    "preferred_life_time": 4294967295,
                }
            ],
        },
    ]
    ifs = {i.name: i for i in P.parse_addresses(json.dumps(sample))}
    assert ifs["enp1s0"].master == "bond0" and ifs["enp1s0"].kind == "ethernet"
    assert ifs["bond0"].kind == "bond"
    v4, v6, ll = ifs["bond0"].addresses
    assert v4.dynamic and v4.family == 4
    assert v6.family == 6 and not v6.dynamic and ll.scope == "link"
    assert (
        ifs["bond0.100"].kind == "vlan"
        and ifs["bond0.100"].vlan_id == 100
        and ifs["bond0.100"].parent == "bond0"
    )

    routes = P.parse_routes(
        json.dumps(
            [
                {
                    "dst": "default",
                    "gateway": "fe80::1",
                    "dev": "bond0",
                    "protocol": "ra",
                    "metric": 1024,
                    "flags": [],
                    "pref": "medium",
                },
                {"dst": "2001:db8:1::/64", "dev": "bond0", "protocol": "kernel", "metric": 256, "flags": []},
                {
                    "type": "unreachable",
                    "dst": "2001:db8:dead::/48",
                    "dev": "lo",
                    "metric": 1024,
                    "flags": [],
                },
            ]
        ),
        6,
    )
    assert routes[0].destination == "::/0" and routes[0].is_default and routes[0].gateway == "fe80::1"
    assert routes[2].type == "unreachable"


def test_constructed_nmcli_escaping_and_resolved_stub() -> None:
    nm = P.parse_nmcli_devices(
        "eth0:ethernet:connected:Wired connection 1\nwlp2s0:wifi:connected:Cafe\\: guest\n"
    )
    assert nm["wlp2s0"]["connection"] == "Cafe: guest"
    servers, search, options = P.parse_resolv_conf(
        "nameserver 127.0.0.53\noptions edns0 trust-ad\nsearch corp.example\n"
    )
    assert servers == ["127.0.0.53"] and search == ["corp.example"] and options == ["edns0", "trust-ad"]
    scopes = P.parse_resolvectl(
        "Global:\nLink 2 (ens3): 10.0.0.2 2001:db8::53\n", "Global:\nLink 2 (ens3): corp.example ~.\n"
    )
    assert scopes[1].servers == ["10.0.0.2", "2001:db8::53"] and scopes[1].domains == ["corp.example", "~."]


def test_resolved_stub_uses_per_link_servers() -> None:
    results = load("netplan")
    results[Cmd.RESOLV_CONF] = CommandResult(
        stdout="nameserver 127.0.0.53\noptions edns0\n", stderr="", exit_status=0
    )
    f = assemble(results)
    assert f.dns.uses_resolved_stub and f.dns.effective_servers == ["127.0.0.11", "172.30.0.1", "9.9.9.9"]
    assert f.dns.source.startswith("systemd-resolved")


def test_ifupdown_sources_and_comments() -> None:
    names, sources = P.parse_ifupdown(
        "# iface fake inet static\nsource /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\niface ens3 inet dhcp\niface ens3 inet6 auto\n"
    )
    assert names == ["ens3"] and sources
