"""Unit tests for the server-side helper's pure functions (lanvex_netapply.py).

These cover how the helper validates requests and rewrites netplan files and NetworkManager keyfiles.
The lab tests (test_changes_lab.py) run the same helper for real on netplan and NetworkManager hosts.
"""

import difflib
import json
import os

import pytest
import yaml

from lanvex.integrations.linux import lanvex_netapply as h


def change(**kw):
    base = {
        "method": "static",
        "addresses": ["10.0.0.5/24"],
        "gateway": "10.0.0.1",
        "dns_servers": ["10.0.0.53"],
        "dns_search": ["example.net"],
    }
    return base | kw


def diff(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


# ---- request validation ---------------------------------------------------------------------------------


def test_validate_static_and_dhcp():
    req = {
        "interface": "eth0",
        "ipv4": {"method": "static", "addresses": ["10.0.0.5/24", "10.0.0.6/24"], "gateway": "10.0.0.1"},
        "dns": {"servers": ["1.1.1.1"], "search": ["example.net."]},
    }
    assert h.validate_change(req) == {
        "interface": "eth0",
        "method": "static",
        "addresses": ["10.0.0.5/24", "10.0.0.6/24"],
        "gateway": "10.0.0.1",
        "dns_servers": ["1.1.1.1"],
        "dns_search": ["example.net"],
        "ipv6": None,  # left as they are
        "routes": None,
    }
    dhcp = h.validate_change({"interface": "ens3", "ipv4": {"method": "dhcp"}})
    assert dhcp["method"] == "dhcp" and dhcp["addresses"] == [] and dhcp["gateway"] is None


@pytest.mark.parametrize(
    ("ipv4", "message"),
    [
        ({"method": "static", "addresses": []}, "at least one address"),
        ({"method": "static", "addresses": ["10.0.0.0/24"]}, "network or broadcast"),
        ({"method": "static", "addresses": ["10.0.0.255/24"]}, "network or broadcast"),
        ({"method": "static", "addresses": ["10.0.0.5"]}, "prefix length"),
        ({"method": "static", "addresses": ["127.0.0.2/8"]}, "cannot be assigned"),
        ({"method": "static", "addresses": ["fd00::5/64"]}, "only IPv4"),
        ({"method": "static", "addresses": ["10.0.0.5/24", "10.0.0.5/16"]}, "same address twice"),
        ({"method": "static", "addresses": ["10.0.0.5/24"], "gateway": "10.1.0.1"}, "not inside"),
        ({"method": "static", "addresses": ["10.0.0.5/24"], "gateway": "10.0.0.5"}, "own addresses"),
        ({"method": "dhcp", "addresses": ["10.0.0.5/24"]}, "come from the DHCP server"),
        ({"method": "manual"}, "must be 'static' or 'dhcp'"),
    ],
)
def test_validate_rejects(ipv4, message):
    with pytest.raises(h.Refused) as e:
        h.validate_change({"interface": "eth0", "ipv4": ipv4})
    assert message in e.value.message


@pytest.mark.parametrize("name", ["lo", "eth0; rm -rf /", "../etc", "a" * 16, "", None, "eth 0"])
def test_validate_rejects_interface_names(name):
    with pytest.raises(h.Refused):
        h.validate_change({"interface": name, "ipv4": {"method": "dhcp"}})


def test_validate_rejects_bad_dns():
    with pytest.raises(h.Refused, match="not a valid domain"):
        h.validate_change({"interface": "eth0", "ipv4": {"method": "dhcp"}, "dns": {"search": ["a b"]}})
    with pytest.raises(h.Refused, match="not an IPv4 address"):
        h.validate_change(
            {"interface": "eth0", "ipv4": {"method": "dhcp"}, "dns": {"servers": ["dns.example"]}}
        )


def test_check_id():
    assert h.check_id("0" * 32) == "0" * 32
    for bad in ("0" * 31, "G" * 32, "../" + "0" * 29, None):
        with pytest.raises(h.Refused):
            h.check_id(bad)


# ---- netplan ----------------------------------------------------------------------------------------------


def test_edit_definition_keeps_ipv6_and_other_routes():
    old = {
        "dhcp4": True,
        "addresses": ["192.0.2.10/24", "2001:db8::10/64", {"192.0.2.11/24": {"label": "eth0:1"}}],
        "gateway4": "192.0.2.1",
        "routes": [
            {"to": "default", "via": "192.0.2.1"},
            {"to": "default", "via": "2001:db8::1"},
            {"to": "10.50.0.0/16", "via": "192.0.2.254"},
        ],
        "nameservers": {"addresses": ["192.0.2.53", "2001:db8::53"], "search": ["old.example"]},
        "mtu": 9000,
    }
    new = h.edit_netplan_definition(old, change())
    assert new == {
        "dhcp4": False,
        "addresses": ["10.0.0.5/24", "2001:db8::10/64"],
        "routes": [
            {"to": "default", "via": "2001:db8::1"},
            {"to": "10.50.0.0/16", "via": "192.0.2.254"},
            {"to": "default", "via": "10.0.0.1"},
        ],
        "nameservers": {"addresses": ["10.0.0.53", "2001:db8::53"], "search": ["example.net"]},
        "mtu": 9000,
    }
    assert old["gateway4"] == "192.0.2.1"  # the input is not modified


def test_edit_definition_to_dhcp_and_without_dns():
    old = {
        "addresses": ["192.0.2.10/24"],
        "routes": [{"to": "default", "via": "192.0.2.1"}],
        "nameservers": {"addresses": ["192.0.2.53"]},
    }
    new = h.edit_netplan_definition(
        old, change(method="dhcp", addresses=[], gateway=None, dns_servers=[], dns_search=[])
    )
    assert new == {"dhcp4": True}


def test_edit_definition_clears_networkmanager_passthrough_overrides():
    old = {
        "renderer": "NetworkManager",
        "match": {"name": "eth1"},
        "addresses": ["172.31.0.12/24"],
        "networkmanager": {
            "uuid": "u",
            "passthrough": {"ipv4.never-default": "true", "ipv4.dns-search": "x;", "ipv6.method": "disabled"},
        },
    }
    new = h.edit_netplan_definition(old, change(addresses=["172.31.0.12/24"], gateway="172.31.0.1"))
    assert new["networkmanager"]["passthrough"] == {"ipv6.method": "disabled"}


NETPLAN = """\
# Managed by the platform team
network:
  version: 2
  renderer: networkd
  ethernets:
    eth0:
      addresses: [172.30.0.11/24]
      routes:
        - to: default
          via: 172.30.0.1
      nameservers:
        addresses: [172.30.0.1, 9.9.9.9]
        search: [lab.example.net]

  # storage network
  bridges:
    br0:
      interfaces: []
      addresses: [10.11.0.1/24]
      ignore-carrier: true
"""


def render(text, section, key, ch):
    doc = yaml.safe_load(text)
    old = doc["network"][section][key]
    new = h.edit_netplan_definition(old, ch)
    doc["network"][section][key] = new
    after, rewritten = h.render_netplan(text, ("network", section, key), old, new, doc)
    assert yaml.safe_load(after) == doc
    return after, rewritten


def test_render_changes_only_the_changed_keys():
    after, rewritten = render(
        NETPLAN,
        "bridges",
        "br0",
        change(addresses=["10.11.0.1/24", "10.11.0.2/24"], gateway=None, dns_servers=[], dns_search=[]),
    )
    assert not rewritten
    assert diff(NETPLAN, after) == [
        "-      addresses: [10.11.0.1/24]",
        "+      addresses: [10.11.0.1/24, 10.11.0.2/24]",
    ]
    after, _ = render(
        NETPLAN,
        "ethernets",
        "eth0",
        change(
            addresses=["172.30.0.11/24"],
            gateway="172.30.0.1",
            dns_servers=["172.30.0.1", "1.1.1.1"],
            dns_search=["lab.example.net"],
        ),
    )
    # The routes did not change, so their block style and the comments around the block survive.
    assert diff(NETPLAN, after) == [
        "-        addresses: [172.30.0.1, 9.9.9.9]",
        "+        addresses: [172.30.0.1, 1.1.1.1]",
    ]
    assert "# storage network" in after and "# Managed by the platform team" in after


def test_render_new_keys_use_block_style():
    after, _ = render(
        NETPLAN,
        "bridges",
        "br0",
        change(addresses=["10.11.0.1/24"], gateway="10.11.0.254", dns_servers=["10.11.0.53"], dns_search=[]),
    )
    assert diff(NETPLAN, after) == [
        "+      routes:",
        "+      - to: default",
        "+        via: 10.11.0.254",
        "+      nameservers:",
        "+        addresses: [10.11.0.53]",
    ]


def test_render_flow_style_definition():
    text = "network:\n  version: 2\n  ethernets:\n    eth0: {addresses: [192.0.2.10/24], dhcp4: false}\n"
    after, rewritten = render(text, "ethernets", "eth0", change(gateway=None, dns_servers=[], dns_search=[]))
    assert not rewritten
    assert diff(text, after) == [
        "-    eth0: {addresses: [192.0.2.10/24], dhcp4: false}",
        "+    eth0: {addresses: [10.0.0.5/24], dhcp4: false}",
    ]


def test_render_falls_back_to_a_full_rewrite_when_it_cannot_splice():
    text = "network:\n  version: 2\n  ethernets:\n    eth0:\n"  # an empty definition
    doc = yaml.safe_load(text)
    new = h.edit_netplan_definition(doc["network"]["ethernets"]["eth0"], change())
    doc["network"]["ethernets"]["eth0"] = new
    after, rewritten = h.render_netplan(text, ("network", "ethernets", "eth0"), None, new, doc)
    assert rewritten and yaml.safe_load(after) == doc


FACTS = {"mac": "52:54:00:12:34:56", "driver": "virtio_net"}


def test_definitions_by_name_set_name_and_exact_match():
    docs = {
        "/etc/netplan/50.yaml": {"network": {"ethernets": {"eth0": {"dhcp4": True}}}},
        "/etc/netplan/60.yaml": {
            "network": {
                "ethernets": {"nic": {"match": {"macaddress": "52:54:00:aa:bb:cc"}, "set-name": "eth9"}}
            }
        },
        "/etc/netplan/90-NM-x.yaml": {"network": {"ethernets": {"NM-x": {"match": {"name": "eth1"}}}}},
        "/etc/netplan/91.yaml": {
            "network": {"ethernets": {"by-mac": {"match": {"macaddress": "52:54:00:12:34:56"}}}}
        },
    }
    assert h.netplan_definitions("eth0", docs, FACTS) == [("/etc/netplan/50.yaml", "ethernets", "eth0")] + [
        ("/etc/netplan/91.yaml", "ethernets", "by-mac")  # same MAC: also eth0
    ]
    assert h.netplan_definitions("eth9", docs, {"mac": "x"}) == [("/etc/netplan/60.yaml", "ethernets", "nic")]
    assert h.netplan_definitions("eth1", docs, {"mac": "y"}) == [
        ("/etc/netplan/90-NM-x.yaml", "ethernets", "NM-x")
    ]


def test_definitions_refuse_broad_match_rules_but_ignore_unrelated_ones():
    glob = {"/etc/netplan/50.yaml": {"network": {"ethernets": {"all": {"match": {"name": "en*"}}}}}}
    with pytest.raises(h.Refused, match="also covers"):
        h.netplan_definitions("ens3", glob, FACTS)
    assert h.netplan_definitions("eth0", glob, FACTS) == []  # en* cannot select eth0
    by_driver = {"/etc/netplan/50.yaml": {"network": {"ethernets": {"d": {"match": {"driver": "virtio*"}}}}}}
    with pytest.raises(h.Refused):
        h.netplan_definitions("eth0", by_driver, FACTS)


# ---- NetworkManager keyfiles -------------------------------------------------------------------------------

KEYFILE = """\
[connection]
id=storage
type=ethernet
interface-name=eth1

[ipv4]
# keep this comment
method=manual
address1=172.31.0.12/24,172.31.0.1
dns=172.31.0.53;
route1=10.50.0.0/16,172.31.0.1,50
never-default=true

[ipv6]
method=disabled
"""


def test_keyfile_static_with_gateway():
    after = h.edit_keyfile(
        KEYFILE,
        change(addresses=["172.31.0.12/24", "172.31.0.13/24"], gateway="172.31.0.1", dns_search=[]),
    )
    assert after.split("[ipv4]")[1].split("[ipv6]")[0].strip().splitlines() == [
        "# keep this comment",
        "method=manual",
        "address1=172.31.0.12/24",
        "address2=172.31.0.13/24",
        "gateway=172.31.0.1",
        "dns=10.0.0.53;",
        "route1=10.50.0.0/16,172.31.0.1,50",
    ]
    assert after.startswith("[connection]\nid=storage") and after.endswith("[ipv6]\nmethod=disabled\n")


def test_keyfile_without_gateway_keeps_never_default():
    after = h.edit_keyfile(
        KEYFILE, change(addresses=["172.31.0.12/24"], gateway=None, dns_servers=[], dns_search=[])
    )
    assert "never-default=true" in after and "gateway" not in after and "dns=" not in after
    assert "address1=172.31.0.12/24\n" in after


def test_keyfile_dhcp_and_missing_ipv4_section():
    after = h.edit_keyfile(
        KEYFILE, change(method="dhcp", addresses=[], gateway=None, dns_servers=[], dns_search=[])
    )
    ipv4 = after.split("[ipv4]")[1].split("[ipv6]")[0]
    assert "method=auto" in ipv4 and "address" not in ipv4
    bare = "[connection]\nid=x\ntype=ethernet\n"
    assert h.edit_keyfile(bare, change(gateway=None, dns_servers=[], dns_search=[])).endswith(
        "\n\n[ipv4]\nmethod=manual\naddress1=10.0.0.5/24\n"
    )


def test_nm_split_unescapes():
    assert h.nm_split(r"uuid:eth0:/etc/x\:y.nmconnection:My\\Name") == [
        "uuid",
        "eth0",
        "/etc/x:y.nmconnection",
        "My\\Name",
    ]


def test_usage_is_json(capsys):
    assert h.main(["lanvex-netapply"]) == 2
    assert '"code": "usage"' in capsys.readouterr().out


# ---- leaving DNS alone ------------------------------------------------------------------------------------


def test_dns_null_keeps_dns_settings_untouched():
    kept = h.validate_change(
        {"interface": "eth0", "ipv4": {"method": "static", "addresses": ["10.0.0.5/24"]}, "dns": None}
    )
    assert kept["dns_servers"] is None and kept["dns_search"] is None
    old = {
        "addresses": ["10.0.0.4/24"],
        "nameservers": {"addresses": ["10.0.0.53"], "search": ["example.net"]},
        "networkmanager": {"passthrough": {"ipv4.dns-search": "x;", "ipv4.method": "manual"}},
    }
    new = h.edit_netplan_definition(old, change(gateway=None, dns_servers=None, dns_search=None))
    assert new["nameservers"] == old["nameservers"] and new["addresses"] == ["10.0.0.5/24"]
    assert new["networkmanager"]["passthrough"] == {"ipv4.dns-search": "x;"}
    after = h.edit_keyfile(
        KEYFILE, change(addresses=["172.31.0.14/24"], gateway=None, dns_servers=None, dns_search=None)
    )
    assert "dns=172.31.0.53;" in after and "address1=172.31.0.14/24" in after
    assert diff(KEYFILE, after) == ["-address1=172.31.0.12/24,172.31.0.1", "+address1=172.31.0.14/24"]


def test_keyfile_section_without_managed_keys_gets_them_at_its_end():
    text = "[connection]\nid=x\n\n[ipv4]\nroute1=10.0.0.0/8,10.0.0.1\n\n[ipv6]\nmethod=auto\n"
    after = h.edit_keyfile(text, change(gateway=None, dns_servers=None, dns_search=None))
    assert "[ipv4]\nroute1=10.0.0.0/8,10.0.0.1\nmethod=manual\naddress1=10.0.0.5/24\n\n[ipv6]" in after


def test_redact_hides_secret_values_only():
    text = 'password: "s3cret"\npsk=hunter2\nkey-mgmt=wpa-psk\npinned=yes\nprivate-key-password=abc\n'
    assert (
        h.redact(text)
        == "password: [redacted]\npsk=[redacted]\nkey-mgmt=wpa-psk\npinned=yes\nprivate-key-password=[redacted]\n"
    )


# ---- verification: is the gateway there? ------------------------------------------------------------


class _Done:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def fake_tools(monkeypatch, ping_ok: bool | None, neigh: str):
    """ping_ok None: ping is not installed. neigh: `ip -json neighbour show` output."""
    calls = []

    def run(argv, timeout=60, check=False):
        calls.append(argv[0])
        if argv[0] == "ping":
            return _Done(0 if ping_ok else 1, f"3 packets transmitted, {3 if ping_ok else 0} received")
        assert argv[:5] == ["ip", "-json", "-4", "neighbour", "show"]
        return _Done(0, neigh)

    monkeypatch.setattr(h, "run", run)
    monkeypatch.setattr(
        h, "which", lambda name: None if ping_ok is None and name == "ping" else "/usr/bin/" + name
    )
    return calls


REACHABLE = '[{"dst":"10.0.0.1","dev":"eth0","lladdr":"52:54:00:12:34:56","state":["REACHABLE"]}]'
STALE = '[{"dst":"10.0.0.1","dev":"eth0","lladdr":"52:54:00:12:34:56","state":["STALE"]}]'
UNRESOLVED = '[{"dst":"10.0.0.1","dev":"eth0","state":["FAILED"]}]'


def test_gateway_that_answers_ping(monkeypatch):
    calls = fake_tools(monkeypatch, True, UNRESOLVED)
    c = h.gateway_check("eth0", "10.0.0.1")
    assert c["ok"] is True and c["label"] == "Gateway 10.0.0.1 answers" and calls == ["ping"]


@pytest.mark.parametrize("neigh", [REACHABLE, STALE])
def test_gateway_that_drops_ping_but_answers_arp_passes(monkeypatch, neigh):
    fake_tools(monkeypatch, False, neigh)
    c = h.gateway_check("eth0", "10.0.0.1")
    assert c["ok"] is True
    assert c["detail"] == "Does not answer ping, but is reachable on the link (52:54:00:12:34:56)."


@pytest.mark.parametrize(
    "neigh", [UNRESOLVED, "[]", '[{"dst":"10.0.0.1","dev":"eth0","state":["INCOMPLETE"]}]']
)
def test_absent_or_mistyped_gateway_fails(monkeypatch, neigh):
    fake_tools(monkeypatch, False, neigh)
    c = h.gateway_check("eth0", "10.0.0.1")
    assert c["ok"] is False and "0 received" in c["detail"]


def test_without_ping_the_arp_table_decides_or_the_check_is_skipped(monkeypatch):
    fake_tools(monkeypatch, None, REACHABLE)
    assert h.gateway_check("eth0", "10.0.0.1")["ok"] is True
    fake_tools(monkeypatch, None, "[]")
    c = h.gateway_check("eth0", "10.0.0.1")
    assert c["ok"] is None and "not checked" not in c["detail"] and "neighbour table" in c["detail"]


# ---- networks: VLAN interfaces, firewalld zones and DHCP -------------------------------------------------

TEST_VLAN = {"name": "vlan50", "parent": "eth0", "id": 50, "addresses": ["192.168.50.1/24"]}
TEST_POOL = {
    "interface": "vlan50",
    "start": "192.168.50.100",
    "end": "192.168.50.199",
    "lease": "2h",
    "reservations": [
        {"mac": "AA-BB-CC-00-00-02", "ip": "192.168.50.20", "name": "drucker"},
        {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.50.10", "name": ""},
    ],
}


def networks(vlans=None, pools=None):  # type: ignore[no-untyped-def]
    return {
        "kind": "networks",
        "vlans": vlans if vlans is not None else [TEST_VLAN],
        "dhcp": None if pools is None else {"pools": pools},
    }


def test_networks_validate_and_defaults():
    out = h.validate_networks(networks(pools=[TEST_POOL]))
    assert out["vlans"] == [TEST_VLAN]
    pool = out["dhcp"]["pools"][0]
    assert pool["network"] == "192.168.50.0/24"
    assert pool["router"] == "192.168.50.1" and pool["dns"] == ["192.168.50.1"]  # the server itself
    # MAC addresses normalised, fixed addresses sorted by address
    assert [r["mac"] for r in pool["reservations"]] == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    assert h.validate_networks(networks())["dhcp"] is None  # DHCP stays as it is
    assert h.validate_networks(networks(vlans=[], pools=[]))["dhcp"] == {"pools": []}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (networks(vlans=[TEST_VLAN | {"id": 0}]), "between 1 and 4094"),
        (networks(vlans=[TEST_VLAN, TEST_VLAN | {"name": "vlan51"}]), "VLAN 50 on eth0 is listed twice"),
        (networks(vlans=[TEST_VLAN | {"addresses": []}]), "needs between 1 and 8"),
        (networks(vlans=[TEST_VLAN | {"name": "lo"}]), "loopback"),
        (
            networks(vlans=[TEST_VLAN, TEST_VLAN | {"name": "v51", "parent": "vlan50", "id": 51}]),
            "on another Lanvex VLAN",
        ),
        (networks(pools=[TEST_POOL | {"interface": "eth0"}]), "only on VLAN interfaces it manages"),
        (networks(pools=[TEST_POOL | {"end": "192.168.51.10"}]), "not inside one network"),
        (
            networks(pools=[TEST_POOL | {"start": "192.168.50.1"}]),
            "own address 192.168.50.1 is inside the pool",
        ),
        (networks(pools=[TEST_POOL | {"start": "192.168.50.200", "end": "192.168.50.100"}]), "starts"),
        (networks(pools=[TEST_POOL | {"lease": "forever"}]), "lease time"),
        (
            networks(pools=[TEST_POOL | {"reservations": [{"mac": "nope", "ip": "192.168.50.5"}]}]),
            "not a MAC address",
        ),
        (
            networks(
                pools=[TEST_POOL | {"reservations": [{"mac": "aa:bb:cc:00:00:01", "ip": "192.168.50.1"}]}]
            ),
            "not a free address",
        ),
        (networks(pools=[TEST_POOL, TEST_POOL]), "more than one DHCP pool"),
    ],
)
def test_networks_validate_rejects(change, message):  # type: ignore[no-untyped-def]
    with pytest.raises(h.Refused) as e:
        h.validate_networks(change)
    assert message in e.value.message


def test_zone_names_fit_firewalld():
    assert h.zone_name("vlan50") == "lx-vlan50"
    assert len(h.zone_name("a-very-long-nam")) == 17
    long = TEST_VLAN | {"name": "classroom-wing1"}
    with pytest.raises(h.Refused, match="zone name"):
        h.validate_networks(networks(vlans=[long, long | {"name": "classroom-wing2", "id": 51}]))


def test_render_vlan_profile_zone_and_dnsmasq():
    change = h.validate_networks(networks(pools=[TEST_POOL]))
    v = change["vlans"][0]
    keyfile = h.render_vlan_keyfile(v, "11111111-2222-3333-4444-555555555555", "lx-vlan50")
    assert (
        "id=lanvex-vlan50\n" in keyfile
        and "interface-name=vlan50\n" in keyfile
        and "zone=lx-vlan50\n" in keyfile
    )
    assert "[vlan]\nflags=1\nid=50\nparent=eth0\n" in keyfile
    assert "[ipv4]\nmethod=manual\naddress1=192.168.50.1/24\n" in keyfile
    assert "zone=" not in h.render_vlan_keyfile(v, "u", None)  # without firewalld

    zone = h.render_zone(v, ["dhcp", "dns"])
    assert '<service name="dhcp"/>' in zone and '<service name="dns"/>' in zone
    assert "--" not in zone.split("<!--")[1].split("-->")[0]  # XML forbids "--" inside a comment

    conf = h.render_dnsmasq(change["dhcp"]["pools"])
    assert "interface=vlan50\n" in conf
    assert "dhcp-range=set:lx-vlan50,192.168.50.100,192.168.50.199,255.255.255.0,2h\n" in conf
    assert "dhcp-option=tag:lx-vlan50,option:router,192.168.50.1\n" in conf
    assert "dhcp-option=tag:lx-vlan50,option:dns-server,192.168.50.1\n" in conf
    assert "dhcp-host=aa:bb:cc:00:00:01,set:lx-vlan50,192.168.50.10\n" in conf
    assert "dhcp-host=aa:bb:cc:00:00:02,set:lx-vlan50,192.168.50.20,drucker\n" in conf


def test_dnsmasq_reads_its_directory_and_conflicts(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    conf, d = tmp_path / "dnsmasq.conf", tmp_path / "dnsmasq.d"
    d.mkdir()
    monkeypatch.setattr(h, "DNSMASQ_CONF", str(conf))
    monkeypatch.setattr(h, "DNSMASQ_DIR", str(d))
    monkeypatch.setattr(h, "DNSMASQ_FILE", str(d / "lanvex.conf"))
    conf.write_text("#conf-dir=/elsewhere\n")
    assert h.dnsmasq_reads_dir() is False
    conf.write_text(f"user=dnsmasq\nconf-dir={d},.rpmnew,.rpmsave,.rpmorig\n")  # as Fedora ships it
    assert h.dnsmasq_reads_dir() is True

    pools = h.validate_networks(networks(pools=[TEST_POOL]))["dhcp"]["pools"]
    (d / "lanvex.conf").write_text("interface=vlan50\n")  # Lanvex's own file never conflicts
    (d / "switch-mgmt.conf").write_text("interface=eno2\ndhcp-range=192.168.178.50,192.168.178.99\n")
    assert h.dnsmasq_conflicts(pools) == []
    (d / "nv-vlans.conf").write_text("# Netzverwaltung\ninterface=vlan50  # comment\n")
    assert h.dnsmasq_conflicts(pools) == [(str(d / "nv-vlans.conf"), "interface=vlan50")]
    (d / "nv-vlans.conf").write_text("dhcp-range=set:x,192.168.50.150,192.168.50.160,12h\n")
    assert h.dnsmasq_conflicts(pools)[0][0].endswith("nv-vlans.conf")


def test_networks_requests_go_through_plan_for(monkeypatch):  # type: ignore[no-untyped-def]
    seen = {}

    def fake_plan(change):  # type: ignore[no-untyped-def]
        seen["change"] = change
        return {"backend": "networkmanager", "files": [], "_changed": False}

    monkeypatch.setattr(h, "networks_plan", fake_plan)
    kind, change, _ = h.plan_for(networks(pools=[TEST_POOL]))
    assert kind == "networks" and seen["change"] == change
    with pytest.raises(h.Refused, match="Unknown kind"):
        h.plan_for({"kind": "firewall"})
    with pytest.raises(h.Refused) as e:
        h.cmd_plan(networks())
    assert e.value.code == "no_change"


def test_render_netplan_and_ifupdown_vlans():
    v = h.validate_networks(networks())["vlans"][0] | {"addresses": ["192.168.50.1/24", "10.50.0.1/24"]}
    doc = h._yaml().safe_load(h.render_netplan_vlans([v], {"eth0": "trunk"}))
    assert doc == {
        "network": {
            "version": 2,
            "vlans": {
                "vlan50": {"id": 50, "link": "trunk", "addresses": ["192.168.50.1/24", "10.50.0.1/24"]}
            },
        }
    }

    text = h.render_ifupdown_vlans([v, v | {"name": "vlan60", "id": 60, "addresses": ["192.168.60.1/24"]}])
    assert "auto vlan50\niface vlan50 inet static\n" in text
    assert (
        "    pre-up [ -e /sys/class/net/vlan50 ] || ip link add link eth0 name vlan50 type vlan id 50\n"
        in text
    )
    assert "    address 192.168.50.1/24\n    up ip address add 10.50.0.1/24 dev vlan50\n" in text
    assert "    post-down ip link delete vlan50\n" in text
    stanzas = h.ifupdown_stanzas(text)
    assert sorted(stanzas) == ["vlan50", "vlan60"]
    assert "vlan60" not in stanzas["vlan50"] and "id 60" in stanzas["vlan60"]


def test_ifupdown_must_read_its_directory(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    main, d = tmp_path / "interfaces", tmp_path / "interfaces.d"
    d.mkdir()
    monkeypatch.setattr(h, "IFUPDOWN_MAIN", str(main))
    monkeypatch.setattr(h, "IFUPDOWN_DIR", str(d))
    monkeypatch.setattr(h, "IFUPDOWN_LANVEX", str(d / "lanvex"))
    main.write_text("auto lo\niface lo inet loopback\n")
    assert h.ifupdown_sources_dir() is False
    main.write_text(f"source {d}/*\n\nauto eth0\niface eth0 inet static\n    address 10.0.0.2/24\n")
    assert h.ifupdown_sources_dir() is True
    main.write_text(f"source-directory {d}\n")
    assert h.ifupdown_sources_dir() is True
    (d / "vlans").write_text("auto vlan50\niface vlan50 inet manual\n")
    (d / "lanvex").write_text("iface vlan60 inet static\n")  # Lanvex's own file never counts
    assert h.ifupdown_defined_elsewhere("vlan50") == str(d / "vlans")
    assert h.ifupdown_defined_elsewhere("vlan60") is None


def test_zone_names_its_interface_without_networkmanager():
    v = h.validate_networks(networks())["vlans"][0]
    assert '<interface name="vlan50"/>' in h.render_zone(v, ["dhcp"], bind=True)
    assert "<interface" not in h.render_zone(v, ["dhcp"])  # NetworkManager sets the zone per profile


def test_dnsmasq_modes(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    conf, d = tmp_path / "dnsmasq.conf", tmp_path / "dnsmasq.d"
    d.mkdir()
    monkeypatch.setattr(h, "DNSMASQ_CONF", str(conf))
    monkeypatch.setattr(h, "DNSMASQ_DIR", str(d))
    monkeypatch.setattr(h, "DNSMASQ_FILE", str(d / "lanvex.conf"))
    pools = h.validate_networks(networks(pools=[TEST_POOL]))["dhcp"]["pools"]

    conf.write_text(f"conf-dir={d}\n")
    # Not running yet: answer only on Lanvex's interfaces, and bind only them (systemd-resolved holds :53).
    assert h.dnsmasq_mode(active=False) == (True, True)
    # Running for someone else on every interface: listing ours would end that.
    assert h.dnsmasq_mode(active=True) == (False, False)
    (d / "lan.conf").write_text("interface=eno1\nbind-interfaces\n")
    # Restricted already, and binding chosen elsewhere (bind-dynamic next to bind-interfaces fails).
    assert h.dnsmasq_mode(active=True) == (True, False)
    assert h.dnsmasq_mode(active=False) == (True, False)
    (d / "lan.conf").unlink()

    # Fedora's and RHEL's default: loopback alone, its addresses bound. Lanvex's interface lines end
    # that, so it lists its interfaces and binds only their addresses, running or not.
    conf.write_text(f"local-service=host\nconf-dir={d}\n")
    assert h.dnsmasq_others()["loopback_only"] is True
    assert h.dnsmasq_mode(active=True) == (True, True)
    assert h.dnsmasq_mode(active=False) == (True, True)
    conf.write_text(f"local-service\nconf-dir={d}\n")  # local networks on every interface
    assert h.dnsmasq_mode(active=True) == (False, False)

    # Taking over: dnsmasq answers as the replaced configuration has it (only its interfaces, the
    # wildcard address bound), not as it would without that configuration.
    conf.write_text(f"local-service=host\nconf-dir={d}\n")
    (d / "nv-vlans.conf").write_text("interface=gast\ndhcp-range=set:gast,192.168.50.100,192.168.50.199\n")
    monkeypatch.setattr(h, "TAKING_OVER", {str(d / "nv-vlans.conf")})
    assert h.dnsmasq_others() == {"restricted": True, "bind": False, "loopback_only": True}
    assert h.dnsmasq_others(now=True) == {"restricted": True, "bind": False, "loopback_only": False}
    assert h.dnsmasq_mode(active=True) == (True, False)
    conf.write_text(f"conf-dir={d}\n")
    assert h.dnsmasq_mode(active=True) == (True, False)
    monkeypatch.setattr(h, "TAKING_OVER", set())
    (d / "nv-vlans.conf").unlink()
    conf.write_text(f"conf-dir={d}\n")

    conf_text = h.render_dnsmasq(pools, interfaces=True, bind_dynamic=True)
    assert "\nbind-dynamic\n" in conf_text and "interface=vlan50\n" in conf_text
    (d / "lanvex.conf").write_text(conf_text)
    assert h.dnsmasq_mode(active=True) == (True, True)  # kept as Lanvex wrote it
    bare = h.render_dnsmasq(pools, interfaces=False)
    assert "interface=" not in bare and "bind-dynamic" not in bare
    assert "dhcp-range=set:lx-vlan50," in bare
    (d / "lanvex.conf").write_text(bare)
    assert h.dnsmasq_mode(active=False) == (False, False)


def test_ufw_rules_are_marked_and_removable():
    rules = h.ufw_wanted_rules("vlan50")
    assert rules[0] == "ufw allow in on vlan50 to any port 67 proto udp comment 'lanvex:vlan50'"
    assert h.ufw_rule_argv(rules[0]) == [
        "ufw", "allow", "in", "on", "vlan50", "to", "any", "port", "67", "proto", "udp", "comment", "lanvex:vlan50",
    ]  # fmt: skip
    assert h.ufw_rule_argv(rules[2], delete=True) == [
        "ufw", "delete", "allow", "in", "on", "vlan50", "to", "any", "port", "53", "proto", "tcp",
    ]  # fmt: skip
    route = "ufw route allow in on vlan50 out on eth0 to 10.0.0.0/8 comment 'lanvex:vlan50'"
    assert h.ufw_rule_argv(route, delete=True) == [
        "ufw", "route", "delete", "allow", "in", "on", "vlan50", "out", "on", "eth0", "to", "10.0.0.0/8",
    ]  # fmt: skip


def test_backend_follows_the_parent_interface(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    change = h.validate_networks(networks())
    monkeypatch.setattr(h, "MANIFEST", str(tmp_path / "networks.json"))
    monkeypatch.setattr(h, "NETPLAN_DIR", str(tmp_path / "netplan"))
    monkeypatch.setattr(h, "IFUPDOWN_MAIN", str(tmp_path / "interfaces"))
    tools = {"netplan": True, "ifup": True}
    monkeypatch.setattr(h, "which", lambda name: "/usr/sbin/" + name if tools.get(name) else None)
    nm = {"running": True, "states": {"eth0": "connected"}}
    monkeypatch.setattr(h, "nm_running", lambda: nm["running"])
    monkeypatch.setattr(h, "nm_device_states", lambda: nm["states"])
    monkeypatch.setattr(h, "netplan_definitions", lambda iface, docs: [("f", "ethernets", iface)])
    (tmp_path / "netplan").mkdir()
    (tmp_path / "interfaces").write_text("")

    assert h.choose_backend(change) == "networkmanager"
    nm["states"] = {"eth0": "unmanaged"}  # Ubuntu Server: NetworkManager installed, networkd in charge
    assert h.choose_backend(change) == "netplan"
    nm["running"] = False
    tools["netplan"] = False
    assert h.choose_backend(change) == "ifupdown"
    tools["ifup"] = False
    with pytest.raises(h.Refused, match="none of them does here"):
        h.choose_backend(change)
    # Once Lanvex has VLANs on the server, it stays with the way it created them.
    (tmp_path / "networks.json").write_text(h.render_manifest("ifupdown", ["vlan50"]))
    assert h.choose_backend(change) == "ifupdown"


def test_other_dnsmasq_configuration_is_found_in_every_file_dnsmasq_reads(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    conf, d = tmp_path / "dnsmasq.conf", tmp_path / "dnsmasq.d"
    d.mkdir()
    monkeypatch.setattr(h, "DNSMASQ_CONF", str(conf))
    monkeypatch.setattr(h, "DNSMASQ_DIR", str(d))
    monkeypatch.setattr(h, "DNSMASQ_FILE", str(d / "lanvex.conf"))
    conf.write_text(f"conf-dir={d},.rpmnew,.rpmsave,.rpmorig\n")
    (d / "lanvex.conf").write_text(
        "dhcp-range=192.168.50.100,192.168.50.199\n"
    )  # Lanvex's own: never "other"
    (d / "nv-vlans").write_text(
        "# Netzverwaltung\ninterface=vlan10\ndhcp-range=192.168.10.50,192.168.10.99\n"
    )
    (d / "old.conf.rpmsave").write_text("dhcp-range=192.168.20.50,192.168.20.99\n")  # not read by dnsmasq
    assert h.dnsmasq_serves_others() == [str(d / "nv-vlans")]  # no .conf ending needed (Fedora, Debian)
    assert h.dnsmasq_others() == {"restricted": True, "bind": False, "loopback_only": False}
    pools = h.validate_networks(networks(pools=[TEST_POOL]))["dhcp"]["pools"]
    assert h.dnsmasq_conflicts(pools) == []
    (d / "nv-vlans").write_text("dhcp-range=192.168.50.20,192.168.50.30\n")
    assert h.dnsmasq_conflicts(pools) == [(str(d / "nv-vlans"), "dhcp-range=192.168.50.20,192.168.50.30")]


ACCESS_VLANS = {
    "vlan50": {"name": "vlan50", "parent": "eth0", "id": 50, "addresses": ["192.168.50.1/24"]},
    "vlan60": {"name": "vlan60", "parent": "eth0", "id": 60, "addresses": ["192.168.60.1/24"]},
}


def test_access_rules_are_validated_and_normalised():
    access = {
        "rules": [
            {
                "interface": "vlan50",
                "internet": True,
                "destinations": [
                    {"network": "192.168.178.3", "ports": ["443/tcp", "80/tcp", "8000-8100/tcp"]},
                    {"network": "192.168.60.7/24"},
                ],
                "services": ["22/tcp", "22/tcp"],
            }
        ]
    }
    out = h.validate_access(access, ACCESS_VLANS)
    assert out == {
        "rules": [
            {
                "interface": "vlan50",
                "internet": True,
                "destinations": [
                    {"network": "192.168.60.0/24", "ports": []},
                    {"network": "192.168.178.3/32", "ports": ["80/tcp", "443/tcp", "8000-8100/tcp"]},
                ],
                "services": ["22/tcp"],
            }
        ]
    }
    assert h.validate_access(None, ACCESS_VLANS) is None  # as last applied
    for bad, message in [
        ({"interface": "eth0"}, "Lanvex's VLAN interfaces"),
        ({"interface": "vlan50", "destinations": [{"network": "0.0.0.0/0"}]}, "with internet"),
        ({"interface": "vlan50", "destinations": [{"network": "192.168.50.0/25"}]}, "this network itself"),
        ({"interface": "vlan50", "services": ["ssh"]}, "not a port"),
        ({"interface": "vlan50", "services": ["0/tcp"]}, "between 1 and 65535"),
        ({"interface": "vlan50", "services": ["90-80/tcp"]}, "between 1 and 65535"),
        ({"interface": "vlan50", "internet": "yes"}, "true or false"),
    ]:
        with pytest.raises(h.Refused, match=message):
            h.validate_access({"rules": [bad]}, ACCESS_VLANS)


def test_firewalld_policy_for_the_internet_and_a_host_on_the_lan():
    rule = h.validate_access(
        {
            "rules": [
                {
                    "interface": "vlan50",
                    "internet": True,
                    "destinations": [{"network": "192.168.178.3", "ports": ["443/tcp"]}],
                }
            ]
        },
        ACCESS_VLANS,
    )["rules"][0]
    xml = h.render_policy(
        "vlan50", "lx-vlan50", "FedoraServer", rule, rule["destinations"], internet=True, nat=True
    )
    assert '<policy target="ACCEPT">' in xml and "<masquerade/>" in xml
    assert '<ingress-zone name="lx-vlan50"/>' in xml and '<egress-zone name="FedoraServer"/>' in xml
    # The allowed host first (priority -100), then private networks closed (priority 0), then the rest.
    host = xml.index('<destination address="192.168.178.3/32"/>')
    assert '<port port="443" protocol="tcp"/>' in xml[host:]
    assert xml.index('priority="-100"') < xml.index('<destination address="192.168.0.0/16"/>')
    assert xml.count("<reject/>") == len(h.PRIVATE4)
    closed = h.render_policy("vlan50", "lx-vlan50", "lx-vlan60", rule, [], internet=False, nat=False)
    assert '<policy target="CONTINUE">' in closed and "masquerade" not in closed
    assert len(h.policy_name("a-very-long-name", "FedoraServer")) <= 17
    assert h.policy_name("vlan50", "a") != h.policy_name("vlan50", "b")
    zone = h.render_zone(ACCESS_VLANS["vlan50"], ["dhcp", "dns"], ports=["22/tcp", "8000-8100/tcp"])
    assert '<port port="22" protocol="tcp"/>' in zone and '<port port="8000-8100" protocol="tcp"/>' in zone


def test_ufw_rules_keep_their_order():
    rule = h.validate_access(
        {
            "rules": [
                {
                    "interface": "vlan50",
                    "internet": True,
                    "destinations": [{"network": "192.168.178.3", "ports": ["443/tcp", "8000-8100/tcp"]}],
                    "services": ["22/tcp"],
                }
            ]
        },
        ACCESS_VLANS,
    )["rules"][0]
    rules = h.ufw_access_rules(rule, True, [(rule["destinations"][0], "eth0")], "eth0")
    mark = "comment 'lanvex:vlan50'"
    assert rules[:3] == h.ufw_wanted_rules("vlan50")
    assert rules[3] == "ufw allow in on vlan50 to any port 22 proto tcp " + mark
    assert rules[4] == "ufw route allow in on vlan50 out on eth0 to 192.168.178.3 port 443 proto tcp " + mark
    assert (
        rules[5]
        == "ufw route allow in on vlan50 out on eth0 to 192.168.178.3 port 8000:8100 proto tcp " + mark
    )
    # Allowed hosts before private networks are closed, and only then everything else.
    assert rules[6:11] == [f"ufw route deny in on vlan50 out on eth0 to {n} {mark}" for n in h.PRIVATE4]
    assert rules[11] == "ufw route allow in on vlan50 out on eth0 " + mark


def test_lanvex_nftables_table():
    access = {
        "rules": [
            {
                "interface": "vlan50",
                "internet": True,
                "destinations": [
                    {"network": "192.168.60.0/24"},
                    {"network": "172.30.0.13", "ports": ["22/tcp"]},
                ],
                "services": ["22/tcp", "123/udp"],
            }
        ]
    }
    rules = h.validate_access(access, ACCESS_VLANS)["rules"] + [h.default_rule("vlan60")]
    d60, d13 = rules[0]["destinations"][1], rules[0]["destinations"][0]
    pairs = {"vlan50": [(d60, "vlan60"), (d13, "eth0")], "vlan60": []}
    text = h.render_nft(rules, pairs, "eth0", {"vlan50"}, ["vlan50", "vlan60"], filtering=True)
    assert text.startswith("# " + h.MANAGED + "\ntable inet lanvex\ndelete table inet lanvex\n")
    assert '    iifname "vlan50" jump in_vlan50\n' in text
    assert "    udp dport { 53, 67 } accept\n    tcp dport 53 accept\n    tcp dport { 22 } accept\n" in text
    assert "    udp dport { 123 } accept\n    drop\n" in text
    assert '    oifname "vlan60" ip daddr 192.168.60.0/24 accept\n' in text
    assert '    oifname "eth0" ip daddr 172.30.0.13/32 tcp dport { 22 } accept\n' in text
    assert '    ip daddr @private4 drop\n    oifname "eth0" accept\n    drop\n' in text
    assert '    oifname { "vlan50", "vlan60" } drop\n' in text  # nothing new into Lanvex's networks otherwise
    assert '    iifname "vlan50" oifname "eth0" masquerade\n' in text  # not towards vlan60
    assert 'vlan60" oifname' not in text
    # Next to ufw: masquerading only.
    nat_only = h.render_nft(rules, pairs, "eth0", {"vlan50"}, ["vlan50", "vlan60"], filtering=False)
    assert "hook input" not in nat_only and "hook forward" not in nat_only and "masquerade" in nat_only


def test_dns_filter_rules_and_files():
    rule = h.validate_access(
        {
            "rules": [
                {
                    "interface": "vlan50",
                    "dns_filter": {
                        "block": ["TikTok.com", "*.instagram.com", "tiktok.com."],
                        "allow": ["edu.tiktok.com"],
                    },
                }
            ]
        },
        ACCESS_VLANS,
    )["rules"][0]
    assert rule["dns_filter"] == {"block": ["instagram.com", "tiktok.com"], "allow": ["edu.tiktok.com"]}
    with pytest.raises(h.Refused, match="not a domain"):
        h.validate_access(
            {"rules": [{"interface": "vlan50", "dns_filter": {"block": ["no spaces.com"]}}]}, ACCESS_VLANS
        )
    # One address per filtered network on Lanvex's link, in the order of their names.
    rules = [
        rule,
        h.default_rule("vlan60") | {"dns_filter": {"block": [], "allow": []}},
        h.default_rule("vlan70"),
    ]
    assert h.dns_addresses(rules) == {"vlan50": "10.99.53.1", "vlan60": "10.99.53.2"}

    conf = h.render_dns_filter("vlan50", "10.99.53.1", "192.168.50.1", rule["dns_filter"])
    assert "\nlisten-address=10.99.53.1\nport=53\n" in conf and "\nbind-interfaces\n" in conf
    assert "\nserver=192.168.50.1\n" in conf and "\nno-dhcp-interface=\n" in conf
    assert "\nserver=/edu.tiktok.com/192.168.50.1\n" in conf
    assert "\naddress=/instagram.com/\naddress=/tiktok.com/\n" in conf
    unit = h.render_dns_unit()
    assert (
        "address replace ${LANVEX_DNS_ADDRESS}/32 dev lxdns" in unit
        and "EnvironmentFile=/etc/lanvex/dns/%i.env" in unit
    )

    pools = h.validate_networks(networks(pools=[TEST_POOL]))["dhcp"]["pools"]
    main = h.render_dnsmasq(pools, True, True, {"vlan50": "10.99.53.1"})
    assert "\nexcept-interface=lxdns\n" in main  # the main dnsmasq leaves the filters' addresses alone
    assert "dhcp-option=tag:lx-vlan50,option:dns-server,10.99.53.1\n" in main  # devices ask the filter
    assert "except-interface" not in h.render_dnsmasq(pools, True, True)


def test_taking_over_netzverwaltung_needs_all_its_networks_and_no_policy_naming_its_zones(
    tmp_path, monkeypatch
):  # type: ignore[no-untyped-def]
    nv, dnsmasq, zones, policies = (tmp_path / p for p in ("netz.yaml", "dnsmasq.d", "zones", "policies"))
    for d in (dnsmasq, zones, policies):
        d.mkdir()
    monkeypatch.setattr(h, "NV_STATE", str(nv))
    monkeypatch.setattr(h, "DNSMASQ_DIR", str(dnsmasq))
    monkeypatch.setattr(h, "ZONE_DIR", str(zones))
    monkeypatch.setattr(h, "POLICY_DIR", str(policies))
    monkeypatch.setattr(h, "nm_running", lambda: False)
    monkeypatch.setattr(h, "service_state", lambda unit: {"active": True, "enabled": True})
    wanted = {"gast": {}, "bw": {}}

    with pytest.raises(h.Refused, match="not installed"):
        h.nv_takeover(wanted, "networkmanager")
    nv.write_text("vlans:\n  - {name: gast, vlan_id: 50}\n  - {name: bw, vlan_id: 60}\n")
    (dnsmasq / "nv-vlans.conf").write_text("interface=gast\n")
    (dnsmasq / "switch-mgmt.conf").write_text("dhcp-host=aa:bb:cc:00:00:09,192.168.178.3\n")  # hand-kept
    (zones / "gast.xml").write_text("<zone/>")
    (zones / "public.xml").write_text("<zone/>")
    (policies / "nv-gast-egress.xml").write_text('<policy><ingress-zone name="gast"/></policy>')

    with pytest.raises(h.Refused, match="lacks bw"):
        h.nv_takeover({"gast": {}}, "networkmanager")
    with pytest.raises(h.Refused, match="NetworkManager profiles; this server uses netplan"):
        h.nv_takeover(wanted, "netplan")
    found = h.nv_takeover(wanted, "networkmanager")
    assert found["names"] == ["bw", "gast"]
    assert found["files"] == [
        str(dnsmasq / "nv-vlans.conf"),
        str(zones / "gast.xml"),
        str(policies / "nv-gast-egress.xml"),
    ]  # its hand-kept neighbours stay
    assert found["units"] == {u: {"active": True, "enabled": True} for u in h.NV_UNITS}

    # A policy kept by hand that names one of its zones would stop working once the zone is gone.
    (policies / "lan-to-switch.xml").write_text(
        '<policy><ingress-zone name="public"/><egress-zone name="internal"/></policy>'
    )
    assert h.nv_takeover(wanted, "networkmanager")["names"] == ["bw", "gast"]
    (policies / "gast-to-printer.xml").write_text(
        '<policy><ingress-zone name="gast"/><egress-zone name="public"/></policy>'
    )
    with pytest.raises(h.Refused, match="gast-to-printer refers to zone gast.*use lx-gast"):
        h.nv_takeover(wanted, "networkmanager")


# ---- IPv6 and static routes (helper 6) --------------------------------------------------------------------


def v6(method="static", addresses=("2001:db8:1::5/64",), gateway="2001:db8:1::1"):
    return {"method": method, "addresses": list(addresses), "gateway": gateway}


def test_validate_ipv6_and_routes():
    req = {
        "interface": "eth0",
        "ipv4": {"method": "static", "addresses": ["10.0.0.5/24"], "gateway": "10.0.0.1"},
        "ipv6": {"method": "static", "addresses": ["2001:DB8:1:0::5/64"], "gateway": "fe80::1"},
        "routes": [
            {"to": "10.50.0.0/16", "via": "10.0.0.254", "metric": 50},
            {"to": "2001:db8:99::/48", "via": "2001:db8:1::fe"},
            {"to": "192.0.2.7/32", "via": "10.0.0.254"},
        ],
    }
    out = h.validate_change(req)
    assert out["ipv6"] == {"method": "static", "addresses": ["2001:db8:1::5/64"], "gateway": "fe80::1"}
    assert out["routes"] == [
        {"to": "10.50.0.0/16", "via": "10.0.0.254", "metric": 50},
        {"to": "2001:db8:99::/48", "via": "2001:db8:1::fe", "metric": None},
        {"to": "192.0.2.7/32", "via": "10.0.0.254", "metric": None},
    ]
    auto = h.validate_change(
        {"interface": "eth0", "ipv4": {"method": "dhcp"}, "ipv6": {"method": "auto"}, "routes": []}
    )
    assert auto["ipv6"] == {"method": "auto", "addresses": [], "gateway": None} and auto["routes"] == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"ipv6": {"method": "manual"}}, "ipv6.method must be"),
        ({"ipv6": v6(addresses=[])}, "at least one address"),
        ({"ipv6": v6(addresses=["10.0.0.9/24"])}, "not an IPv6 address"),
        ({"ipv6": v6(addresses=["fe80::5/64"])}, "cannot be assigned"),
        ({"ipv6": v6(gateway="2001:db8:2::1")}, "neither link-local nor inside"),
        ({"ipv6": v6(gateway="2001:db8:1::5")}, "own addresses"),
        ({"ipv6": v6(method="auto")}, "only be set with the static method"),
        ({"routes": [{"to": "0.0.0.0/0", "via": "10.0.0.1"}]}, "set as the gateway"),
        ({"routes": [{"to": "10.50.0.1/16", "via": "10.0.0.1"}]}, "not a network"),
        ({"routes": [{"to": "10.50.0.0/16", "via": "10.9.0.1"}]}, "not on any of the interface's networks"),
        ({"routes": [{"to": "10.50.0.0/16", "via": "2001:db8:1::1"}]}, "not the same family"),
        ({"routes": [{"to": "224.0.0.0/4", "via": "10.0.0.1"}]}, "cannot be routed"),
        ({"routes": [{"to": "10.50.0.0/16", "via": "10.0.0.1", "metric": -1}]}, "whole number"),
        ({"routes": [{"to": "10.50.0.0/16", "via": "10.0.0.1"}] * 2}, "listed twice"),
        ({"routes": "10.50.0.0/16"}, "must be a list"),
        (
            {"ipv6": {"method": "link-local"}, "routes": [{"to": "2001:db8:9::/48", "via": "2001:db8:1::1"}]},
            "not on any",
        ),
    ],
)
def test_validate_ipv6_and_routes_rejects(extra, message):
    req = {"interface": "eth0", "ipv4": {"method": "static", "addresses": ["10.0.0.5/24"]}} | extra
    with pytest.raises(h.Refused) as e:
        h.validate_change(req)
    assert message in e.value.message


def test_routes_with_dhcp_are_not_checked_for_the_link():
    out = h.validate_change(
        {
            "interface": "eth0",
            "ipv4": {"method": "dhcp"},
            "routes": [{"to": "10.50.0.0/16", "via": "10.9.0.1"}],
        }
    )
    assert out["routes"][0]["via"] == "10.9.0.1"


def test_netplan_ipv6_static_and_routes():
    old = {
        "addresses": ["192.0.2.10/24", "2001:db8::10/64"],
        "dhcp6": True,
        "gateway6": "2001:db8::1",
        "routes": [
            {"to": "default", "via": "192.0.2.1"},
            {"to": "default", "via": "2001:db8::1"},
            {"to": "10.50.0.0/16", "via": "192.0.2.254"},
            {"to": "10.60.0.0/16", "via": "192.0.2.254", "table": 100},  # policy routing: not ours
            {"to": "10.70.0.0/16", "scope": "link"},  # no next hop: not ours
        ],
        "mtu": 9000,
    }
    c = change(
        addresses=["192.0.2.10/24"],
        gateway="192.0.2.1",
        dns_servers=None,
        dns_search=None,
        ipv6=v6(addresses=["2001:db8:1::5/64"], gateway="fe80::1"),
        routes=[
            {"to": "10.80.0.0/16", "via": "192.0.2.254", "metric": 20},
            {"to": "2001:db8:99::/48", "via": "fe80::fe", "metric": None},
        ],
    )
    new = h.edit_netplan_definition(old, c)
    assert new == {
        "addresses": ["192.0.2.10/24", "2001:db8:1::5/64"],
        "dhcp6": False,
        "routes": [
            {"to": "10.60.0.0/16", "via": "192.0.2.254", "table": 100},
            {"to": "10.70.0.0/16", "scope": "link"},
            {"to": "10.80.0.0/16", "via": "192.0.2.254", "metric": 20},
            {"to": "2001:db8:99::/48", "via": "fe80::fe"},
            {"to": "default", "via": "192.0.2.1"},
            {"to": "default", "via": "fe80::1"},
        ],
        "mtu": 9000,
        "accept-ra": False,
    }


def test_netplan_ipv6_auto_and_link_local_keep_ipv4_and_routes():
    old = {
        "addresses": ["192.0.2.10/24", "2001:db8::10/64"],
        "routes": [{"to": "10.50.0.0/16", "via": "192.0.2.254"}],
    }
    base = dict(addresses=["192.0.2.10/24"], gateway=None, dns_servers=None, dns_search=None)
    auto = h.edit_netplan_definition(
        old, change(**base, ipv6={"method": "auto", "addresses": [], "gateway": None})
    )
    assert auto == {
        "addresses": ["192.0.2.10/24"],
        "routes": [{"to": "10.50.0.0/16", "via": "192.0.2.254"}],
        "dhcp6": True,
        "accept-ra": True,
    }
    ll = h.edit_netplan_definition(
        old, change(**base, ipv6={"method": "link-local", "addresses": [], "gateway": None})
    )
    assert ll["addresses"] == ["192.0.2.10/24"] and ll["accept-ra"] is False and "dhcp6" not in ll
    # Neither set: IPv6 and routes exactly as they were.
    same = h.edit_netplan_definition(old, change(**base))
    assert same == old


def test_keyfile_ipv6_and_routes():
    c = change(
        addresses=["172.31.0.12/24"],
        gateway="172.31.0.1",
        dns_servers=None,
        dns_search=None,
        ipv6=v6(addresses=["2001:db8:1::5/64", "2001:db8:1::6/64"], gateway="2001:db8:1::1"),
        routes=[
            {"to": "10.60.0.0/16", "via": "172.31.0.254", "metric": None},
            {"to": "2001:db8:99::/48", "via": "2001:db8:1::fe", "metric": 10},
        ],
    )
    after = h.edit_keyfile(KEYFILE, c)
    ipv4 = after.split("[ipv4]")[1].split("[ipv6]")[0].strip().splitlines()
    assert ipv4 == [
        "# keep this comment",
        "method=manual",
        "address1=172.31.0.12/24",
        "gateway=172.31.0.1",
        "route1=10.60.0.0/16,172.31.0.254",
        "dns=172.31.0.53;",  # DNS not part of this change
    ]
    assert after.split("[ipv6]")[1].strip().splitlines() == [
        "method=manual",
        "address1=2001:db8:1::5/64",
        "address2=2001:db8:1::6/64",
        "gateway=2001:db8:1::1",
        "route1=2001:db8:99::/48,2001:db8:1::fe,10",
    ]
    assert h.nm_keyfile_routes(KEYFILE) == [{"to": "10.50.0.0/16", "via": "172.31.0.1"}]


def test_keyfile_routes_only_and_a_profile_without_ipv6():
    c = change(addresses=["172.31.0.12/24"], gateway=None, dns_servers=None, dns_search=None, routes=[])
    after = h.edit_keyfile(KEYFILE, c)
    assert "route1" not in after and after.endswith("[ipv6]\nmethod=disabled\n")  # IPv6 untouched
    bare = "[connection]\nid=x\ntype=ethernet\n"
    only_v4 = h.edit_keyfile(bare, change(gateway=None, dns_servers=None, dns_search=None, routes=[]))
    assert "[ipv6]" not in only_v4  # no section created for nothing
    with_v6 = h.edit_keyfile(
        bare,
        change(
            gateway=None,
            dns_servers=None,
            dns_search=None,
            ipv6={"method": "auto", "addresses": [], "gateway": None},
        ),
    )
    assert with_v6.endswith("[ipv6]\nmethod=auto\n")


def test_routes_check_compares_what_the_kernel_has(monkeypatch):
    tables = {
        4: [
            {"dst": "10.50.0.0/16", "gateway": "172.31.0.254", "metric": 20},
            {"dst": "192.0.2.7", "gateway": "172.31.0.254"},
            {"dst": "10.70.0.0/16", "gateway": "172.31.0.254"},
            {"dst": "172.31.0.0/24", "scope": "link"},
        ],
        6: [{"dst": "2001:db8:99::/48", "gateway": "2001:DB8:1::FE", "metric": 1024}],
    }
    monkeypatch.setattr(
        h,
        "interface_routes",
        lambda iface, fam: [
            {
                "to": str(
                    __import__("ipaddress").ip_network(
                        x["dst"] if "/" in x["dst"] else x["dst"] + ("/32" if fam == 4 else "/128")
                    )
                ),
                "via": str(__import__("ipaddress").ip_address(x["gateway"])) if x.get("gateway") else None,
                "metric": x.get("metric"),
            }
            for x in tables[fam]
        ],
    )
    wanted = [
        {"to": "10.50.0.0/16", "via": "172.31.0.254", "metric": 20},
        {"to": "192.0.2.7/32", "via": "172.31.0.254", "metric": None},
        {"to": "2001:db8:99::/48", "via": "2001:db8:1::fe", "metric": None},
    ]
    assert h.routes_check("eth1", wanted, []) == (True, "3 static routes in place.")
    ok, detail = h.routes_check(
        "eth1", [dict(wanted[0], metric=30)], [{"to": "10.70.0.0/16", "via": "172.31.0.254"}]
    )
    assert (
        not ok
        and detail == "Missing: 10.50.0.0/16 via 172.31.0.254. Still there: 10.70.0.0/16 via 172.31.0.254."
    )


# ---- bridges and bonds (kind "link") ------------------------------------------------------------------


def link_req(**kw):
    return {
        "action": "create",
        "name": "br1",
        "type": "bridge",
        "members": ["eth2"],
        "method": "static",
        "addresses": ["10.12.0.1/24"],
    } | kw


def test_validate_link():
    assert h.validate_link(link_req()) == {
        "action": "create",
        "name": "br1",
        "type": "bridge",
        "members": ["eth2"],
        "mode": None,
        "stp": False,
        "method": "static",
        "addresses": ["10.12.0.1/24"],
        "gateway": None,
    }
    bond = h.validate_link(
        link_req(name="bond0", type="bond", members=["eth2", "eth3"], method="dhcp", addresses=[])
    )
    assert bond["mode"] == "active-backup" and bond["stp"] is None and bond["method"] == "dhcp"
    assert h.validate_link({"action": "remove", "name": "br1"}) == {"action": "remove", "name": "br1"}


@pytest.mark.parametrize(
    ("req", "message"),
    [
        (link_req(action="replace"), "action must be"),
        (link_req(name="lanvex-x"), "cannot be the name"),
        (link_req(name="eth0.5"), "cannot be the name"),
        (link_req(type="team"), "type must be"),
        (link_req(members=[]), "between 1 and 8"),
        (link_req(members=["eth2", "eth2"]), "Each member once"),
        (link_req(members=["br1"]), "Each member once"),
        (link_req(type="bond", mode="fastest"), "Bond mode"),
        (link_req(addresses=[]), "at least one address"),
        (link_req(method="none"), "need the static method"),
        (link_req(gateway="10.13.0.1"), "not inside"),
        (link_req(members=["eth2; reboot"]), "Invalid interface"),
    ],
)
def test_validate_link_rejects(req, message):
    with pytest.raises(h.Refused) as e:
        h.validate_link(req)
    assert message in e.value.message


def test_link_netplan_file():
    change = h.validate_link(
        link_req(type="bond", name="bond0", members=["eth2", "eth3"], gateway="10.12.0.254")
    )
    text = h.render_link_netplan(change, {"eth2": "lan2"})  # eth2 is defined elsewhere as lan2
    assert text.startswith("# Managed by Lanvex.")
    assert yaml.safe_load(text) == {
        "network": {
            "version": 2,
            "ethernets": {"eth3": {}},
            "bonds": {
                "bond0": {
                    "interfaces": ["lan2", "eth3"],
                    "parameters": {"mode": "active-backup", "mii-monitor-interval": 100},
                    "addresses": ["10.12.0.1/24"],
                    "routes": [{"to": "default", "via": "10.12.0.254"}],
                }
            },
        }
    }


def test_link_keyfiles():
    change = h.validate_link(link_req(members=["eth2", "eth3"], stp=True))
    files = h.render_link_keyfiles(change, {"br1": "u-br1", "eth2": "u-2", "eth3": "u-3"})
    d = "/etc/NetworkManager/system-connections/"
    assert sorted(files) == [
        d + "lxlink-br1-eth2.nmconnection",
        d + "lxlink-br1-eth3.nmconnection",
        d + "lxlink-br1.nmconnection",
    ]
    br = files[d + "lxlink-br1.nmconnection"]
    assert "type=bridge\ninterface-name=br1" in br and "[bridge]\nstp=true" in br
    assert "[ipv4]\nmethod=manual\naddress1=10.12.0.1/24\n" in br and "[ipv6]\nmethod=disabled" in br
    port = files[d + "lxlink-br1-eth2.nmconnection"]
    assert (
        "id=lxlink-br1-eth2\nuuid=u-2\ntype=ethernet\ninterface-name=eth2\nmaster=br1\nslave-type=bridge"
        in port
    )
    # Never named like the VLAN profiles of the networks kind, which owns every lanvex-* profile.
    assert not any(os.path.basename(p).startswith("lanvex-") for p in files)


def test_link_plan_create_and_remove(tmp_path, monkeypatch):
    netplan = tmp_path / "netplan"
    netplan.mkdir()
    (netplan / "50-lab.yaml").write_text("network:\n  version: 2\n  ethernets:\n    eth2: {}\n")
    monkeypatch.setattr(h, "NETPLAN_DIR", str(netplan))
    monkeypatch.setattr(h, "LINKS_MANIFEST", str(tmp_path / "links.json"))
    monkeypatch.setattr(h, "link_members_free", lambda change: None)
    monkeypatch.setattr(h, "link_backend", lambda members: "netplan")
    monkeypatch.setattr(
        h.os.path, "exists", lambda p, real=os.path.exists: False if p.startswith("/sys/") else real(p)
    )
    plan = h.link_plan(h.validate_link(link_req()))
    path = str(netplan / "61-lxlink-br1.yaml")
    assert plan["backend"] == "netplan" and [f["path"] for f in plan["files"]] == [
        path,
        str(tmp_path / "links.json"),
    ]
    assert "eth2" not in yaml.safe_load(plan["_write"][path])["network"].get(
        "ethernets", {}
    )  # defined in 50-lab
    manifest = json.loads(plan["_write"][str(tmp_path / "links.json")])
    assert manifest["links"]["br1"] == {
        "type": "bridge",
        "members": ["eth2"],
        "backend": "netplan",
        "files": [path],
    }

    (tmp_path / "links.json").write_text(plan["_write"][str(tmp_path / "links.json")])
    (netplan / "61-lxlink-br1.yaml").write_text(plan["_write"][path])
    gone = h.link_plan({"action": "remove", "name": "br1"})
    assert (
        gone["_delete"] == [path] and json.loads(gone["_write"][str(tmp_path / "links.json")])["links"] == {}
    )
    assert [f["action"] for f in gone["files"]] == ["delete", "modify"]
    with pytest.raises(h.Refused, match="not created by Lanvex"):
        h.link_plan({"action": "remove", "name": "br9"})

    (netplan / "50-lab.yaml").write_text("network:\n  version: 2\n  ethernets:\n    eth2: {dhcp4: true}\n")
    (tmp_path / "links.json").write_text(h.render_links({}))
    with pytest.raises(h.Refused, match="IP settings of its own"):
        h.link_plan(h.validate_link(link_req()))


# ---- Kea ---------------------------------------------------------------------------------------------------

KEA_CONF = """// Kea as shipped, with Lanvex's networks included
{
  "Dhcp4": {
    # the interfaces Kea listens on
    "interfaces-config": { "interfaces": [ "*" ] },
    "control-socket": { "socket-type": "unix", "socket-name": "/run/kea/kea4-ctrl-socket" },
    /* a subnet of the administrator's own */
    "subnet4": [ { "id": 1, "subnet": "192.0.2.0/24", "pools": [ { "pool": "192.0.2.100 - 192.0.2.200" } ] } ],
    "shared-networks": <?include "/etc/kea/lanvex-shared-networks.json"?>,
    "option-data": [ { "name": "domain-name", "data": "a // b # c" } ]
  }
}
"""


def test_kea_comments_and_includes_are_read_past():
    doc = json.loads(h.strip_kea_comments(KEA_CONF))
    assert doc["Dhcp4"]["interfaces-config"]["interfaces"] == ["*"]
    assert doc["Dhcp4"]["shared-networks"] == []
    assert doc["Dhcp4"]["option-data"][0]["data"] == "a // b # c"  # comment characters inside strings stay


def test_kea_setup_and_which_dhcp_server(tmp_path, monkeypatch):
    conf = tmp_path / "kea-dhcp4.conf"
    conf.write_text(KEA_CONF)
    monkeypatch.setattr(h, "KEA_CONF", str(conf))
    monkeypatch.setattr(h, "KEA_FILE", str(tmp_path / "lanvex-shared-networks.json"))
    monkeypatch.setattr(h, "read_manifest", lambda: {})
    monkeypatch.setattr(h, "DNSMASQ_FILE", str(tmp_path / "lanvex.conf"))
    monkeypatch.setattr(h, "kea_unit", lambda: "kea-dhcp4.service")
    assert h.kea_setup() == {"unit": "kea-dhcp4.service", "included": True, "interfaces": ["*"]}
    assert h.dhcp_server() == "kea"
    conf.write_text(KEA_CONF.replace('<?include "/etc/kea/lanvex-shared-networks.json"?>', "[]"))
    assert h.kea_setup()["included"] is False and h.dhcp_server() == "dnsmasq"  # not set up for Lanvex
    monkeypatch.setattr(h, "kea_unit", lambda: None)
    conf.write_text(KEA_CONF)
    assert h.dhcp_server() == "dnsmasq"  # included, but Kea is not installed
    monkeypatch.setattr(h, "kea_unit", lambda: "kea-dhcp4.service")
    (tmp_path / "lanvex.conf").write_text("interface=vlan50\n")
    assert h.dhcp_server() == "dnsmasq"  # Lanvex's pools are in dnsmasq already: not served twice


def test_kea_networks():
    pool = {
        "interface": "vlan50",
        "network": "192.168.50.0/24",
        "start": "192.168.50.100",
        "end": "192.168.50.199",
        "lease": "12h",
        "router": "192.168.50.1",
        "dns": ["192.168.50.1", "9.9.9.9"],
        "reservations": [
            {"mac": "02:00:00:00:00:05", "ip": "192.168.50.5", "name": "nas"},
            {"mac": "02:00:00:00:00:06", "ip": "192.168.50.6", "name": None},
        ],
    }
    nets = json.loads(h.render_kea([pool]))
    assert nets == [
        {
            "name": "lanvex-vlan50",
            "user-context": {"lanvex": h.MANAGED, "interface": "vlan50"},
            "subnet4": [
                {
                    "id": h.kea_subnet_id("vlan50"),
                    "subnet": "192.168.50.0/24",
                    "pools": [{"pool": "192.168.50.100 - 192.168.50.199"}],
                    "valid-lifetime": 43200,
                    "option-data": [
                        {"name": "routers", "data": "192.168.50.1"},
                        {"name": "domain-name-servers", "data": "192.168.50.1, 9.9.9.9"},
                    ],
                    "reservations": [
                        {"hw-address": "02:00:00:00:00:05", "ip-address": "192.168.50.5", "hostname": "nas"},
                        {"hw-address": "02:00:00:00:00:06", "ip-address": "192.168.50.6"},
                    ],
                }
            ],
        }
    ]
    assert h.kea_subnet_id("vlan50") == h.kea_subnet_id("vlan50") >= 2**30  # stable, far from 1, 2, 3…
    assert h.kea_subnet_id("vlan50") != h.kea_subnet_id("vlan60")
    assert h.kea_pools(h.render_kea([pool])) == {"vlan50"} and h.kea_pools("[]\n") == set()
    assert [h.lease_seconds(x) for x in ("30m", "2d", "infinite")] == [1800, 172800, 4294967295]
