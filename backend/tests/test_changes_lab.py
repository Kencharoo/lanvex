"""Configuration changes against REAL servers: the lab in deploy/lab. Skipped unless LANVEX_TEST_LAB=1.

Everything goes through the Lanvex API and the change runner, over real SSH, to the real helper on
Ubuntu 24.04 hosts managed by netplan/systemd-networkd and by NetworkManager. `docker exec` plays the
server administrator (installing the helper, checking results as root) and resets the lab between
tests. These tests change only lab containers.
"""

import hashlib
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from lanvex.changes.runner import ChangeRunner
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.core.ratelimit import MemoryRateLimiter
from lanvex.integrations.linux import helper
from lanvex.main import create_app

from .conftest import login
from .test_servers_lab import authorize, ok, register, sh

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(os.environ.get("LANVEX_TEST_LAB") != "1", reason="LANVEX_TEST_LAB=1 not set"),
]

LAB = Path(__file__).resolve().parents[2] / "deploy" / "lab"
HELPER = "/usr/local/libexec/lanvex/lanvex-netapply"


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def install_helper(host: str) -> None:
    """What the administrator does: install the helper as root and allow it for the lanvex user."""
    docker("cp", str(helper.HELPER_FILE), f"{host}:/tmp/lanvex-netapply")
    sh(
        host,
        f"install -D -m 0755 -o root -g root /tmp/lanvex-netapply {HELPER} && "
        f"echo 'lanvex ALL=(root) NOPASSWD: {HELPER}' > /etc/sudoers.d/lanvex && "
        "chmod 0440 /etc/sudoers.d/lanvex && visudo -cf /etc/sudoers.d/lanvex",
    )


def reset(host: str) -> None:
    """Put the lab host's network configuration back to what deploy/lab ships."""
    sh(host, "systemctl stop 'lanvex-rollback-*.timer' 2>/dev/null; rm -rf /var/lib/lanvex/changes; true")
    if host == "lab-netplan":
        docker("cp", str(LAB / "netplan-networkd" / "50-lab.yaml"), "lab-netplan:/etc/netplan/50-lab.yaml")
        sh(
            host,
            "chown root:root /etc/netplan/50-lab.yaml && chmod 600 /etc/netplan/50-lab.yaml && netplan apply",
        )
    else:
        for name in ("eth0.nmconnection", "eth1.nmconnection"):
            docker(
                "cp",
                str(LAB / "networkmanager" / name),
                f"lab-nm:/etc/NetworkManager/system-connections/{name}",
            )
        sh(
            host,
            "rm -f /etc/netplan/90-NM-*.yaml; chown root:root /etc/NetworkManager/system-connections/*; "
            "chmod 600 /etc/NetworkManager/system-connections/*; "
            "nmcli connection reload && nmcli connection up storage >/dev/null && nmcli device reapply eth0",
        )


def ipv4(host: str, iface: str) -> list[str]:
    out = sh(host, f"ip -4 -o address show dev {iface}")
    return sorted(line.split()[3] for line in out.splitlines())


def default_via(host: str, iface: str) -> str:
    return sh(host, f"ip -4 route show default dev {iface}").strip()


def sha(host: str, path: str) -> str:
    return sh(host, f"sha256sum {path}").split()[0]


@pytest.fixture
async def lab(
    database: Database, users: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, ChangeRunner]]:
    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    app = create_app(get_settings())
    app.state.db, app.state.redis, app.state.rate_limiter = database, None, MemoryRateLimiter()
    app.state.netbox = None
    runner = ChangeRunner(database, get_settings())
    app.state.change_runner = runner
    for host in ("lab-netplan", "lab-nm"):
        reset(host)
        install_helper(host)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://lanvex.test", timeout=300
    ) as c:
        await login(c, "admin", users["admin"])
        yield c, runner
    for host in ("lab-netplan", "lab-nm"):
        reset(host)


async def ready(api: httpx.AsyncClient, host: str) -> dict[str, Any]:
    s = await register(api, host)
    authorize(host, s["authorized_keys_line"])
    ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))
    assert ok(await api.post(f"/api/v1/servers/{s['id']}/changes/helper/check"))["helper"]["state"] == "ok"
    r = ok(await api.put(f"/api/v1/servers/{s['id']}/changes/enabled", json={"enabled": True}))
    assert r["ready"] is True, r["blockers"]
    return s


async def run(
    api: httpx.AsyncClient, runner: ChangeRunner, s: dict[str, Any], change: dict[str, Any]
) -> dict[str, Any]:
    c = ok(await api.post(f"/api/v1/servers/{s['id']}/changes", json=change), 201)
    ok(await api.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": s["name"]}))
    assert await runner.run_once()
    return ok(await api.get(f"/api/v1/changes/{c['id']}"))


def change(
    iface: str,
    addresses: list[str],
    gateway: str | None = None,
    dns: list[str] | None = None,
    search: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "interface": iface,
        "method": "static",
        "addresses": addresses,
        "gateway": gateway,
        "dns_servers": dns or [],
        "dns_search": search or [],
    }


async def test_netplan_secondary_address_is_applied_verified_and_confirmed(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    s = await ready(api, "lab-netplan")
    c = ok(
        await api.post(
            f"/api/v1/servers/{s['id']}/changes", json=change("br0", ["10.11.0.1/24", "10.11.0.2/24"])
        ),
        201,
    )
    diff = [
        line
        for line in c["preview"]["files"][0]["diff"].splitlines()
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert diff == ["-      addresses: [10.11.0.1/24]", "+      addresses: [10.11.0.1/24, 10.11.0.2/24]"]
    assert ipv4("lab-netplan", "br0") == ["10.11.0.1/24"]  # preparing changed nothing
    ok(await api.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": s["name"]}))
    assert await runner.run_once()
    done = ok(await api.get(f"/api/v1/changes/{c['id']}"))
    assert done["status"] == "succeeded", done
    assert all(ch["ok"] is not False for ch in done["checks"]) and done["checks"][0]["id"] == "lanvex"
    assert ipv4("lab-netplan", "br0") == ["10.11.0.1/24", "10.11.0.2/24"]
    assert "addresses: [10.11.0.1/24, 10.11.0.2/24]" in sh("lab-netplan", "cat /etc/netplan/50-lab.yaml")
    assert sh("lab-netplan", "stat -c '%a %U' /etc/netplan/50-lab.yaml").strip() == "600 root"
    assert (
        sh("lab-netplan", "systemctl list-timers --all --no-legend 'lanvex-rollback-*' | wc -l").strip()
        == "0"
    )
    # Lanvex read the server again: the new address is what the server page shows now.
    snap = ok(await api.get(f"/api/v1/servers/{s['id']}/snapshots/latest"))
    br0 = next(i for i in snap["facts"]["interfaces"] if i["name"] == "br0")
    assert sorted(a["address"] for a in br0["addresses"] if a["family"] == 4) == [
        "10.11.0.1/24",
        "10.11.0.2/24",
    ]

    back = await run(api, runner, s, change("br0", ["10.11.0.1/24"]))
    assert back["status"] == "succeeded" and ipv4("lab-netplan", "br0") == ["10.11.0.1/24"]


async def test_networkmanager_bad_gateway_fails_verification_and_is_rolled_back(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    s = await ready(api, "lab-nm")
    keyfile = "/etc/NetworkManager/system-connections/eth1.nmconnection"
    original = sha("lab-nm", keyfile)
    done = await run(
        api, runner, s, change("eth1", ["172.31.0.12/24", "172.31.0.13/24"], "172.31.0.99", ["172.31.0.1"])
    )
    assert done["status"] == "rolled_back", done
    failed = [ch["label"] for ch in done["checks"] if ch["ok"] is False]
    assert failed == ["Gateway 172.31.0.99 answers"]
    assert done["outcome"].startswith("Verification failed (Gateway 172.31.0.99 answers).")
    assert ipv4("lab-nm", "eth1") == ["172.31.0.12/24"] and default_via("lab-nm", "eth1") == ""
    assert sha("lab-nm", keyfile) == original  # the profile is byte for byte what it was


async def test_networkmanager_dns_change_on_the_management_interface(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    s = await ready(api, "lab-nm")
    c = ok(
        await api.post(
            f"/api/v1/servers/{s['id']}/changes",
            json=change(
                "eth0", ["172.30.0.12/24"], "172.30.0.1", ["172.30.0.1"], ["lab.example.net", "example.org"]
            ),
        ),
        201,
    )
    assert c["preview"]["warnings"][0].startswith("Lanvex reaches")
    diff = [
        line
        for line in c["preview"]["files"][0]["diff"].splitlines()
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert "+dns=172.30.0.1;" in diff and "+dns-search=lab.example.net;example.org;" in diff
    ok(await api.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": s["name"]}))
    assert await runner.run_once()
    done = ok(await api.get(f"/api/v1/changes/{c['id']}"))
    assert done["status"] == "succeeded", done
    assert sh("lab-nm", "nmcli -g IP4.DNS device show eth0").split() == ["172.30.0.1"]
    assert ipv4("lab-nm", "eth0") == ["172.30.0.12/24"]


async def test_a_gateway_that_drops_ping_is_accepted_when_it_answers_arp(lab) -> None:  # type: ignore[no-untyped-def]
    """Many firewalls and routers do not answer ping. The gateway must still be there on the link."""
    api, runner = lab
    s = await ready(api, "lab-nm")
    # lab-ifup plays a gateway that ignores ping: same subnet as lab-nm's eth1, answers ARP only.
    docker("network", "connect", "--ip", "172.31.0.20", "lanvex-lab-b", "lab-ifup")
    try:
        sh("lab-ifup", "sysctl -qw net.ipv4.icmp_echo_ignore_all=1")
        done = await run(api, runner, s, change("eth1", ["172.31.0.12/24"], "172.31.0.20", ["172.31.0.1"]))
        assert done["status"] == "succeeded", done
        gw = next(ch for ch in done["checks"] if ch["id"] == "gateway")
        assert gw["ok"] is True and gw["detail"].startswith(
            "Does not answer ping, but is reachable on the link ("
        )
        assert "172.31.0.20" in default_via("lab-nm", "eth1")
        back = await run(api, runner, s, change("eth1", ["172.31.0.12/24"], None, ["172.31.0.1"]))
        assert back["status"] == "succeeded", back
        assert default_via("lab-nm", "eth1") == ""
    finally:
        sh("lab-ifup", "sysctl -qw net.ipv4.icmp_echo_ignore_all=0")
        docker("network", "disconnect", "lanvex-lab-b", "lab-ifup")


async def test_lost_connection_is_not_confirmed_and_the_server_restores_itself(
    lab, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """A /32 mask keeps Lanvex's address but removes the route back to Lanvex: the session dies."""
    api, runner = lab
    monkeypatch.setattr(get_settings(), "change_rollback_seconds", 30)
    s = await ready(api, "lab-netplan")
    done = await run(
        api,
        runner,
        s,
        change("eth0", ["172.30.0.11/32"], None, ["172.30.0.1", "9.9.9.9"], ["lab.example.net"]),
    )
    assert done["status"] == "rolled_back", done
    assert done["outcome"].startswith("Lanvex could not verify the change, so it did not confirm it.")
    assert "No confirmation from Lanvex before the deadline" in done["outcome"]
    assert done["checks"][0]["ok"] is False
    assert ipv4("lab-netplan", "eth0") == ["172.30.0.11/24"]
    assert "via 172.30.0.1" in default_via("lab-netplan", "eth0")


async def test_a_file_edited_after_the_preview_is_not_overwritten(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    s = await ready(api, "lab-netplan")
    c = ok(
        await api.post(
            f"/api/v1/servers/{s['id']}/changes", json=change("br0", ["10.11.0.1/24", "10.11.0.9/24"])
        ),
        201,
    )
    sh("lab-netplan", "echo '# edited by hand' >> /etc/netplan/50-lab.yaml")
    edited = sha("lab-netplan", "/etc/netplan/50-lab.yaml")
    ok(await api.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": s["name"]}))
    assert await runner.run_once()
    done = ok(await api.get(f"/api/v1/changes/{c['id']}"))
    assert done["status"] == "failed" and "changed after the change was prepared" in done["outcome"]
    assert sha("lab-netplan", "/etc/netplan/50-lab.yaml") == edited
    assert ipv4("lab-netplan", "br0") == ["10.11.0.1/24"]


async def test_hosts_that_cannot_take_changes_say_why(lab) -> None:  # type: ignore[no-untyped-def]
    api, _ = lab
    s = await register(api, "lab-ifup")
    authorize("lab-ifup", s["authorized_keys_line"])
    ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))
    r = ok(await api.post(f"/api/v1/servers/{s['id']}/changes/helper/check"))
    assert r["helper"]["state"] == "no_sudo"  # without the rule, sudo cannot tell whether the file exists
    assert "Install the helper and add the sudoers rule" in r["helper"]["message"]
    try:
        install_helper("lab-ifup")  # this host has no Python
        r = ok(await api.post(f"/api/v1/servers/{s['id']}/changes/helper/check"))
        assert r["helper"]["state"] == "no_python" and "Python 3" in r["helper"]["message"]
    finally:
        sh("lab-ifup", "rm -rf /usr/local/libexec/lanvex /etc/sudoers.d/lanvex")
    # ifupdown: networks (VLANs, DHCP) only, no interface settings.
    assert r["stack_supported"] is True and r["interface_changes"] is False

    # Uploading the helper puts the exact file in the user's home, with a checksum to verify.
    up = ok(await api.post(f"/api/v1/servers/{s['id']}/changes/helper/upload"))
    assert up["path"] == "/home/lanvex/lanvex-netapply"
    assert sha("lab-ifup", up["path"]) == hashlib.sha256(helper.source()).hexdigest() == up["sha256"]
    sh("lab-ifup", "rm -f /home/lanvex/lanvex-netapply")


async def test_static_routes_replaced_on_networkmanager_and_added_on_netplan(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    s = await ready(api, "lab-nm")
    body = change("eth1", ["172.31.0.12/24"]) | {"dns_servers": None, "dns_search": None}
    c = ok(
        await api.post(
            f"/api/v1/servers/{s['id']}/changes",
            json=body | {"routes": [{"to": "10.60.0.0/16", "via": "172.31.0.1", "metric": 30}]},
        ),
        201,
    )
    assert c["before"]["routes"] == [{"to": "10.50.0.0/16", "via": "172.31.0.1", "metric": 50}]
    assert "+route1=10.60.0.0/16,172.31.0.1,30" in c["preview"]["files"][0]["diff"]
    ok(await api.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": s["name"]}))
    assert await runner.run_once()
    done = ok(await api.get(f"/api/v1/changes/{c['id']}"))
    assert done["status"] == "succeeded", done
    assert next(ch for ch in done["checks"] if ch["id"] == "routes")["ok"] is True
    routes = sh("lab-nm", "ip -4 route show dev eth1")
    assert "10.60.0.0/16 via 172.31.0.1" in routes and "metric 30" in routes and "10.50.0.0/16" not in routes

    n = await ready(api, "lab-netplan")
    done = await run(
        api,
        runner,
        n,
        change("br0", ["10.11.0.1/24"])
        | {"dns_servers": None, "dns_search": None, "routes": [{"to": "10.77.0.0/16", "via": "10.11.0.254"}]},
    )
    assert done["status"] == "succeeded", done
    assert "to: 10.77.0.0/16" in sh("lab-netplan", "cat /etc/netplan/50-lab.yaml")
    assert "10.77.0.0/16 via 10.11.0.254" in sh("lab-netplan", "ip -4 route show dev br0")


async def test_ipv6_static_on_networkmanager(lab) -> None:  # type: ignore[no-untyped-def]
    if sh("lab-nm", "test -d /proc/sys/net/ipv6 && echo yes || echo no").strip() != "yes":
        pytest.skip("no IPv6 in this kernel")
    api, runner = lab
    s = await ready(api, "lab-nm")
    done = await run(
        api,
        runner,
        s,
        change("eth1", ["172.31.0.12/24"])
        | {
            "dns_servers": None,
            "dns_search": None,
            "ipv6": {"method": "static", "addresses": ["fd00:31::12/64"]},
            "routes": [{"to": "fd00:99::/48", "via": "fd00:31::1"}],
        },
    )
    assert done["status"] == "succeeded", done
    assert "fd00:31::12/64" in sh("lab-nm", "ip -6 -o address show dev eth1 scope global")
    assert "fd00:99::/48 via fd00:31::1" in sh("lab-nm", "ip -6 route show dev eth1")
    assert {ch["id"] for ch in done["checks"]} >= {"ipv6_addresses", "ipv6_default_route", "routes"}


def spare_links(host: str) -> None:
    """Two interfaces without addresses, as a server's unused network ports would be."""
    sh(
        host,
        "for i in 0 1; do ip link del lxv$i 2>/dev/null; ip link add lxv$i type veth peer name lxp$i && "
        "ip link set lxv$i up && ip link set lxp$i up; done",
    )


def drop_links(host: str) -> None:
    sh(
        host,
        "for i in 0 1; do ip link del lxv$i 2>/dev/null; done; ip link del lxbr1 2>/dev/null; "
        "ip link del lxbond1 2>/dev/null; rm -f /etc/netplan/61-lxlink-* "
        "/etc/NetworkManager/system-connections/lxlink-* /etc/lanvex/links.json; "
        "nmcli connection reload 2>/dev/null; true",
    )


@pytest.mark.parametrize("host", ["lab-netplan", "lab-nm"])
async def test_a_bridge_created_and_removed(lab, host: str) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    drop_links(host)
    spare_links(host)
    try:
        s = await ready(api, host)
        made = await run(
            api,
            runner,
            s,
            {
                "kind": "link",
                "name": "lxbr1",
                "type": "bridge",
                "members": ["lxv0"],
                "method": "static",
                "addresses": ["10.12.0.1/24"],
            },
        )
        assert made["status"] == "succeeded", made
        assert sh(host, "readlink /sys/class/net/lxv0/master").strip().endswith("/lxbr1")
        assert ipv4(host, "lxbr1") == ["10.12.0.1/24"]
        links = ok(await api.get(f"/api/v1/servers/{s['id']}/changes/readiness"))["links"]
        assert [x["name"] for x in links] == ["lxbr1"]

        gone = await run(api, runner, s, {"kind": "link", "action": "remove", "name": "lxbr1"})
        assert gone["status"] == "succeeded", gone
        assert sh(host, "test -e /sys/class/net/lxbr1 && echo there || echo gone").strip() == "gone"
        assert sh(host, "test -e /sys/class/net/lxv0/master && echo held || echo free").strip() == "free"
    finally:
        drop_links(host)


async def test_a_bond_on_networkmanager(lab) -> None:  # type: ignore[no-untyped-def]
    api, runner = lab
    drop_links("lab-nm")
    spare_links("lab-nm")
    try:
        s = await ready(api, "lab-nm")
        done = await run(
            api,
            runner,
            s,
            {
                "kind": "link",
                "name": "lxbond1",
                "type": "bond",
                "members": ["lxv0", "lxv1"],
                "mode": "active-backup",
            },
        )
        assert done["status"] == "succeeded", done
        assert sh("lab-nm", "cat /sys/class/net/lxbond1/bonding/mode").split()[0] == "active-backup"
        assert sorted(sh("lab-nm", "cat /sys/class/net/lxbond1/bonding/slaves").split()) == ["lxv0", "lxv1"]
    finally:
        drop_links("lab-nm")
