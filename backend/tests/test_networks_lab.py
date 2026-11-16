"""VLAN interfaces, firewalls and DHCP on REAL servers of each kind Lanvex supports, with a real DHCP
client on another host (lab-nm):

- lab-fedora: NetworkManager, firewalld, dnsmasq installed but off (Fedora Server, RHEL).
- lab-netplan: netplan with systemd-networkd, systemd-resolved holding port 53, dnsmasq enabled but
  failing next to it, ufw inactive (Ubuntu Server).
- lab-debian: ifupdown sourcing interfaces.d, dnsmasq running on every interface (Debian 12).

Skipped unless LANVEX_TEST_LAB=1; each host is skipped where it does not run (lab-fedora and lab-debian
are built from their distributions' images, so only where a registry is reachable: CI). VLANs need a
kernel with 802.1Q.

These tests run the helper itself, as root, the way sudo runs it for Lanvex: plan, apply, verify,
confirm, roll back, and the server's own rollback timer. They change only lab containers.
"""

import json
import os
import subprocess
import time
import uuid
from typing import Any

import pytest

from lanvex.integrations.linux import helper

from .test_servers_lab import sh

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(os.environ.get("LANVEX_TEST_LAB") != "1", reason="LANVEX_TEST_LAB=1 not set"),
]

CLIENT = "lab-nm"  # Ubuntu with NetworkManager: a device on the new VLAN
# lab-ifup plays the rest of the world for access rules: a host on the main LAN (172.30.0.13), a stand-in
# for the Internet (198.51.100.10, a documentation address, routed to it by the server), and a device on
# VLAN 60 (192.168.60.20).
OTHER = "lab-ifup"
INTERNET = "198.51.100.10"
HELPER = "/usr/local/libexec/lanvex/lanvex-netapply"
BACKENDS = {"lab-fedora": "networkmanager", "lab-netplan": "netplan", "lab-debian": "ifupdown"}
VLAN_FILE = {
    "networkmanager": "/etc/NetworkManager/system-connections/lanvex-vlan50.nmconnection",
    "netplan": "/etc/netplan/60-lanvex.yaml",
    "ifupdown": "/etc/network/interfaces.d/lanvex",
}

# Put each host back the way it was built. What ran before the change matters: a rollback restores it.
RESET = {
    "lab-fedora": (
        "rm -f /etc/NetworkManager/system-connections/lanvex-* /etc/NetworkManager/system-connections/other-* "
        "/etc/firewalld/zones/lx-*; nmcli connection reload; firewall-cmd --reload >/dev/null; "
        "systemctl disable --now dnsmasq 2>/dev/null"
    ),
    "lab-netplan": (
        "rm -f /etc/netplan/60-lanvex.yaml /etc/netplan/70-other.yaml; for l in vlan50 vlan60; do "
        "ip link del $l 2>/dev/null; done; netplan apply; ufw --force reset >/dev/null 2>&1; "
        "rm -f /etc/ufw/*.rules.20*; systemctl enable dnsmasq 2>/dev/null; systemctl stop dnsmasq; "
        "systemctl reset-failed dnsmasq 2>/dev/null"
    ),
    "lab-debian": (
        "for l in vlan50 vlan60; do ifdown --force $l >/dev/null 2>&1; done; "
        "rm -f /etc/network/interfaces.d/lanvex /etc/network/interfaces.d/other-vlans; "
        "systemctl enable dnsmasq 2>/dev/null; systemctl restart dnsmasq"
    ),
}


def running(host: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", host], capture_output=True, text=True
    )
    return r.stdout.strip() == "true"


@pytest.fixture(scope="module", params=list(BACKENDS))
def host(request: pytest.FixtureRequest) -> str:
    name: str = request.param
    if not running(name):
        pytest.skip(f"{name} is not running (see deploy/lab/README.md)")
    # Piped in rather than copied: Fedora mounts a fresh tmpfs on /tmp, which hides what docker cp puts there.
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            name,
            "sh",
            "-c",
            f"mkdir -p {HELPER.rsplit('/', 1)[0]} && cat > {HELPER} && chmod 0755 {HELPER}",
        ],
        input=helper.HELPER_FILE.read_bytes(),
        check=True,
        capture_output=True,
    )
    return name


@pytest.fixture(autouse=True)
def clean(host: str) -> Any:
    reset(host)
    yield
    reset(host)


def reset(host: str) -> None:
    sh(
        host,
        "systemctl stop 'lanvex-rollback-*.timer' 2>/dev/null; rm -rf /var/lib/lanvex/changes; "
        f"{RESET[host]}; rm -f /etc/lanvex/networks.json /etc/dnsmasq.d/lanvex.conf /etc/dnsmasq.d/nv-vlans.conf; "
        "for l in vlan50 vlan60; do ip link del $l 2>/dev/null; done; "
        # Access rules: Lanvex's nftables table and its unit, firewalld policies, forwarding as built.
        "nft delete table inet lanvex 2>/dev/null; systemctl disable lanvex-firewall 2>/dev/null; "
        "systemctl disable --now 'lanvex-dns@*' 2>/dev/null; rm -rf /etc/lanvex/dns "
        "/etc/systemd/system/lanvex-dns@.service; ip link del lxdns 2>/dev/null; "
        "rm -f /etc/lanvex/firewall.nft /etc/systemd/system/lanvex-firewall.service /etc/sysctl.d/60-lanvex.conf "
        "/etc/firewalld/policies/lxf-*; systemctl daemon-reload; "
        "if command -v firewall-cmd >/dev/null; then firewall-cmd --reload >/dev/null; fi; "
        "ip route del 198.51.100.10/32 2>/dev/null; true",
    )
    sh(CLIENT, "nmcli connection delete client50 >/dev/null 2>&1; ip link del eth0.50 2>/dev/null; true")
    sh(OTHER, "ip addr del 198.51.100.10/32 dev eth0 2>/dev/null; ip link del eth0.60 2>/dev/null; true")


def call(
    host: str, command: str, payload: dict[str, Any] | None = None, check: bool = True
) -> dict[str, Any]:
    r = subprocess.run(
        ["docker", "exec", "-i", host, HELPER, command],
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = json.loads(r.stdout)
    if check:
        assert out["ok"], out
    return out


def client_mac() -> str:
    return sh(CLIENT, "cat /sys/class/net/eth0/address").strip()


def networks(pools: list[dict[str, Any]] | None, vlans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "networks",
        "vlans": vlans
        if vlans is not None
        else [{"name": "vlan50", "parent": "eth0", "id": 50, "addresses": ["192.168.50.1/24"]}],
        "dhcp": None if pools is None else {"pools": pools},
    }


def pool(**over: Any) -> dict[str, Any]:
    return {"interface": "vlan50", "start": "192.168.50.100", "end": "192.168.50.199", "lease": "2h"} | over


def apply(host: str, request: dict[str, Any], seconds: int = 120) -> tuple[str, dict[str, Any]]:
    plan = call(host, "plan", request)
    cid = uuid.uuid4().hex
    expect = {"backend": plan["backend"], "files": {f["path"]: f["sha256"] for f in plan["files"]}}
    result = call(host, "apply", request | {"id": cid, "rollback_seconds": seconds, "expect": expect})
    assert result["state"] == "applied"
    return cid, plan


def links(host: str) -> set[str]:
    return set(sh(host, "ls /sys/class/net").split())


def unit(host: str, what: str) -> str:
    return sh(host, f"systemctl {what} dnsmasq || true").strip()


def client_gets(address: str) -> None:
    """A device on VLAN 50 asks for an address by DHCP and gets `address`."""
    sh(
        CLIENT,
        "nmcli connection add type vlan con-name client50 ifname eth0.50 dev eth0 id 50 "
        "ipv4.method auto ipv6.method disabled >/dev/null && nmcli --wait 60 connection up client50 >/dev/null",
    )
    assert f"{address}/24" in sh(CLIENT, "ip -4 -o address show dev eth0.50")


OWN_TABLE = [
    "systemctl daemon-reload",
    "nft -f /etc/lanvex/firewall.nft",
    "systemctl enable lanvex-firewall.service",
]


def test_a_new_vlan_with_dhcp_serves_a_real_client(host: str) -> None:
    backend = BACKENDS[host]
    mac = client_mac()
    request = networks([pool(reservations=[{"mac": mac, "ip": "192.168.50.20", "name": "lab-nm"}])])
    plan = call(host, "plan", request)
    assert plan["backend"] == backend
    by_path = {f["path"]: f for f in plan["files"]}
    assert by_path[VLAN_FILE[backend]]["action"] == "create"
    assert json.loads(by_path["/etc/lanvex/networks.json"]["after"])["vlans"] == ["vlan50"]
    dnsmasq = by_path["/etc/dnsmasq.d/lanvex.conf"]["after"]
    assert f"dhcp-host={mac},set:lx-vlan50,192.168.50.20,lab-nm" in dnsmasq
    if backend == "networkmanager":
        assert by_path["/etc/firewalld/zones/lx-vlan50.xml"]["action"] == "create"
        assert plan["commands"] == [
            "firewall-cmd --check-config",
            "firewall-cmd --reload",
            "nmcli connection reload",
            "nmcli connection up id lanvex-vlan50",
            "systemctl enable dnsmasq",
            "systemctl restart dnsmasq",
        ]
    elif backend == "netplan":
        # systemd-resolved holds port 53: dnsmasq binds only the interfaces it serves.
        assert "\nbind-dynamic\n" in dnsmasq and "\ninterface=vlan50\n" in dnsmasq
        assert plan["commands"] == [
            "netplan generate",
            "netplan apply",
            *OWN_TABLE,
            "systemctl restart dnsmasq",
        ]
    else:
        # dnsmasq answers on every interface already: Lanvex adds its pool without restricting that.
        assert "interface=" not in dnsmasq and "bind-dynamic" not in dnsmasq
        assert any("every interface" in w for w in plan["warnings"])
        assert plan["commands"] == ["ifup vlan50", *OWN_TABLE, "systemctl restart dnsmasq"]
    assert "vlan50" not in links(host)  # planning changes nothing

    cid, _ = apply(host, request)
    result = call(host, "verify", {"id": cid})
    assert result["passed"], result["checks"]
    # Without firewalld, Lanvex's own nftables table limits what devices on its networks reach.
    expected = {"vlan:vlan50", "dhcp"} | ({"zone:vlan50"} if backend == "networkmanager" else {"nftables"})
    assert {c["id"] for c in result["checks"]} == expected
    if backend == "networkmanager":
        assert sh(host, "firewall-cmd --zone=lx-vlan50 --list-services").split() == ["dhcp", "dns"]
    if backend == "netplan":
        assert "192.168.50.1:53" in sh(host, "ss -H -l -n -u")  # DNS for the devices, next to resolved

    client_gets("192.168.50.20")
    assert call(host, "confirm", {"id": cid})["state"] == "confirmed"
    assert unit(host, "is-enabled") == "enabled"

    # Nothing left to do: the same request again is not a change.
    again = call(host, "plan", request, check=False)
    assert again["ok"] is False and again["error"]["code"] == "no_change"


def test_removing_a_vlan_and_rolling_it_back(host: str) -> None:
    cid, _ = apply(host, networks([pool()]))
    call(host, "confirm", {"id": cid})
    removal = networks([], vlans=[])
    plan = call(host, "plan", removal)
    assert {f["action"] for f in plan["files"]} == {"delete"}
    assert any("disconnects every device" in w for w in plan["warnings"])

    cid, _ = apply(host, removal)
    assert "vlan50" not in links(host)
    assert call(host, "verify", {"id": cid})["passed"]
    assert call(host, "rollback", {"id": cid})["state"] == "rolled_back"
    # The interface, its zone and DHCP are back exactly as they were.
    deadline = time.monotonic() + 30  # systemd-networkd creates it in the background
    while time.monotonic() < deadline:
        if "vlan50" in links(host) and "192.168.50.1/24" in sh(host, "ip -4 -o address show dev vlan50"):
            break
        time.sleep(1)
    assert "192.168.50.1/24" in sh(host, "ip -4 -o address show dev vlan50")
    assert unit(host, "is-enabled") == "enabled"
    if BACKENDS[host] == "networkmanager":
        assert sh(host, "firewall-cmd --get-zone-of-interface=vlan50").strip() == "lx-vlan50"
    assert unit(host, "is-active") == "active"
    assert sh(host, "cat /etc/lanvex/networks.json").count("vlan50") == 1


def test_the_servers_timer_restores_an_unconfirmed_change(host: str) -> None:
    before = (unit(host, "is-enabled"), unit(host, "is-active"))
    cid, _ = apply(host, networks([pool()]), seconds=30)
    assert "vlan50" in links(host)
    deadline = time.monotonic() + 90
    while call(host, "status", {"id": cid})["state"] != "rolled_back" and time.monotonic() < deadline:
        time.sleep(3)
    state = call(host, "status", {"id": cid})
    assert state["state"] == "rolled_back" and "No confirmation" in state["reason"]
    assert "vlan50" not in links(host)
    assert sh(host, "ls /etc/lanvex/ /etc/firewalld/zones/ 2>/dev/null || true").count("lx-") == 0
    assert "networks.json" not in sh(host, "ls /etc/lanvex/ 2>/dev/null || true")
    assert (unit(host, "is-enabled"), unit(host, "is-active")) == before  # dnsmasq as it was


OTHER_VLAN60 = {
    # Another tool's VLAN interface vlan60, in that tool's own configuration.
    "lab-fedora": (
        "nmcli connection add type vlan con-name other-vlan60 ifname vlan60 dev eth0 id 60 "
        "ipv4.method manual ipv4.addresses 192.168.60.1/24 >/dev/null",
        "'other-vlan60'",
    ),
    "lab-netplan": (
        "printf 'network:\\n  version: 2\\n  vlans:\\n    vlan60:\\n      id: 60\\n      link: eth0\\n' "
        "> /etc/netplan/70-other.yaml && chmod 600 /etc/netplan/70-other.yaml",
        "another netplan file",
    ),
    "lab-debian": (
        "printf 'iface vlan60 inet manual\\n' > /etc/network/interfaces.d/other-vlans",
        "/etc/network/interfaces.d/other-vlans",
    ),
}


def test_another_tools_vlan_and_dhcp_are_never_changed(host: str) -> None:
    # Like Netzverwaltung on KlassenServer: its own interface configuration and dnsmasq file.
    setup, named = OTHER_VLAN60[host]
    sh(host, setup)
    vlan60 = [{"name": "vlan60", "parent": "eth0", "id": 60, "addresses": ["192.168.60.1/24"]}]
    refused = call(host, "plan", networks(None, vlans=vlan60), check=False)
    assert refused["error"]["code"] == "conflict" and named in refused["error"]["message"], refused

    sh(host, "printf 'interface=vlan50\\n' > /etc/dnsmasq.d/nv-vlans.conf")
    refused = call(host, "plan", networks([pool()]), check=False)
    assert refused["error"]["code"] == "conflict" and "nv-vlans.conf" in refused["error"]["message"]


def test_ufw_lets_dhcp_through_and_takes_lanvexs_rules_back(host: str) -> None:
    if host != "lab-netplan":
        pytest.skip("ufw is tested on Ubuntu (lab-netplan)")
    # As an administrator turns it on: SSH allowed, everything else coming in denied.
    sh(host, "ufw allow 22/tcp >/dev/null && ufw --force enable >/dev/null")
    request = networks([pool(reservations=[{"mac": client_mac(), "ip": "192.168.50.21"}])])
    plan = call(host, "plan", request)
    assert "ufw allow in on vlan50 to any port 67 proto udp comment lanvex:vlan50" in plan["commands"]
    cid, _ = apply(host, request)
    result = call(host, "verify", {"id": cid})
    assert result["passed"] and "ufw:vlan50" in {c["id"] for c in result["checks"]}, result["checks"]
    client_gets("192.168.50.21")  # through ufw's default deny
    call(host, "confirm", {"id": cid})

    # Removing the network takes Lanvex's rules with it; a rollback brings them back.
    cid, plan = apply(host, networks([], vlans=[]))
    assert "ufw delete allow in on vlan50 to any port 67 proto udp" in plan["commands"]
    assert "lanvex:" not in sh(host, "ufw show added")
    assert "22/tcp" in sh(host, "ufw status")  # the administrator's own rules stay
    assert call(host, "rollback", {"id": cid})["state"] == "rolled_back"
    assert sh(host, "ufw show added").count("comment 'lanvex:vlan50'") == 3
    assert "67/udp on vlan50" in sh(host, "ufw status")


def reaches(host: str, address: str, port: int = 22) -> bool:
    """A TCP connection from `host` to address:port (every lab host has SSH listening)."""
    r = subprocess.run(
        ["docker", "exec", host, "timeout", "4", "bash", "-c", f"</dev/tcp/{address}/{port}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


VLAN60 = {"name": "vlan60", "parent": "eth0", "id": 60, "addresses": ["192.168.60.1/24"]}


def access_request(rules: list[dict[str, Any]], mac: str) -> dict[str, Any]:
    vlan50 = networks(None)["vlans"][0]
    return {
        "kind": "networks",
        "vlans": [vlan50, VLAN60],
        "dhcp": {"pools": [pool(reservations=[{"mac": mac, "ip": "192.168.50.20"}])]},
        "access": {"rules": rules},
    }


@pytest.mark.parametrize("ufw", [False, True], ids=["own-firewall", "ufw"])
def test_access_rules_let_devices_reach_exactly_what_is_allowed(host: str, ufw: bool) -> None:
    if ufw and host != "lab-netplan":
        pytest.skip("ufw is tested on Ubuntu (lab-netplan)")
    if ufw:
        sh(host, "ufw --force enable >/dev/null")  # denies everything coming in; docker exec still works
    mac = client_mac()

    # First: the Internet only. Private networks, the server's SSH and VLAN 60 stay closed.
    first = access_request([{"interface": "vlan50", "internet": True}], mac)
    plan = call(host, "plan", first)
    cid, _ = apply(host, first)
    result = call(host, "verify", {"id": cid})
    assert result["passed"], result["checks"]
    ids = {c["id"] for c in result["checks"]}
    assert "forwarding" in ids
    firewall = "policies" if BACKENDS[host] == "networkmanager" else ("ufw:vlan50" if ufw else "nftables")
    assert firewall in ids, (ids, plan["commands"])
    call(host, "confirm", {"id": cid})

    # The world beyond the server: the Internet stand-in behind the uplink (routed there only now, since
    # netplan apply drops routes systemd-networkd did not configure), a LAN host, a device on VLAN 60.
    sh(OTHER, f"ip addr add {INTERNET}/32 dev eth0")
    sh(host, f"ip route add {INTERNET}/32 via 172.30.0.13")
    client_gets("192.168.50.20")
    for net in (f"{INTERNET}/32", "172.30.0.13/32", "192.168.60.0/24"):
        sh(CLIENT, f"ip route add {net} via 192.168.50.1 dev eth0.50")  # through the server, not eth0
    sh(
        OTHER,
        "ip link add link eth0 name eth0.60 type vlan id 60 && ip addr add 192.168.60.20/24 dev eth0.60 "
        "&& ip link set eth0.60 up && ip route add 192.168.50.0/24 via 192.168.60.1",
    )
    assert reaches(CLIENT, INTERNET)  # the Internet, masqueraded
    assert not reaches(CLIENT, "172.30.0.13")  # a private network
    assert not reaches(CLIENT, "192.168.50.1")  # SSH on the server
    assert not reaches(CLIENT, "192.168.60.20")  # another Lanvex network
    assert not reaches(OTHER, "192.168.50.20")  # and from VLAN 60 to VLAN 50

    # Then: the LAN host on port 22, all of VLAN 60, SSH on the server.
    second = access_request(
        [
            {
                "interface": "vlan50",
                "internet": True,
                "destinations": [
                    {"network": "172.30.0.13", "ports": ["22/tcp"]},
                    {"network": "192.168.60.0/24"},
                ],
                "services": ["22/tcp"],
            }
        ],
        mac,
    )
    cid, _ = apply(host, second)
    assert call(host, "verify", {"id": cid})["passed"]
    assert (
        reaches(CLIENT, "172.30.0.13")
        and reaches(CLIENT, "192.168.50.1")
        and reaches(CLIENT, "192.168.60.20")
    )
    assert not reaches(OTHER, "192.168.50.20")  # VLAN 60's own rules did not change
    assert reaches(CLIENT, INTERNET)

    # Rolled back: as after the first change.
    assert call(host, "rollback", {"id": cid})["state"] == "rolled_back"
    assert not reaches(CLIENT, "172.30.0.13") and not reaches(CLIENT, "192.168.50.1")
    assert not reaches(CLIENT, "192.168.60.20")
    assert reaches(CLIENT, INTERNET)
    applied = json.loads(sh(host, "cat /etc/lanvex/networks.json"))["access"]
    assert applied == [{"interface": "vlan50", "internet": True, "destinations": [], "services": []}]


def client_dns(server: str, name: str) -> tuple[int, int]:
    """(answer code, number of answers) for an A query from the client, sent with bash alone."""
    labels = "".join(f"\\x{len(p):02x}{p}" for p in name.split("."))
    script = (
        f"exec 3<>/dev/udp/{server}/53; "
        f"printf '\\x4c\\x58\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00{labels}\\x00\\x00\\x01\\x00\\x01' >&3; "
        "timeout 3 head -c 8 <&3 | od -An -tu1"
    )
    out = sh(CLIENT, f'bash -c "{script}" || true').split()
    assert len(out) == 8, f"no answer from {server} for {name}"
    return int(out[3]) & 0xF, int(out[6]) * 256 + int(out[7])


def test_a_dns_filter_blocks_names_for_one_network(host: str) -> None:
    mac = client_mac()
    request = networks([pool(reservations=[{"mac": mac, "ip": "192.168.50.20", "name": "lab-nm"}])])
    request["access"] = {
        "rules": [{"interface": "vlan50", "dns_filter": {"block": ["blocked.lanvex.test"], "allow": []}}]
    }
    plan = call(host, "plan", request)
    assert "systemctl restart lanvex-dns@vlan50" in plan["commands"]
    cid, _ = apply(host, request)
    result = call(host, "verify", {"id": cid})
    assert result["passed"], result["checks"]
    check = next(c for c in result["checks"] if c["id"] == "dns:vlan50")
    assert "blocks blocked.lanvex.test" in check["detail"]
    call(host, "confirm", {"id": cid})

    # The client is told to ask the filter, which blocks the domain and every name below it, and hands
    # everything else to the main dnsmasq (which knows the client's own name).
    client_gets("192.168.50.20")
    assert sh(CLIENT, "nmcli -g IP4.DNS device show eth0.50").strip() == "10.99.53.1"
    sh(CLIENT, "ip route add 10.99.53.0/24 via 192.168.50.1 dev eth0.50")
    assert client_dns("10.99.53.1", "blocked.lanvex.test") == (3, 0)
    assert client_dns("10.99.53.1", "www.blocked.lanvex.test") == (3, 0)
    code, answers = client_dns("10.99.53.1", "lab-nm")
    assert code == 0 and answers >= 1

    # Switched off again, and that rolled back: the filter is back as it was.
    request["access"] = {"rules": []}
    cid, _ = apply(host, request)
    assert call(host, "verify", {"id": cid})["passed"]
    assert sh(host, "systemctl is-active lanvex-dns@vlan50 || true").strip() != "active"
    assert "lxdns" not in links(host)
    assert call(host, "rollback", {"id": cid})["state"] == "rolled_back"
    assert sh(host, "systemctl is-active lanvex-dns@vlan50 || true").strip() == "active"
    assert client_dns("10.99.53.1", "blocked.lanvex.test") == (3, 0)
