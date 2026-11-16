"""Server management against REAL SSH servers: the lab in deploy/lab. Skipped unless LANVEX_TEST_LAB=1.

Uses `docker exec` to play the server administrator's part (installing the authorized key,
regenerating host keys). The comparison test also needs the live NetBox variables from
test_integration_netbox.py, and removes any NetBox IP records for the addresses in OWNED_ADDRESSES.
"""

import asyncio
import os
import secrets
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.core.ratelimit import MemoryRateLimiter
from lanvex.main import create_app
from lanvex.netbox.client import NetBoxClient

from .conftest import login

NB_URL = os.environ.get("LANVEX_TEST_NETBOX_URL", "")
NB_TOKEN = os.environ.get("LANVEX_TEST_NETBOX_TOKEN", "")  # the token Lanvex uses
NB_ADMIN = os.environ.get("LANVEX_TEST_NETBOX_ADMIN_TOKEN") or NB_TOKEN  # creates the test objects

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(os.environ.get("LANVEX_TEST_LAB") != "1", reason="LANVEX_TEST_LAB=1 not set"),
]

HOSTS = {"lab-netplan": "172.30.0.11", "lab-nm": "172.30.0.12", "lab-ifup": "172.30.0.13"}
OWNED_ADDRESSES = ("172.30.0.11", "172.30.0.50", "10.11.0.1", "172.30.0.60", "192.0.2.70")


def sh(host: str, script: str) -> str:
    return subprocess.run(
        ["docker", "exec", host, "sh", "-c", script], check=True, capture_output=True, text=True
    ).stdout


def server_fingerprint(host: str) -> str:
    return sh(host, "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub").split()[1]


def authorize(host: str, line: str) -> None:
    sh(
        host,
        f"printf '%s\\n' '{line}' > /home/lanvex/.ssh/authorized_keys && chown lanvex: /home/lanvex/.ssh/authorized_keys && chmod 600 /home/lanvex/.ssh/authorized_keys",
    )


@pytest.fixture
async def api(database: Database, users: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(get_settings())
    app.state.db, app.state.redis, app.state.rate_limiter = database, None, MemoryRateLimiter()
    app.state.netbox = NetBoxClient(NB_URL, NB_TOKEN) if NB_URL and NB_TOKEN else None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://lanvex.test", timeout=60
    ) as c:
        await login(c, "admin", users["admin"])
        yield c
    if app.state.netbox:
        await app.state.netbox.aclose()


def ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text}"
    return r.json() if r.content else None


async def register(
    api: httpx.AsyncClient, host: str, *, credentials: dict | None = None, username: str = "lanvex"
) -> dict:
    hk = ok(await api.post("/api/v1/servers/scan-host-key", json={"address": HOSTS[host]}))
    assert hk["fingerprint_sha256"] == server_fingerprint(
        host
    )  # what an administrator compares on the server
    created = ok(
        await api.post(
            "/api/v1/servers",
            json={
                "name": f"{host}-{secrets.token_hex(2)}",
                "address": HOSTS[host],
                "username": username,
                "host_key": {"public_key": hk["public_key"], "fingerprint_sha256": hk["fingerprint_sha256"]},
                "credentials": credentials or {"method": "generated_key"},
            },
        ),
        201,
    )
    return created["server"]


async def test_generated_key_flow_on_ifupdown_host(api: httpx.AsyncClient) -> None:
    s = await register(api, "lab-ifup")
    sid = s["id"]
    assert (
        ok(await api.post(f"/api/v1/servers/{sid}/test"))["status"] == "auth_failed"
    )  # key not installed yet
    authorize("lab-ifup", s["authorized_keys_line"])
    t = ok(await api.post(f"/api/v1/servers/{sid}/test"))
    assert t["status"] == "ok" and t["remote_hostname"] == "lab-ifup"

    snap = ok(await api.post(f"/api/v1/servers/{sid}/collect"))
    assert snap["status"] == "ok" and snap["duration_ms"] < 10_000
    f = snap["facts"]
    assert f["system"]["os"]["id"] == "ubuntu" and f["system"]["os"]["supported"]
    assert f["stack"]["key"] == "ifupdown"
    ifs = {i["name"]: i for i in f["interfaces"]}
    assert ifs["eth0"]["managed_by"] == "ifupdown" and [a["address"] for a in ifs["eth0"]["addresses"]] == [
        "172.30.0.13/24"
    ]
    assert ifs["br0"]["kind"] == "bridge"
    assert any(r["destination"] == "0.0.0.0/0" and r["gateway"] == "172.30.0.1" for r in f["routes"])

    raw = ok(await api.get(f"/api/v1/servers/{sid}/snapshots/{snap['id']}/raw"))
    assert raw["ifupdown_config"]["stdout"].startswith("[not stored")  # may hold secrets such as wpa-psk
    assert '"ifname":"eth0"' in raw["addresses"]["stdout"].replace(" ", "")

    summary = ok(await api.get(f"/api/v1/servers/{sid}"))
    assert summary["network_stack"].startswith("ifupdown") and summary["os_pretty_name"].startswith("Ubuntu")
    assert ok(await api.get(f"/api/v1/servers/{sid}/snapshots/latest"))["id"] == snap["id"]


async def test_password_login_on_networkmanager_host(api: httpx.AsyncClient) -> None:
    s = await register(
        api, "lab-nm", username="ops", credentials={"method": "password", "password": "ops-lab-password-1"}
    )
    snap = ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))
    assert snap["status"] == "ok", snap
    ifs = {i["name"]: i for i in snap["facts"]["interfaces"]}
    assert snap["facts"]["stack"]["key"] == "networkmanager"
    assert ifs["eth0"]["manager_profile"] == "lab-uplink"
    # "connected (externally)" as the lab starts; plain "connected" once a change test re-activated it.
    assert ifs["eth1"]["manager_state"] in ("connected (externally)", "connected")

    bad = await register(
        api, "lab-nm", username="ops", credentials={"method": "password", "password": "wrong-password"}
    )
    assert ok(await api.post(f"/api/v1/servers/{bad['id']}/test"))["status"] == "auth_failed"


async def test_host_key_change_is_refused_until_repinned(api: httpx.AsyncClient) -> None:
    s = await register(api, "lab-netplan")
    sid = s["id"]
    authorize("lab-netplan", s["authorized_keys_line"])
    assert ok(await api.post(f"/api/v1/servers/{sid}/test"))["status"] == "ok"

    old = server_fingerprint("lab-netplan")
    sh(
        "lab-netplan",
        "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && systemctl restart ssh.service 2>/dev/null; true",
    )
    assert server_fingerprint("lab-netplan") != old

    t = ok(await api.post(f"/api/v1/servers/{sid}/test"))
    assert t["status"] == "host_key_mismatch"
    snap = ok(await api.post(f"/api/v1/servers/{sid}/collect"))
    assert snap["status"] == "host_key_mismatch" and snap["facts"] is None

    hk = ok(await api.post("/api/v1/servers/scan-host-key", json={"address": HOSTS["lab-netplan"]}))
    assert hk["fingerprint_sha256"] == server_fingerprint("lab-netplan")
    r = await api.put(
        f"/api/v1/servers/{sid}/host-key", json={"public_key": hk["public_key"], "fingerprint_sha256": old}
    )
    assert r.status_code == 422  # the confirmed fingerprint must belong to the key being pinned
    ok(
        await api.put(
            f"/api/v1/servers/{sid}/host-key",
            json={"public_key": hk["public_key"], "fingerprint_sha256": hk["fingerprint_sha256"]},
        )
    )
    assert ok(await api.post(f"/api/v1/servers/{sid}/test"))["status"] == "ok"


async def test_unchanged_reads_confirm_changed_reads_are_kept(api: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Reads of a real host that did not change are recognised as the same state; changes are kept,
    up to the retention limit, and described in the history."""
    monkeypatch.setattr(get_settings(), "server_snapshot_retention", 2)
    s = await register(api, "lab-ifup")
    authorize("lab-ifup", s["authorized_keys_line"])

    same = [ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))["id"] for _ in range(3)]
    assert len(set(same)) == 1
    snaps = ok(await api.get(f"/api/v1/servers/{s['id']}/snapshots"))
    assert [x["id"] for x in snaps] == [same[0]] and snaps[0]["reads"] == 3

    try:
        sh("lab-ifup", "ip address add 192.0.2.201/32 dev lo")
        added = ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))["id"]
        sh("lab-ifup", "ip address add 192.0.2.202/32 dev lo")
        added2 = ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))["id"]
    finally:
        sh("lab-ifup", "ip address del 192.0.2.201/32 dev lo; ip address del 192.0.2.202/32 dev lo; true")
    kept = [x["id"] for x in ok(await api.get(f"/api/v1/servers/{s['id']}/snapshots"))]
    assert kept == [added2, added]  # the oldest state fell out of the retention limit

    history = ok(await api.get(f"/api/v1/servers/{s['id']}/history"))
    assert [c["text"] for c in history[0]["changes"]] == ["lo: address 192.0.2.202/32 added"]


async def test_discovery_from_real_hosts(api: httpx.AsyncClient) -> None:
    """Neighbour tables and LLDP as the lab hosts report them: systemd-networkd on lab-netplan, lldpd
    on lab-ifup, and lldpd on lab-nm, which lanvex may not ask (deploy/lab/README.md)."""
    # lldpd announces every 30 seconds; after the lab starts, wait until every neighbour this test
    # expects has been heard (a fast lab build reaches here before lab-nm's first announcement).
    for _ in range(40):
        seen_by_netplan = sh("lab-netplan", "networkctl lldp --no-pager 2>/dev/null; true")
        if (
            "lab-nm" in sh("lab-ifup", "lldpcli show neighbors 2>/dev/null; true")
            and "lab-ifup" in seen_by_netplan
            and "lab-nm" in seen_by_netplan
        ):
            break
        await asyncio.sleep(3)
    for h in HOSTS:
        sh(h, "for a in 11 12 13; do ping -c1 -W1 172.30.0.$a >/dev/null; done; true")
    facts = {}
    names = {}
    for h in HOSTS:
        srv = await register(api, h)
        authorize(h, srv["authorized_keys_line"])
        names[h] = srv["name"]
        facts[h] = ok(await api.post(f"/api/v1/servers/{srv['id']}/collect"))["facts"]

    def peers(h: str) -> list[tuple[str, str | None, str | None, str]]:
        return sorted(
            (n["interface"], n["system_name"], n["port_description"], n["source"]) for n in facts[h]["lldp"]
        )

    assert peers("lab-netplan") == [
        ("eth0", "lab-ifup", "eth0", "networkd"),
        ("eth0", "lab-nm", "eth0", "networkd"),
    ]
    assert ("eth0", "lab-nm", "eth0", "lldpd") in peers("lab-ifup")
    assert peers("lab-nm") == [] and any("may not ask it for LLDP" in w for w in facts["lab-nm"]["warnings"])
    for h, me in HOSTS.items():
        assert {n["address"] for n in facts[h]["neighbors"]} >= set(HOSTS.values()) - {me}

    hosts = {x["address"]: x for x in ok(await api.get("/api/v1/discovery/hosts"))["hosts"]}
    for h, address in HOSTS.items():
        assert hosts[address]["server"] == names[h]
        assert {s["server_name"] for s in hosts[address]["seen_by"]} == {names[o] for o in HOSTS if o != h}
        assert hosts[address]["record_as"] == f"{address}/24"
    links = ok(await api.get("/api/v1/discovery/links"))
    assert {(x["server_name"], x["neighbor"]["system_name"]) for x in links["links"]} >= {
        (names["lab-netplan"], "lab-nm"),
        (names["lab-netplan"], "lab-ifup"),
        (names["lab-ifup"], "lab-nm"),
    }
    assert names["lab-nm"] in links["servers_without_lldp"]


@pytest.mark.skipif(not (NB_URL and NB_ADMIN), reason="live NetBox variables not set")
async def test_documented_vs_actual_comparison(api: httpx.AsyncClient) -> None:
    nb = NetBoxClient(NB_URL, NB_ADMIN)
    tag = secrets.token_hex(3)
    made: list[str] = []
    # This test owns these lab addresses in the (disposable) NetBox, as the NetBox suite owns 198.51.100.0/24.
    for ip in await nb.list_all("ipam/ip-addresses/", {"address": list(OWNED_ADDRESSES)}):
        await nb.delete(f"ipam/ip-addresses/{ip['id']}/")
    try:
        site = await nb.post("dcim/sites/", {"name": f"lanvex-it lab {tag}", "slug": f"lanvex-it-lab-{tag}"})
        made.append(f"dcim/sites/{site['id']}/")
        mfr = await nb.post(
            "dcim/manufacturers/", {"name": f"lanvex-it lab {tag}", "slug": f"lanvex-it-labm-{tag}"}
        )
        made.append(f"dcim/manufacturers/{mfr['id']}/")
        dt = await nb.post(
            "dcim/device-types/",
            {"manufacturer": mfr["id"], "model": f"lanvex-it vm {tag}", "slug": f"lanvex-it-vm-{tag}"},
        )
        made.append(f"dcim/device-types/{dt['id']}/")
        role = await nb.post(
            "dcim/device-roles/",
            {"name": f"lanvex-it srv {tag}", "slug": f"lanvex-it-srv-{tag}", "color": "aaaaaa"},
        )
        made.append(f"dcim/device-roles/{role['id']}/")
        dev = await nb.post(
            "dcim/devices/",
            {"name": "lab-netplan", "device_type": dt["id"], "role": role["id"], "site": site["id"]},
        )
        made.append(f"dcim/devices/{dev['id']}/")
        eth0 = await nb.post("dcim/interfaces/", {"device": dev["id"], "name": "eth0", "type": "virtual"})
        br0 = await nb.post("dcim/interfaces/", {"device": dev["id"], "name": "br0", "type": "bridge"})
        loose = await nb.post("ipam/ip-addresses/", {"address": "172.30.0.11/24", "description": "lanvex-it"})
        made.insert(0, f"ipam/ip-addresses/{loose['id']}/")

        s = await register(api, "lab-netplan")
        authorize("lab-netplan", s["authorized_keys_line"])
        ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))

        c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
        assert c["linked"] is False
        assert {r["address"]: r["status"] for r in c["rows"]} == {
            "172.30.0.11": "only_on_server",
            "10.11.0.1": "only_on_server",
        }
        assert (
            next(r for r in c["rows"] if r["address"] == "172.30.0.11")["recorded_elsewhere"]
            == "172.30.0.11/24, not assigned"
        )
        assert any(
            x["object_type"] == "dcim.device" and x["object_id"] == dev["id"] for x in c["suggestions"]
        )

        await nb.patch(
            f"ipam/ip-addresses/{loose['id']}/",
            {"assigned_object_type": "dcim.interface", "assigned_object_id": eth0["id"]},
        )
        for addr, iface in (("10.11.0.1/16", br0), ("172.30.0.50/24", eth0)):
            ip = await nb.post(
                "ipam/ip-addresses/",
                {
                    "address": addr,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": iface["id"],
                },
            )
            made.insert(0, f"ipam/ip-addresses/{ip['id']}/")
        ok(
            await api.patch(
                f"/api/v1/servers/{s['id']}",
                json={"netbox": {"object_type": "dcim.device", "object_id": dev["id"]}},
            )
        )

        c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
        assert c["linked"] and c["netbox"]["name"] == "lab-netplan"
        assert {r["address"]: r["status"] for r in c["rows"]} == {
            "172.30.0.11": "match",
            "10.11.0.1": "mask_differs",
            "172.30.0.50": "only_in_netbox",
        }
        assert c["counts"] == {"match": 1, "mask_differs": 1, "only_in_netbox": 1}

        # Record what the server has: one more address on eth0, and one on an interface NetBox lacks.
        sh(
            "lab-netplan",
            "ip address add 172.30.0.60/24 dev eth0 && ip link add lanvex-t0 type bridge && "
            "ip address add 192.0.2.70/24 dev lanvex-t0 && ip link set lanvex-t0 up",
        )
        ok(await api.post(f"/api/v1/servers/{s['id']}/collect"))
        pick = {"addresses": ["172.30.0.60", "192.0.2.70", "10.11.0.1", "172.30.0.50", "172.30.0.11"]}
        plan = ok(await api.post(f"/api/v1/servers/{s['id']}/record", json=pick))
        assert [(a["kind"], a["text"]) for a in plan["actions"]] == [
            ("create_interface", "Add interface lanvex-t0 to lab-netplan (type Bridge)"),
            ("create", "Record 172.30.0.60/24 on lab-netplan / eth0"),
            ("create", "Record 192.0.2.70/24 on lab-netplan / lanvex-t0"),
            ("update_mask", "Change the record 10.11.0.1/16 to 10.11.0.1/24"),
            ("unassign", "Unassign 172.30.0.50/24 from lab-netplan / eth0 (the record stays in NetBox)"),
        ]
        assert plan["applied"] is False and not any(a["problem"] for a in plan["actions"])
        c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
        assert c["counts"]["only_on_server"] == 2  # the preview wrote nothing

        r = await api.post(f"/api/v1/servers/{s['id']}/record", json={**pick, "token": "0" * 32})
        assert r.status_code == 409, r.text
        done = ok(await api.post(f"/api/v1/servers/{s['id']}/record", json={**pick, "token": plan["token"]}))
        assert done["applied"] and done["failed"] is None and len(done["done"]) == 5
        c = ok(await api.get(f"/api/v1/servers/{s['id']}/comparison"))
        assert {r["address"]: r["status"] for r in c["rows"]} == {
            "172.30.0.11": "match",
            "172.30.0.60": "match",
            "192.0.2.70": "match",
            "10.11.0.1": "match",
        }
        unassigned = await nb.list_all("ipam/ip-addresses/", {"address": "172.30.0.50/24"})
        assert len(unassigned) == 1 and unassigned[0]["assigned_object"] is None
    finally:
        sh("lab-netplan", "ip address del 172.30.0.60/24 dev eth0; ip link del lanvex-t0; true")
        for path in made:
            try:
                await nb.delete(path)
            except Exception:  # noqa: S112 - best-effort cleanup
                continue
        await nb.aclose()
