"""VLAN interfaces, firewalld zones and DHCP on a REAL Fedora host (lab-fedora in deploy/lab), with a
real DHCP client on another host. Skipped unless LANVEX_TEST_LAB=1 and lab-fedora runs (it is built
from Fedora's image, so only where a registry is reachable: CI).

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

HOST = "lab-fedora"
CLIENT = "lab-nm"  # Ubuntu with NetworkManager: a device on the new VLAN
HELPER = "/usr/local/libexec/lanvex/lanvex-netapply"


def running(host: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", host], capture_output=True, text=True
    )
    return r.stdout.strip() == "true"


@pytest.fixture(scope="module", autouse=True)
def fedora() -> None:
    if not running(HOST):
        pytest.skip("lab-fedora is not running (build the lab with LANVEX_LAB_FEDORA=1)")
    subprocess.run(["docker", "cp", str(helper.HELPER_FILE), f"{HOST}:/tmp/lanvex-netapply"], check=True)
    sh(HOST, f"install -D -m 0755 -o root -g root /tmp/lanvex-netapply {HELPER}")


@pytest.fixture(autouse=True)
def clean() -> None:
    reset()
    yield
    reset()


def reset() -> None:
    sh(
        HOST,
        "systemctl stop 'lanvex-rollback-*.timer' 2>/dev/null; rm -rf /var/lib/lanvex/changes; "
        "rm -f /etc/NetworkManager/system-connections/lanvex-* /etc/NetworkManager/system-connections/other-* "
        "/etc/firewalld/zones/lx-* /etc/dnsmasq.d/lanvex.conf /etc/dnsmasq.d/nv-vlans.conf; "
        "nmcli connection reload; firewall-cmd --reload >/dev/null; "
        "systemctl disable --now dnsmasq 2>/dev/null; for l in vlan50 vlan60; do ip link del $l 2>/dev/null; done; true",
    )
    sh(CLIENT, "nmcli connection delete client50 >/dev/null 2>&1; ip link del eth0.50 2>/dev/null; true")


def call(command: str, payload: dict[str, Any] | None = None, check: bool = True) -> dict[str, Any]:
    r = subprocess.run(
        ["docker", "exec", "-i", HOST, HELPER, command],
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


def apply(request: dict[str, Any], seconds: int = 120) -> tuple[str, dict[str, Any]]:
    plan = call("plan", request)
    cid = uuid.uuid4().hex
    expect = {"backend": plan["backend"], "files": {f["path"]: f["sha256"] for f in plan["files"]}}
    result = call("apply", request | {"id": cid, "rollback_seconds": seconds, "expect": expect})
    assert result["state"] == "applied"
    return cid, plan


def links() -> set[str]:
    return set(sh(HOST, "ls /sys/class/net").split())


def test_a_new_vlan_with_dhcp_serves_a_real_client() -> None:
    mac = client_mac()
    request = networks([pool(reservations=[{"mac": mac, "ip": "192.168.50.20", "name": "lab-nm"}])])
    plan = call("plan", request)
    by_path = {f["path"]: f for f in plan["files"]}
    assert by_path["/etc/NetworkManager/system-connections/lanvex-vlan50.nmconnection"]["action"] == "create"
    assert by_path["/etc/firewalld/zones/lx-vlan50.xml"]["action"] == "create"
    assert (
        f"dhcp-host={mac},set:lx-vlan50,192.168.50.20,lab-nm"
        in by_path["/etc/dnsmasq.d/lanvex.conf"]["after"]
    )
    assert plan["commands"] == [
        "firewall-cmd --reload",
        "nmcli connection reload",
        "nmcli connection up id lanvex-vlan50",
        "systemctl enable dnsmasq",
        "systemctl restart dnsmasq",
    ]
    assert "vlan50" not in links()  # planning changes nothing

    cid, _ = apply(request)
    result = call("verify", {"id": cid})
    assert result["passed"], result["checks"]
    assert {c["id"] for c in result["checks"]} == {"vlan:vlan50", "zone:vlan50", "dhcp"}
    assert sh(HOST, "firewall-cmd --zone=lx-vlan50 --list-services").split() == ["dhcp", "dns"]

    # A device on VLAN 50 gets its fixed address from the server, through firewalld.
    sh(
        CLIENT,
        "nmcli connection add type vlan con-name client50 ifname eth0.50 dev eth0 id 50 "
        "ipv4.method auto ipv6.method disabled >/dev/null && nmcli --wait 60 connection up client50 >/dev/null",
    )
    assert "192.168.50.20/24" in sh(CLIENT, "ip -4 -o address show dev eth0.50")
    assert call("confirm", {"id": cid})["state"] == "confirmed"
    assert sh(HOST, "systemctl is-enabled dnsmasq").strip() == "enabled"

    # Nothing left to do: the same request again is not a change.
    again = call("plan", request, check=False)
    assert again["ok"] is False and again["error"]["code"] == "no_change"


def test_removing_a_vlan_and_rolling_it_back() -> None:
    cid, _ = apply(networks([pool()]))
    call("confirm", {"id": cid})
    removal = networks([], vlans=[])
    plan = call("plan", removal)
    assert {f["action"] for f in plan["files"]} == {"delete"}
    assert any("disconnects every device" in w for w in plan["warnings"])

    cid, _ = apply(removal)
    assert "vlan50" not in links()
    assert call("verify", {"id": cid})["passed"]
    assert call("rollback", {"id": cid})["state"] == "rolled_back"
    # Profile, zone and DHCP are back exactly as they were.
    assert "vlan50" in links()
    assert "192.168.50.1/24" in sh(HOST, "ip -4 -o address show dev vlan50")
    assert sh(HOST, "firewall-cmd --get-zone-of-interface=vlan50").strip() == "lx-vlan50"
    assert sh(HOST, "systemctl is-active dnsmasq").strip() == "active"


def test_the_servers_timer_restores_an_unconfirmed_change() -> None:
    cid, _ = apply(networks([pool()]), seconds=30)
    assert "vlan50" in links()
    deadline = time.monotonic() + 90
    while call("status", {"id": cid})["state"] != "rolled_back" and time.monotonic() < deadline:
        time.sleep(3)
    state = call("status", {"id": cid})
    assert state["state"] == "rolled_back" and "No confirmation" in state["reason"]
    assert "vlan50" not in links()
    assert sh(HOST, "ls /etc/firewalld/zones/").count("lx-") == 0
    assert sh(HOST, "systemctl is-enabled dnsmasq || true").strip() == "disabled"  # as it was before


def test_another_tools_vlan_and_dhcp_are_never_changed() -> None:
    # Like Netzverwaltung on KlassenServer: its own NetworkManager profile and dnsmasq file.
    sh(
        HOST,
        "nmcli connection add type vlan con-name other-vlan60 ifname vlan60 dev eth0 id 60 "
        "ipv4.method manual ipv4.addresses 192.168.60.1/24 >/dev/null",
    )
    vlan60 = [{"name": "vlan60", "parent": "eth0", "id": 60, "addresses": ["192.168.60.1/24"]}]
    refused = call("plan", networks(None, vlans=vlan60), check=False)
    assert refused["error"]["code"] == "conflict" and "'other-vlan60'" in refused["error"]["message"]

    sh(HOST, "printf 'interface=vlan50\\n' > /etc/dnsmasq.d/nv-vlans.conf")
    refused = call("plan", networks([pool()]), check=False)
    assert refused["error"]["code"] == "conflict" and "nv-vlans.conf" in refused["error"]["message"]
