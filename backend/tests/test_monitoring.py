"""Monitoring: what the checks find after a read and a probe, alerts' lifecycle, delivery by email and
webhook. Servers of any kind: the checks rest only on what Lanvex reads and what the documentation says."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.integrations.linux import adapter, transport
from lanvex.integrations.linux.assemble import assemble
from lanvex.integrations.linux.facts import DhcpFacts, DhcpLease, DhcpPool
from lanvex.monitoring import checks, notify
from lanvex.monitoring.models import Alert, AlertNotification, ServerProbe
from lanvex.monitoring.notify import Monitor
from lanvex.monitoring.schemas import MonitoringSettings
from lanvex.monitoring.service import MonitoringService
from lanvex.servers.models import ConnectionStatus, Server

from . import test_networks_api
from .test_changes_api import FakeServer, with_lanvex_vlan
from .test_linux_parsing import load
from .test_networks_api import DEVICE, CoreStandIn
from .test_servers_api import body as server_body

fake, core, clock = test_networks_api.fake, test_networks_api.core, test_networks_api.clock


async def router_01(admin: httpx.AsyncClient, fake: FakeServer) -> str:
    """A server linked to its device in Lanvex Core, read once."""
    fake.facts = assemble(load("nm"))
    r = await admin.post(
        "/api/v1/servers",
        json=server_body(
            name="router-01",
            address="172.30.0.12",
            port=22,
            netbox={"object_type": "dcim.device", "object_id": DEVICE},
        ),
    )
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).status_code == 200
    return sid


HOOK = "https://hooks.example.test/services/T000/B000/secret-token"


def with_leases(leases: list[tuple[str, str]], end: str = "192.168.50.103") -> Any:
    facts = with_lanvex_vlan(assemble(load("nm")), "192.168.50.1")
    soon = datetime.now(UTC) + timedelta(hours=2)
    return facts.model_copy(
        update={
            "dhcp": DhcpFacts(
                pools=[
                    DhcpPool(
                        interface="vlan50",
                        start="192.168.50.100",
                        end=end,
                        netmask="255.255.255.0",
                        router="192.168.50.1",
                    )
                ],
                leases=[
                    DhcpLease(ip=ip, mac=mac, hostname=f"host{i}", expires_at=soon)
                    for i, (ip, mac) in enumerate(leases)
                ],
            )
        }
    )


async def alerts(admin: httpx.AsyncClient, state: str = "open") -> dict[str, dict[str, Any]]:
    r = await admin.get(f"/api/v1/alerts?state={state}")
    assert r.status_code == 200, r.text
    return {f"{a['kind']}:{a['key']}": a for a in r.json()["alerts"]}


# ---- the checks ---------------------------------------------------------------------------------------


def test_pool_usage_and_unknown_devices_from_a_read() -> None:
    now = datetime.now(UTC)
    facts = with_leases(
        [
            ("192.168.50.100", "aa:bb:cc:00:00:01"),
            ("192.168.50.101", "aa:bb:cc:00:00:02"),
            ("192.168.50.20", "aa:bb:cc:00:00:03"),
        ]
    )
    (full,) = checks.pool_usage("router-01", facts, 50, now)
    assert full.key == "vlan50" and full.title == "DHCP pool on vlan50 (router-01) 50% in use"
    assert full.detail == ["2 of 4 addresses leased (192.168.50.100 to 192.168.50.103)."]
    assert checks.pool_usage("router-01", facts, 60, now) == []

    cfg = MonitoringSettings()
    found = checks.unknown_devices("router-01", facts, {"aa:bb:cc:00:00:02"}, cfg, now)
    assert [f.key for f in found] == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:03"]
    assert found[0].title == "Unknown device host0 (aa:bb:cc:00:00:01) on vlan50"
    ignore = MonitoringSettings(unknown_device_ignore=["vlan50"])
    assert checks.unknown_devices("router-01", facts, set(), ignore, now) == []
    ignore = MonitoringSettings(unknown_device_ignore=["192.168.50.96/28"])
    assert [f.key for f in checks.unknown_devices("router-01", facts, set(), ignore, now)] == [
        "aa:bb:cc:00:00:03"
    ]


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="not an email address"):
        MonitoringSettings(email_to=["ops at example.net"])
    with pytest.raises(ValueError, match="not a network name or prefix"):
        MonitoringSettings(unknown_device_ignore=["guest wifi"])
    with pytest.raises(ValueError, match="not an interface name or pattern"):
        MonitoringSettings(drift_ignore_interfaces=["docker 0"])
    assert MonitoringSettings(email_to=[" ops@example.net ", "ops@example.net"]).email_to == [
        "ops@example.net"
    ]


# ---- after a read -------------------------------------------------------------------------------------


async def test_alerts_follow_what_reads_find(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, database: Database
) -> None:
    r = await admin.post(
        "/api/v1/monitoring/webhooks",
        json={"name": "Ops chat", "format": "slack", "url": HOOK, "min_severity": "warning"},
    )
    assert r.status_code == 201, r.text
    hook = r.json()
    assert hook["url_hint"] == "https://hooks.example.test/…" and "url" not in hook
    sid = await router_01(admin, fake)
    # The pool is full; one device is the NAS documented with its MAC address, one nobody documented.
    fake.facts = with_leases(
        [
            ("192.168.50.100", "aa:bb:cc:00:00:05"),
            ("192.168.50.101", "aa:bb:cc:00:00:99"),
            ("192.168.50.102", "aa:bb:cc:00:00:98"),
            ("192.168.50.103", "aa:bb:cc:00:00:97"),
        ]
    )
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).json()["status"] == "ok"
    found = await alerts(admin)
    assert found["pool_full:vlan50"]["severity"] == "critical"
    assert found["pool_full:vlan50"]["title"] == "DHCP pool on vlan50 (router-01) 100% in use"
    assert {k for k in found if k.startswith("unknown_device")} == {
        "unknown_device:aa:bb:cc:00:00:99",
        "unknown_device:aa:bb:cc:00:00:98",
        "unknown_device:aa:bb:cc:00:00:97",
    }
    drift = found["drift:"]
    assert drift["severity"] == "warning" and "differs from its documentation" in drift["title"]
    assert "172.30.0.12 (eth0): not documented" in drift["detail"]
    assert not any(line.startswith("172.") and "docker" in line for line in drift["detail"])
    count = (await admin.get("/api/v1/alerts/count")).json()
    assert count == {"open": 5, "unacknowledged": 5, "critical": 1}

    # The webhook takes warnings and above: the pool and the drift (found by the first read, more of it
    # by the second), not the devices.
    async with database.sessionmaker() as session:
        queued = (await session.scalars(select(AlertNotification))).all()
        assert sorted(n.event for n in queued) == ["changed", "opened", "opened"]
        assert {n.channel for n in queued} == {f"webhook:{hook['id']}"}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(HOOK).mock(return_value=httpx.Response(200))
        assert await Monitor(database, get_settings(), lambda: None).deliver() == 3
        (call,) = route.calls  # one message for all
        sent = call.request.content.decode()
        assert (
            '"text": "*3 alerts*' in sent and "100% in use" in sent and "Changed: router-01 differs" in sent
        )
    async with database.sessionmaker() as session:
        assert all(n.sent_at for n in (await session.scalars(select(AlertNotification))).all())

    # Acknowledging an unknown device ends its alert; it is not reported again while still unknown.
    unknown = found["unknown_device:aa:bb:cc:00:00:99"]
    r = await admin.post(f"/api/v1/alerts/{unknown['id']}/acknowledge")
    assert r.json()["state"] == "resolved" and r.json()["acknowledged_by"] == "admin"
    # The pool has room again; one device is documented now.
    core.t("dcim/interfaces")[21]["mac_addresses"].append({"id": 9, "mac_address": "aa:bb:cc:00:00:98"})
    fake.facts = with_leases(
        [("192.168.50.100", "aa:bb:cc:00:00:99"), ("192.168.50.101", "aa:bb:cc:00:00:98")],
        end="192.168.50.199",
    )
    await admin.post(f"/api/v1/servers/{sid}/collect")
    found = await alerts(admin)
    assert "pool_full:vlan50" not in found and "unknown_device:aa:bb:cc:00:00:99" not in found
    assert "unknown_device:aa:bb:cc:00:00:98" not in found  # documented: resolved
    assert "unknown_device:aa:bb:cc:00:00:97" in found  # its lease ended, it is still not documented
    resolved = await alerts(admin, "resolved")
    assert {
        "pool_full:vlan50",
        "unknown_device:aa:bb:cc:00:00:98",
        "unknown_device:aa:bb:cc:00:00:99",
    } <= set(resolved)
    async with database.sessionmaker() as session:
        pending = (
            await session.scalars(select(AlertNotification).where(AlertNotification.sent_at.is_(None)))
        ).all()
        assert [n.event for n in pending] == ["resolved"]  # the pool; devices are below the webhook's level

    # A check turned off ends its alerts without a message.
    cfg = (await admin.get("/api/v1/monitoring")).json()["settings"]
    r = await admin.put("/api/v1/monitoring", json=cfg | {"drift": False})
    assert r.status_code == 200, r.text
    await admin.post(f"/api/v1/servers/{sid}/collect")
    assert "drift:" not in await alerts(admin)


async def test_a_failed_read_says_so_and_keeps_what_the_last_good_read_found(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await router_01(admin, fake)
    assert "drift:" in await alerts(admin)
    collect = adapter.collect

    async def rejected(*_: Any, **__: Any) -> Any:
        raise transport.SSHError("The server rejected the credentials for user 'lanvex'.", code="auth_failed")

    monkeypatch.setattr(adapter, "collect", rejected)
    await admin.post(f"/api/v1/servers/{sid}/collect")
    found = await alerts(admin)
    assert found["read_failed:"]["title"] == "router-01: sign-in rejected"
    assert found["read_failed:"]["detail"] == ["The server rejected the credentials for user 'lanvex'."]
    assert "drift:" in found  # unknown now; not resolved by a read that saw nothing
    # Not answering at all is the probe's alert, not a second one.
    fake.reachable = False
    monkeypatch.setattr(adapter, "collect", collect)
    await admin.post(f"/api/v1/servers/{sid}/collect")
    assert "read_failed:" not in await alerts(admin)
    fake.reachable = True
    await admin.post(f"/api/v1/servers/{sid}/collect")
    assert "read_failed:" not in await alerts(admin)


# ---- probes -------------------------------------------------------------------------------------------


async def test_a_server_whose_ssh_port_stops_answering(
    admin: httpx.AsyncClient,
    core: CoreStandIn,
    fake: FakeServer,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await router_01(admin, fake)
    answers: list[str | None] = ["Connection refused", "Connection refused", "No answer (timed out).", None]

    async def probe(self: Monitor, address: str, port: int) -> str | None:
        return answers.pop(0)

    monkeypatch.setattr(Monitor, "probe", probe)
    monitor = Monitor(database, get_settings(), lambda: None)
    for _ in range(2):
        await monitor.probe_all()
    assert "unreachable:" not in await alerts(admin)  # two misses may be a restart
    await monitor.probe_all()
    found = (await alerts(admin))["unreachable:"]
    assert found["severity"] == "critical" and found["title"] == "router-01 is not reachable"
    assert found["detail"] == ["SSH port not answering (3 checks in a row).", "No answer (timed out)."]
    await monitor.probe_all()
    assert "unreachable:" not in await alerts(admin)
    async with database.sessionmaker() as session:
        p = await session.get(ServerProbe, uuid.UUID(sid))
        assert p is not None and p.failures == 0 and p.last_ok_at is not None


async def test_the_probe_connects_without_signing_in(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    connections: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(await reader.read(100))  # the probe sends nothing and closes
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monitor = Monitor(database, get_settings(), lambda: None)
    try:
        assert await monitor.probe("127.0.0.1", port) is None
        await asyncio.sleep(0.05)
        assert connections == [b""]
    finally:
        server.close()
        await server.wait_closed()
    assert await monitor.probe("127.0.0.1", port) is not None  # nothing listens any more


# ---- delivery -----------------------------------------------------------------------------------------


class FakeSMTP:
    sent: list[EmailMessage] = []
    calls: list[str] = []

    def __init__(self, host: str, port: int, timeout: float = 0, **_: Any) -> None:
        self.calls.append(f"connect {host}:{port}")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)

    def quit(self) -> None:
        self.calls.append("quit")


async def test_email_with_retries(
    admin: httpx.AsyncClient,
    fake: FakeServer,
    core: CoreStandIn,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            "smtp_host": "mail.example.net",
            "smtp_from": "lanvex@example.net",
            "smtp_username": "lanvex",
            "smtp_password": SecretStr("app-password"),
            "public_url": "https://lanvex.example.net",
        }
    )
    sid = await router_01(admin, fake)
    async with database.sessionmaker() as session:
        svc = MonitoringService(session, settings)
        from lanvex.auth.models import User

        admin_user = (await session.scalars(select(User))).first()
        assert admin_user is not None
        await svc.save_config(
            MonitoringSettings(email_enabled=True, email_to=["ops@example.net"]), admin_user
        )
        s = await session.get(Server, uuid.UUID(sid))
        assert s is not None
        snap_status = ConnectionStatus.AUTH_FAILED

        class Snap:
            status, message, facts = snap_status, "rejected", None

        await svc.after_read(s, Snap())  # type: ignore[arg-type]
        await session.commit()
    FakeSMTP.sent, FakeSMTP.calls = [], []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monitor = Monitor(database, settings, lambda: None)

    def broken(*_: Any, **__: Any) -> None:
        raise OSError("Connection refused")

    monkeypatch.setattr(notify.smtplib, "SMTP", broken)
    now = datetime.now(UTC)
    assert await monitor.deliver(now) == 0
    async with database.sessionmaker() as session:
        (n,) = (await session.scalars(select(AlertNotification))).all()
        assert n.attempts == 1 and n.error == "Email: OSError: Connection refused" and n.sent_at is None
    assert await monitor.deliver(now + timedelta(seconds=30)) == 0  # not due yet
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    assert await monitor.deliver(now + timedelta(minutes=2)) == 1
    (msg,) = FakeSMTP.sent
    assert msg["Subject"] == "[Lanvex] router-01: sign-in rejected" and msg["To"] == "ops@example.net"
    assert "https://lanvex.example.net/alerts" in msg.get_content()
    assert FakeSMTP.calls == ["connect mail.example.net:587", "starttls", "login lanvex", "quit"]


async def test_webhooks_never_show_their_url(admin: httpx.AsyncClient, database: Database) -> None:
    r = await admin.post("/api/v1/monitoring/webhooks", json={"name": "ntfy", "format": "ntfy", "url": HOOK})
    wid = r.json()["id"]
    r = await admin.put(f"/api/v1/monitoring/webhooks/{wid}", json={"name": "ntfy on call", "format": "ntfy"})
    assert r.status_code == 200 and r.json()["name"] == "ntfy on call"
    listed = (await admin.get("/api/v1/monitoring/webhooks")).text
    assert "secret-token" not in listed
    audit = (await admin.get("/api/v1/audit?limit=20")).text
    assert "secret-token" not in audit
    with respx.mock() as mock:
        route = mock.post(HOOK).mock(return_value=httpx.Response(403))
        r = await admin.post("/api/v1/monitoring/test", json={"channel": f"webhook:{wid}"})
        assert r.json() == {
            "channel": f"webhook:{wid}",
            "ok": False,
            "error": "https://hooks.example.test/…: HTTP 403",
        }
        assert route.calls[0].request.headers["Title"] == "Test message from Lanvex"
    r = await admin.post("/api/v1/monitoring/test", json={"channel": "email"})
    assert r.json()["ok"] is False and "SMTP is not configured" in r.json()["error"]
    assert (await admin.delete(f"/api/v1/monitoring/webhooks/{wid}")).status_code == 204


async def test_readers_see_alerts_but_cannot_change_monitoring(reader: httpx.AsyncClient) -> None:
    assert (await reader.get("/api/v1/alerts")).status_code == 200
    assert (await reader.get("/api/v1/monitoring")).status_code == 403
    assert (await reader.put("/api/v1/monitoring", json={})).status_code == 403


async def test_alerts_end_with_their_server(
    admin: httpx.AsyncClient, core: CoreStandIn, fake: FakeServer, database: Database
) -> None:
    sid = await router_01(admin, fake)
    assert await alerts(admin)
    assert (await admin.delete(f"/api/v1/servers/{sid}")).status_code == 204
    async with database.sessionmaker() as session:
        assert (await session.scalars(select(Alert))).all() == []
