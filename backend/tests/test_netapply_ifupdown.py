"""The helper on ifupdown servers (lanvex_netapply.py): interface settings and bridges and bonds.

Files are written to a temporary /etc/network; nothing is run. The lab tests (test_changes_lab.py)
apply the same changes on a Debian 12 host, and ifupdown's own parser checks the files there.
"""

import shutil
from pathlib import Path

import pytest

from lanvex.integrations.linux import lanvex_netapply as h

from .test_netapply import diff

DEBIAN = """\
# This file describes the network interfaces available on your system
source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

# The primary network interface
allow-hotplug eth0
iface eth0 inet static
    # Docker pre-assigns the same address; clear it so ifupdown owns the configuration.
    pre-up ip addr flush dev eth0 || true
    address 172.30.0.15/24
    gateway 172.30.0.1
    up ip route replace 10.50.0.0/16 via 172.30.0.254 dev eth0
    down ip route del 10.50.0.0/16 via 172.30.0.254 dev eth0 || true
    mtu 1450

auto eth1
iface eth1 inet manual
"""


@pytest.fixture
def etc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "interfaces.d"
    d.mkdir()
    main = tmp_path / "interfaces"
    main.write_text(DEBIAN.replace("/etc/network/interfaces.d", str(d)))
    monkeypatch.setattr(h, "IFUPDOWN_MAIN", str(main))
    monkeypatch.setattr(h, "IFUPDOWN_DIR", str(d))
    monkeypatch.setattr(h, "IFUPDOWN_LANVEX", str(d / "lanvex"))
    monkeypatch.setattr(h, "RESOLV_HOOKS", (str(tmp_path / "000resolvconf"),))
    monkeypatch.setattr(h, "which", lambda name: f"/usr/sbin/{name}")
    return tmp_path


def plan(**request):  # type: ignore[no-untyped-def]
    base = {
        "interface": "eth0",
        "ipv4": {"method": "static", "addresses": ["172.30.0.15/24"], "gateway": "172.30.0.1"},
    }
    change = h.validate_change(base | request)
    return h.ifupdown_plan(change["interface"], change)


def test_a_second_address_is_a_stanza_of_its_own(etc: Path) -> None:
    p = plan(
        ipv4={"method": "static", "addresses": ["172.30.0.15/24", "172.30.0.16/24"], "gateway": "172.30.0.1"}
    )
    f = p["files"][0]
    assert p["backend"] == "ifupdown" and f["path"] == str(etc / "interfaces")
    assert f["after"] == f["before"].replace(
        "    mtu 1450\n", "    mtu 1450\n\niface eth0 inet static\n    address 172.30.0.16/24\n"
    )
    assert p["_ifupdown"] == {
        "interface": "eth0",
        "old": ["172.30.0.15/24"],
        "new": ["172.30.0.15/24", "172.30.0.16/24"],
    }

    # And taken away again: the extra stanza and the blank line before it go, nothing else moves.
    (etc / "interfaces").write_text(f["after"])
    back = plan()["files"][0]
    assert back["after"] == f["before"]


def test_address_gateway_and_routes_change_in_place(etc: Path) -> None:
    p = plan(
        ipv4={"method": "static", "addresses": ["172.30.0.20/24"], "gateway": "172.30.0.2"},
        routes=[{"to": "10.60.0.0/16", "via": "172.30.0.253", "metric": 30}],
    )
    assert diff(p["files"][0]["before"], p["files"][0]["after"]) == [
        "-    address 172.30.0.15/24",
        "-    gateway 172.30.0.1",
        "-    up ip route replace 10.50.0.0/16 via 172.30.0.254 dev eth0",
        "-    down ip route del 10.50.0.0/16 via 172.30.0.254 dev eth0 || true",
        "+    address 172.30.0.20/24",
        "+    gateway 172.30.0.2",
        "+    up ip route replace 10.60.0.0/16 via 172.30.0.253 metric 30 dev eth0",
        "+    down ip route del 10.60.0.0/16 via 172.30.0.253 metric 30 dev eth0 || true",
    ]
    assert p["_removed_routes"] == [{"to": "10.50.0.0/16", "via": "172.30.0.254"}]
    assert "mtu 1450" in p["files"][0]["after"]  # what Lanvex does not manage stays


def test_routes_left_out_stay_as_they_are(etc: Path) -> None:
    p = plan(ipv4={"method": "static", "addresses": ["172.30.0.15/24"], "gateway": None})
    assert diff(p["files"][0]["before"], p["files"][0]["after"]) == ["-    gateway 172.30.0.1"]


def test_dhcp(etc: Path) -> None:
    p = plan(ipv4={"method": "dhcp"})
    assert diff(p["files"][0]["before"], p["files"][0]["after"]) == [
        "-iface eth0 inet static",
        "+iface eth0 inet dhcp",
        "-    address 172.30.0.15/24",
        "-    gateway 172.30.0.1",
    ]


def test_dns_needs_resolvconf_or_resolved(etc: Path) -> None:
    with pytest.raises(h.Refused, match="resolvconf or systemd-resolved"):
        plan(dns={"servers": ["10.0.0.53"], "search": []})
    (etc / "000resolvconf").write_text("")
    p = plan(dns={"servers": ["10.0.0.53", "10.0.0.54"], "search": ["school.example"]})
    assert diff(p["files"][0]["before"], p["files"][0]["after"]) == [
        "+    dns-nameservers 10.0.0.53 10.0.0.54",
        "+    dns-search school.example",
    ]


def test_ipv6_static_auto_and_link_local(etc: Path) -> None:
    p = plan(
        ipv6={"method": "static", "addresses": ["fd00:30::15/64", "fd00:30::16/64"], "gateway": "fe80::1"}
    )
    after = p["files"][0]["after"]
    assert "iface eth0 inet6 static\n    address fd00:30::15/64\n    gateway fe80::1\n" in after
    assert "\niface eth0 inet6 static\n    address fd00:30::16/64\n" in after
    assert after.index("inet6") > after.index("mtu 1450")  # after the IPv4 stanza

    (etc / "interfaces").write_text(after)
    p = plan(ipv6={"method": "link-local"})
    assert "    mtu 1450\n\niface eth0 inet6 auto\n    accept_ra 0\n\nauto eth1\n" in p["files"][0]["after"]
    (etc / "interfaces").write_text(p["files"][0]["after"])
    p = plan(ipv6={"method": "auto"})
    assert diff(p["files"][0]["before"], p["files"][0]["after"]) == ["-    accept_ra 0"]


def test_ipv6_routes_go_in_the_ipv6_stanza(etc: Path) -> None:
    with pytest.raises(h.Refused, match="no IPv6 stanza"):
        plan(routes=[{"to": "fd00:99::/48", "via": "fd00:30::1"}])
    p = plan(
        ipv6={"method": "static", "addresses": ["fd00:30::15/64"]},
        routes=[{"to": "10.50.0.0/16", "via": "172.30.0.254"}, {"to": "fd00:99::/48", "via": "fd00:30::1"}],
    )
    after = p["files"][0]["after"]
    v6 = after[after.index("inet6") :]
    assert "up ip route replace fd00:99::/48 via fd00:30::1 dev eth0" in v6
    assert "10.50.0.0/16" not in v6 and "up ip route replace 10.50.0.0/16" in after


def test_refused_where_lanvex_would_have_to_guess(etc: Path) -> None:
    main = etc / "interfaces"
    main.write_text(DEBIAN + "\niface eth0 inet static\n    address 172.30.0.17/24\n    mtu 9000\n")
    with pytest.raises(h.Refused, match="settings besides its address"):
        plan()
    main.write_text(DEBIAN.replace("    mtu 1450", "    up route add -net 10.70.0.0/16 gw 172.30.0.254"))
    plan()  # routes not part of the change: the line stays
    with pytest.raises(h.Refused, match="command Lanvex does not manage"):
        plan(routes=[])
    main.write_text(DEBIAN.replace("iface eth0 inet static", "iface eth0 inet ppp"))
    with pytest.raises(h.Refused, match="ppp method"):
        plan()
    main.write_text(DEBIAN.replace("/etc/network/interfaces.d", str(etc / "interfaces.d")))
    (etc / "interfaces.d" / "eth0").write_text("iface eth0 inet6 auto\n")
    with pytest.raises(h.Refused, match="more than one ifupdown file"):
        plan()


def test_an_interface_in_a_sourced_file(etc: Path) -> None:
    (etc / "interfaces.d" / "vlan20").write_text(
        "auto ens19\niface ens19 inet static\n\taddress 10.20.0.1/24\n"
    )
    change = h.validate_change(
        {"interface": "ens19", "ipv4": {"method": "static", "addresses": ["10.20.0.1/24", "10.20.0.2/24"]}}
    )
    p = h.ifupdown_plan("ens19", change)
    assert p["files"][0]["path"] == str(etc / "interfaces.d" / "vlan20")
    assert p["files"][0]["after"].endswith(
        "\taddress 10.20.0.1/24\n\niface ens19 inet static\n\taddress 10.20.0.2/24\n"
    )


def test_bridges_and_bonds_need_no_extra_packages() -> None:
    bridge = h.validate_link(
        {
            "action": "create",
            "name": "br1",
            "type": "bridge",
            "members": ["eth1"],
            "method": "static",
            "addresses": ["10.12.0.1/24", "10.12.0.2/24"],
            "gateway": "10.12.0.254",
        }
    )
    text = h.render_link_ifupdown(bridge)
    assert "auto br1\niface br1 inet static\n" in text
    assert "    pre-up [ -e /sys/class/net/br1 ] || ip link add br1 type bridge\n" in text
    assert "    pre-up ip link set eth1 master br1 && ip link set eth1 up\n" in text
    assert "    address 10.12.0.1/24\n    gateway 10.12.0.254\n    post-down ip link delete br1\n" in text
    assert text.endswith("\niface br1 inet static\n    address 10.12.0.2/24\n")
    bond = h.validate_link(
        {"action": "create", "name": "bond0", "type": "bond", "members": ["eth1", "eth2"], "mode": "802.3ad"}
    )
    text = h.render_link_ifupdown(bond)
    assert "iface bond0 inet manual" in text and "type bond mode 802.3ad miimon 100" in text
    assert "pre-up ip link set eth2 down && ip link set eth2 master bond0 && ip link set eth2 up" in text


def test_a_member_with_addresses_of_its_own_is_refused(etc: Path) -> None:
    h.ifupdown_member_free("eth1")  # inet manual: fine
    with pytest.raises(h.Refused, match="IP settings of its own"):
        h.ifupdown_member_free("eth0")


def test_secrets_on_ifupdown_lines_are_redacted() -> None:
    assert (
        h.redact("    wpa-ssid school\n    wpa-psk hunter22\n")
        == "    wpa-ssid school\n    wpa-psk [redacted]\n"
    )


@pytest.mark.skipif(shutil.which("ifup") is None, reason="ifupdown is not installed here")
def test_ifupdown_parses_what_lanvex_writes(etc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "which", shutil.which)
    (etc / "000resolvconf").write_text("")
    p = plan(
        ipv4={"method": "static", "addresses": ["172.30.0.15/24", "172.30.0.16/24"], "gateway": "172.30.0.1"},
        dns={"servers": ["10.0.0.53"], "search": ["school.example"]},
        ipv6={"method": "static", "addresses": ["fd00:30::15/64"], "gateway": "fe80::1"},
        routes=[
            {"to": "10.60.0.0/16", "via": "172.30.0.253", "metric": 30},
            {"to": "fd00:99::/48", "via": "fd00:30::1"},
        ],
    )
    h.ifupdown_validate(p["_write"], "eth0")
    with pytest.raises(h.Refused, match="rejects"):
        h.ifupdown_validate({"x": "iface eth0 inet bogus\n"}, "eth0")
