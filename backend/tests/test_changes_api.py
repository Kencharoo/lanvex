"""Configuration changes: preparation, approval, the runner's outcomes, and the safety rules.

The SSH layer and the server-side helper are replaced by a scripted fake (FakeServer), and time by a
fake clock, so every outcome (success, failed verification, lost contact, drift, a dropped connection)
runs in milliseconds. test_changes_lab.py runs the same flow against real netplan and
NetworkManager hosts.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncssh
import httpx
import pytest
from sqlalchemy import func, select

from lanvex.audit.models import AuditEvent
from lanvex.auth.models import Role
from lanvex.auth.service import create_user
from lanvex.changes.models import ChangeStatus, ServerChange
from lanvex.changes.runner import ChangeRunner, Clock
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.integrations.linux import adapter, helper, transport
from lanvex.integrations.linux.adapter import Collection
from lanvex.integrations.linux.assemble import assemble
from lanvex.integrations.linux.commands import Cmd
from lanvex.servers.models import ServerSnapshot

from .conftest import login
from .test_linux_parsing import load
from .test_servers_api import body as server_body

MGMT = "172.30.0.11"
BR0_SECONDARY = {
    "interface": "br0",
    "method": "static",
    "addresses": ["10.11.0.1/24", "10.11.0.2/24"],
    "gateway": None,
    "dns_servers": [],
    "dns_search": [],
}


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(seconds, 0.001))


class FakeServer:
    """Plays the SSH server and the helper on it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[Cmd, dict[str, Any] | None]] = []
        self.states: dict[str, dict[str, Any]] = {}
        self.version: dict[str, Any] | Exception = {
            "ok": True,
            "version": 1,
            "sha256": helper.sha256(),
            "python": "3.12.3",
            "tools": {"systemd_run": True},
        }
        self.plan_error: Exception | None = None
        self.apply_error: Exception | None = None
        self.drop_during_apply = False
        self.verify_passed = True
        self.reachable = True
        self.lose_contact_after_apply = False  # unreachable until the server's own rollback has run
        self.never_back = False
        self.deadline: datetime | None = None
        self.down: set[str] = set()  # addresses that do not answer
        self.down_after_apply: set[str] = set()  # addresses a change cuts off

    def _timer(self) -> None:
        """The server's rollback timer: fires at the deadline unless the change was confirmed."""
        for st in self.states.values():
            if st["state"] == "applied" and self.deadline and self.clock.now() >= self.deadline:
                st.update(state="rolled_back", reason="No confirmation from Lanvex before the deadline.")

    def can_connect(self) -> bool:
        self._timer()
        if not self.reachable:
            return False
        if self.lose_contact_after_apply and self.deadline:
            return not self.never_back and self.clock.now() >= self.deadline
        return True

    @asynccontextmanager
    async def connect(self, target: transport.Target, timeout: float) -> AsyncIterator[object]:
        cut = self.down_after_apply if self.deadline else set()
        if not self.can_connect() or target.address in self.down | cut:
            raise transport.SSHError(
                f"{target.address}:22 could not be reached (TimeoutError).", code="unreachable"
            )
        yield object()

    async def call(self, conn: object, cmd: Cmd, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((cmd, payload))
        self._timer()
        if cmd == Cmd.HELPER_VERSION:
            if isinstance(self.version, Exception):
                raise self.version
            return self.version
        if cmd == Cmd.HELPER_PLAN:
            if self.plan_error:
                raise self.plan_error
            return {
                "ok": True,
                "backend": "netplan",
                "files": [
                    {
                        "path": "/etc/netplan/50-lab.yaml",
                        "label": "netplan file",
                        "before": "      addresses: [10.11.0.1/24]\n",
                        "after": "      addresses: [10.11.0.1/24, 10.11.0.2/24]\n",
                        "sha256": "abc123",
                        "action": "modify",
                    }
                ],
                "commands": ["netplan generate", "netplan apply"],
                "warnings": ["br0 is a bridge entry; only its IPv4 settings change."],
            }
        assert payload is not None
        cid = payload["id"]
        if cmd == Cmd.HELPER_APPLY:
            if self.apply_error:
                raise self.apply_error
            self.deadline = self.clock.now() + timedelta(seconds=payload["rollback_seconds"])
            self.states[cid] = {"state": "applied", "reason": None}
            if self.drop_during_apply:
                raise asyncssh.ConnectionLost("Connection lost")
            return {
                "ok": True,
                "state": "applied",
                "deadline": self.deadline.timestamp(),
                "backend": "netplan",
            }
        if cid not in self.states:
            raise helper.HelperRefused("This server has no record of the change.", code="unknown_change")
        st = self.states[cid]
        if cmd == Cmd.HELPER_STATUS:
            return {"ok": True, "state": st["state"], "reason": st["reason"], "history": []}
        if cmd == Cmd.HELPER_VERIFY:
            checks = [
                {"id": "addresses", "label": "IPv4 addresses on br0", "ok": True, "detail": "found"},
                {
                    "id": "gateway",
                    "label": "Gateway 10.11.0.254 answers",
                    "ok": self.verify_passed,
                    "detail": "x",
                },
            ]
            return {"ok": True, "state": "applied", "checks": checks, "passed": self.verify_passed}
        if cmd == Cmd.HELPER_CONFIRM:
            if st["state"] != "applied":
                raise helper.HelperRefused(f"Change was {st['state']}.", code="wrong_state")
            st["state"] = "confirmed"
            return {"ok": True, "state": "confirmed"}
        if cmd == Cmd.HELPER_ROLLBACK:
            st.update(state="rolled_back", reason="Rolled back at Lanvex's request.")
            return {"ok": True, "state": "rolled_back", "reason": st["reason"]}
        raise AssertionError(cmd)

    def commands(self) -> list[Cmd]:
        return [c for c, _ in self.calls]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> FakeServer:
    f = FakeServer(clock)
    facts = assemble(load("netplan"))

    async def resolve(address: str) -> list[str]:
        return [address]

    async def collect(target: transport.Target, connect_timeout: float, command_timeout: float) -> Collection:
        if not f.can_connect():
            raise transport.SSHError("unreachable", code="unreachable")
        return Collection(facts=facts, raw={}, duration_ms=5)

    async def upload(conn: object, filename: str, data: bytes, timeout: float) -> str:
        return f"/home/lanvex/{filename}"

    monkeypatch.setattr(transport, "connect", f.connect)
    monkeypatch.setattr(transport, "resolve", resolve)
    monkeypatch.setattr(transport, "upload", upload)
    monkeypatch.setattr(helper, "call", f.call)
    monkeypatch.setattr(adapter, "collect", collect)
    return f


@pytest.fixture
def automation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "automation_enabled", True)


@pytest.fixture
def runner(app, database: Database, clock: FakeClock) -> ChangeRunner:  # type: ignore[no-untyped-def]
    r = ChangeRunner(database, get_settings(), lambda: None, clock=clock)
    app.state.change_runner = r
    return r


async def new_server(admin: httpx.AsyncClient) -> str:
    r = await admin.post("/api/v1/servers", json=server_body(address=MGMT, port=22))
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"]


@pytest.fixture
async def ready_server(
    admin: httpx.AsyncClient, fake: FakeServer, automation: None, runner: ChangeRunner
) -> str:
    sid = await new_server(admin)
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).status_code == 200
    r = await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")
    assert r.json()["helper"]["state"] == "ok", r.text
    r = await admin.put(f"/api/v1/servers/{sid}/changes/enabled", json={"enabled": True})
    assert r.status_code == 200 and r.json()["ready"] is True, r.text
    return sid


async def prepare(admin: httpx.AsyncClient, sid: str, **over: Any) -> dict[str, Any]:
    r = await admin.post(f"/api/v1/servers/{sid}/changes", json=BR0_SECONDARY | over)
    assert r.status_code == 201, r.text
    return r.json()


async def approve_and_run(
    admin: httpx.AsyncClient, runner: ChangeRunner, change: dict[str, Any]
) -> dict[str, Any]:
    r = await admin.post(f"/api/v1/changes/{change['id']}/approve", json={"confirm_name": "web-01"})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert await runner.run_once() is True
    return (await admin.get(f"/api/v1/changes/{change['id']}")).json()


# ---- off by default -----------------------------------------------------------------------------------


async def test_changes_are_off_by_default(admin: httpx.AsyncClient, fake: FakeServer) -> None:
    sid = await new_server(admin)
    r = await admin.get(f"/api/v1/servers/{sid}/changes/readiness")
    assert r.status_code == 200 and r.json()["ready"] is False and r.json()["automation_enabled"] is False
    assert any("LANVEX_AUTOMATION_ENABLED" in b for b in r.json()["blockers"])
    r = await admin.put(f"/api/v1/servers/{sid}/changes/enabled", json={"enabled": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "automation_disabled"
    r = await admin.post(f"/api/v1/servers/{sid}/changes", json=BR0_SECONDARY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "changes_not_ready"
    assert fake.calls == []  # nothing was asked of the server
    caps = (await admin.get(f"/api/v1/servers/{sid}")).json()["capabilities"]
    assert "change.interface_ipv4" in caps  # the adapter can; the installation does not allow it


async def test_readiness_reports_each_missing_piece(
    admin: httpx.AsyncClient, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await new_server(admin)
    blockers = (await admin.get(f"/api/v1/servers/{sid}/changes/readiness")).json()["blockers"]
    assert [b.split(".")[0] for b in blockers] == [
        "Read the server first, so Lanvex knows how its network is managed",
        "The change helper has not been checked on this server yet",
        "Changes are not enabled for this server",
    ]
    fake.version = helper.HelperUnavailable(
        "The helper is not installed at /usr/local/libexec/lanvex/lanvex-netapply.", code="missing"
    )
    r = (await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")).json()
    assert r["helper"]["state"] == "missing" and "not installed" in r["helper"]["message"]
    fake.version = {"ok": True, "version": 0, "python": "3.8.10", "tools": {"systemd_run": True}}
    r = (await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")).json()
    assert r["helper"]["state"] == "outdated"
    # Same version number, different file (an older build, or edited on the server): also outdated.
    fake.version = {
        "ok": True,
        "version": 1,
        "sha256": "0" * 64,
        "python": "3.12.3",
        "tools": {"systemd_run": True},
    }
    r = (await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")).json()
    assert r["helper"]["state"] == "outdated" and "different build" in r["helper"]["message"]

    up = await admin.post(f"/api/v1/servers/{sid}/changes/helper/upload")
    assert up.status_code == 200
    out = up.json()
    assert out["path"] == "/home/lanvex/lanvex-netapply" and len(out["sha256"]) == 64
    assert out["commands"][0] == f"echo '{out['sha256']}  /home/lanvex/lanvex-netapply' | sha256sum -c -"
    assert "NOPASSWD: /usr/local/libexec/lanvex/lanvex-netapply" in out["commands"][2]
    dl = await admin.get("/api/v1/changes/helper")
    assert dl.status_code == 200 and dl.headers["x-lanvex-sha256"] == out["sha256"]
    assert dl.content.startswith(b"#!/usr/bin/python3 -I")


async def test_reader_can_look_but_not_change(
    client: httpx.AsyncClient, ready_server: str, users: dict[str, str]
) -> None:
    change = await prepare(client, ready_server)
    await login(client, "reader", users["reader"])
    assert (await client.get(f"/api/v1/servers/{ready_server}/changes/readiness")).status_code == 200
    detail = (await client.get(f"/api/v1/changes/{change['id']}")).json()
    assert detail["can_approve"] is False and detail["can_cancel"] is False
    for method, path, payload in (
        ("post", f"/api/v1/changes/{change['id']}/approve", {"confirm_name": "web-01"}),
        ("post", f"/api/v1/changes/{change['id']}/cancel", None),
        ("put", f"/api/v1/servers/{ready_server}/changes/enabled", {"enabled": False}),
        ("post", f"/api/v1/servers/{ready_server}/changes", BR0_SECONDARY),
        ("post", f"/api/v1/servers/{ready_server}/changes/helper/upload", None),
    ):
        r = await client.request(method, path, json=payload)
        assert r.status_code == 403, (path, r.text)


# ---- preparing ---------------------------------------------------------------------------------------


async def test_prepare_shows_exactly_what_will_change(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, database: Database
) -> None:
    c = await prepare(admin, ready_server)
    assert c["status"] == "pending_approval" and c["number"] >= 1
    assert c["summary"] == "br0: static 10.11.0.1/24, 10.11.0.2/24 · no gateway · no DNS servers"
    assert c["before"]["addresses"] == ["10.11.0.1/24"]
    f = c["preview"]["files"][0]
    assert (
        f["path"] == "/etc/netplan/50-lab.yaml"
        and "+      addresses: [10.11.0.1/24, 10.11.0.2/24]" in f["diff"]
    )
    assert "sha256" not in f  # kept server-side for the drift check
    assert c["preview"]["warnings"] == ["br0 is a bridge entry; only its IPv4 settings change."]
    assert c["can_approve"] is True and c["log"][0]["message"].startswith("Prepared by admin")
    # Preparing reads the server and asks for a plan; it never applies anything.
    assert fake.commands() == [Cmd.HELPER_VERSION, Cmd.HELPER_PLAN]
    async with database.sessionmaker() as db:
        assert await db.scalar(select(func.sum(ServerSnapshot.reads))) == 2  # reads, kept or confirmed


@pytest.mark.parametrize(
    ("change", "field", "message"),
    [
        (
            {"interface": "eth0", "addresses": ["172.30.0.12/24"]},
            "addresses",
            "Lanvex connects to web-01 at 172.30.0.11",
        ),
        ({"interface": "eth0", "method": "dhcp", "addresses": []}, "addresses", "Keep that address"),
        ({"interface": "eth7"}, "interface", "has no interface eth7"),
    ],
)
async def test_prepare_refuses_changes_that_would_cut_lanvex_off_or_make_no_sense(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, change: dict, field: str, message: str
) -> None:
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=BR0_SECONDARY | change)
    assert r.status_code == 422, r.text
    assert message in r.json()["error"]["message"] and field in r.json()["error"]["fields"]
    assert Cmd.HELPER_PLAN not in fake.commands()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"gateway": "10.99.0.1"}, "not inside any of the interface's networks"),
        ({"addresses": ["fd00::1/64"]}, "Only IPv4"),
        ({"addresses": ["10.11.0.0/24"]}, "network or broadcast"),
        ({"addresses": []}, "at least one address"),
        ({"method": "dhcp", "addresses": ["10.11.0.5/24"]}, "come from the DHCP server"),
        ({"dns_search": ["not a domain"]}, "not a valid domain"),
        ({"interface": "br0;reboot"}, "Not an interface"),
    ],
)
async def test_prepare_validates_input(
    admin: httpx.AsyncClient, ready_server: str, change: dict, message: str
) -> None:
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=BR0_SECONDARY | change)
    assert r.status_code == 422 and message in r.text, r.text


async def test_management_interface_change_is_allowed_with_a_warning(
    admin: httpx.AsyncClient, ready_server: str
) -> None:
    c = await prepare(
        admin,
        ready_server,
        interface="eth0",
        addresses=["172.30.0.11/24"],
        gateway="172.30.0.254",
        dns_servers=["172.30.0.1"],
    )
    w = c["preview"]["warnings"]
    assert w[0].startswith("Lanvex reaches web-01 through eth0. If the change breaks that connection")
    assert "The default gateway changes from 172.30.0.1 to 172.30.0.254." in w


async def test_one_open_change_per_server_and_no_op_changes(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer
) -> None:
    first = await prepare(admin, ready_server)
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=BR0_SECONDARY)
    assert r.status_code == 409 and f"#{first['number']}" in r.json()["error"]["message"]
    assert (await admin.post(f"/api/v1/changes/{first['id']}/cancel")).json()["status"] == "cancelled"
    fake.plan_error = helper.HelperRefused("already has these settings", code="no_change")
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=BR0_SECONDARY)
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_change"


# ---- approving and running --------------------------------------------------------------------------


async def test_success_applies_verifies_and_confirms(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner, database: Database
) -> None:
    c = await prepare(admin, ready_server)
    r = await admin.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": "WEB-01"})
    assert r.status_code == 422 and r.json()["error"]["fields"]["confirm_name"] == ["Type web-01."]
    done = await approve_and_run(admin, runner, c)
    assert done["status"] == "succeeded", done
    assert done["outcome"] == "Applied, verified and confirmed. The new configuration is permanent."
    assert [ch["id"] for ch in done["checks"]] == ["lanvex", "addresses", "gateway"]
    apply = next(p for cmd, p in fake.calls if cmd == Cmd.HELPER_APPLY)
    assert apply is not None
    assert apply["id"] == c["id"].replace("-", "") and apply["rollback_seconds"] == 120
    assert apply["expect"] == {"backend": "netplan", "files": {"/etc/netplan/50-lab.yaml": "abc123"}}
    assert apply["ipv4"] == {
        "method": "static",
        "addresses": ["10.11.0.1/24", "10.11.0.2/24"],
        "gateway": None,
    }
    assert fake.commands()[-4:] == [
        Cmd.HELPER_APPLY,
        Cmd.HELPER_STATUS,
        Cmd.HELPER_VERIFY,
        Cmd.HELPER_CONFIRM,
    ]
    messages = [e["message"] for e in done["log"]]
    assert messages[0].startswith("Prepared by admin") and messages[1] == "Approved by admin."
    assert any(
        m.startswith("Applied (netplan). The server restores its previous configuration at") for m in messages
    )
    async with database.sessionmaker() as db:
        actions = {
            (e.action, e.outcome)
            for e in (
                await db.scalars(select(AuditEvent).where(AuditEvent.action.like("server.change.%")))
            ).all()
        }
        assert actions == {
            ("server.change.prepare", "success"),
            ("server.change.approve", "failure"),
            ("server.change.approve", "success"),
            ("server.change.execute", "success"),
        }
        assert await db.scalar(select(func.sum(ServerSnapshot.reads))) == 3  # read again afterwards
    detail = (await admin.get(f"/api/v1/changes/{c['id']}")).json()
    assert detail["can_approve"] is False and detail["can_cancel"] is False
    assert (await admin.post(f"/api/v1/changes/{c['id']}/cancel")).status_code == 409


async def test_failed_verification_rolls_back(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    fake.verify_passed = False
    done = await approve_and_run(admin, runner, await prepare(admin, ready_server))
    assert done["status"] == "rolled_back"
    assert done["outcome"] == (
        "Verification failed (Gateway 10.11.0.254 answers). "
        "Lanvex had the server restore its previous configuration."
    )
    assert Cmd.HELPER_ROLLBACK in fake.commands() and Cmd.HELPER_CONFIRM not in fake.commands()
    assert [ch["ok"] for ch in done["checks"]] == [True, True, False]


async def test_lost_contact_is_never_confirmed_and_the_server_restores_itself(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    fake.lose_contact_after_apply = True
    done = await approve_and_run(admin, runner, await prepare(admin, ready_server))
    assert done["status"] == "rolled_back", done
    assert done["outcome"].startswith("Lanvex could not verify the change, so it did not confirm it.")
    assert "No confirmation from Lanvex before the deadline" in done["outcome"]
    assert Cmd.HELPER_CONFIRM not in fake.commands() and Cmd.HELPER_VERIFY not in fake.commands()
    assert done["checks"] == [
        {
            "id": "lanvex",
            "label": "Lanvex can still sign in",
            "ok": False,
            "detail": "No working SSH session to 172.30.0.11 since the change: 172.30.0.11:22 could not be reached (TimeoutError).",
        }
    ]
    assert any(
        e["level"] == "warning" and e["message"].startswith("Lanvex cannot reach the server yet")
        for e in done["log"]
    )


async def test_unreachable_after_the_deadline_needs_attention_until_checked_again(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    fake.lose_contact_after_apply = fake.never_back = True
    c = await prepare(admin, ready_server)
    done = await approve_and_run(admin, runner, c)
    assert done["status"] == "needs_attention" and done["can_recheck"] is True
    assert "Check the server on its console" in done["outcome"]
    # Another change can be prepared (the helper refuses to run two), but it says what is unresolved.
    fake.never_back = False
    other = await prepare(admin, ready_server, addresses=["10.11.0.1/24"])
    assert other["preview"]["warnings"][0].startswith(f"Change #{c['number']} needs attention")
    assert (await admin.post(f"/api/v1/changes/{other['id']}/cancel")).status_code == 200
    # the console visit found it back on its old configuration
    r = await admin.post(f"/api/v1/changes/{c['id']}/recheck")
    assert r.status_code == 200 and r.json()["status"] == "rolled_back", r.text
    assert (await admin.post(f"/api/v1/changes/{c['id']}/recheck")).status_code == 409


async def test_drift_since_the_preview_changes_nothing(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    fake.apply_error = helper.HelperRefused(
        "/etc/netplan/50-lab.yaml changed after the change was prepared. Nothing was changed; prepare the change again.",
        code="drift",
    )
    done = await approve_and_run(admin, runner, await prepare(admin, ready_server))
    assert done["status"] == "failed" and "changed after the change was prepared" in done["outcome"]
    assert fake.commands()[-1] == Cmd.HELPER_APPLY


async def test_a_connection_dropped_during_apply_is_resolved_by_asking_the_server(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    fake.drop_during_apply = True
    done = await approve_and_run(admin, runner, await prepare(admin, ready_server))
    assert done["status"] == "succeeded", done
    assert any("connection dropped while the change was being applied" in e["message"] for e in done["log"])


async def test_unreachable_before_apply_fails_without_changing_anything(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    c = await prepare(admin, ready_server)
    fake.reachable = False
    done = await approve_and_run(admin, runner, c)
    assert done["status"] == "failed" and done["outcome"].endswith("Nothing was changed.")
    assert Cmd.HELPER_APPLY not in fake.commands()


async def test_automation_turned_off_after_approval(
    admin: httpx.AsyncClient,
    ready_server: str,
    fake: FakeServer,
    runner: ChangeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = await prepare(admin, ready_server)
    assert (
        await admin.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": "web-01"})
    ).status_code == 200
    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    await runner.run_once()
    done = (await admin.get(f"/api/v1/changes/{c['id']}")).json()
    assert done["status"] == "failed" and "turned off" in done["outcome"]
    assert Cmd.HELPER_APPLY not in fake.commands()


async def test_second_administrator_rule(
    client: httpx.AsyncClient, ready_server: str, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    monkeypatch.setattr(get_settings(), "changes_require_second_admin", True)
    c = await prepare(client, ready_server)
    assert (
        c["can_approve"] is False
        and c["approve_blocker"] == "Another administrator has to approve changes you prepared."
    )
    r = await client.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": "web-01"})
    assert r.status_code == 403
    async with database.sessionmaker() as db:
        await create_user(db, "admin2", "second-admin-pw-123", Role.ADMIN)
        await db.commit()
    await login(client, "admin2", "second-admin-pw-123")
    r = await client.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": "web-01"})
    assert r.status_code == 200 and r.json()["approved_by"] == "admin2"


async def test_cancel_and_server_removal_rules(
    admin: httpx.AsyncClient, ready_server: str, database: Database
) -> None:
    c = await prepare(admin, ready_server)
    r = await admin.put(f"/api/v1/servers/{ready_server}/changes/enabled", json={"enabled": False})
    assert r.status_code == 409  # not while a change is open
    assert (
        await admin.post(f"/api/v1/changes/{c['id']}/approve", json={"confirm_name": "web-01"})
    ).status_code == 200
    async with database.sessionmaker() as db:
        row = await db.get(ServerChange, c["id"])
        assert row is not None
        row.status = ChangeStatus.RUNNING
        await db.commit()
    r = await admin.delete(f"/api/v1/servers/{ready_server}")
    assert r.status_code == 409 and f"#{c['number']}" in r.json()["error"]["message"]
    assert (await admin.post(f"/api/v1/changes/{c['id']}/cancel")).status_code == 409


async def test_a_change_left_running_by_a_stopped_runner_is_reconciled(
    admin: httpx.AsyncClient,
    ready_server: str,
    fake: FakeServer,
    runner: ChangeRunner,
    clock: FakeClock,
    database: Database,
) -> None:
    c = await prepare(admin, ready_server)
    cid = c["id"].replace("-", "")
    fake.states[cid] = {"state": "confirmed", "reason": None}
    async with database.sessionmaker() as db:
        row = await db.get(ServerChange, c["id"])
        assert row is not None
        row.status = ChangeStatus.VERIFYING
        row.started_at = row.heartbeat_at = clock.now() - timedelta(minutes=10)
        row.deadline_at = clock.now() - timedelta(minutes=8)
        await db.commit()
    await runner.reconcile_stale()
    done = (await admin.get(f"/api/v1/changes/{c['id']}")).json()
    assert done["status"] == "succeeded" and "reports the change as confirmed" in done["outcome"]
    assert any("Lanvex stopped while this change was in progress" in e["message"] for e in done["log"])


async def test_list_changes(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer, runner: ChangeRunner
) -> None:
    c = await prepare(admin, ready_server)
    await approve_and_run(admin, runner, c)
    c2 = await prepare(admin, ready_server, addresses=["10.11.0.1/24"])
    rows = (await admin.get("/api/v1/changes")).json()
    assert [r["number"] for r in rows] == [c2["number"], c["number"]]
    assert [r["status"] for r in (await admin.get("/api/v1/changes?status=succeeded")).json()] == [
        "succeeded"
    ]
    assert len((await admin.get(f"/api/v1/changes?server_id={ready_server}")).json()) == 2
    open_rows = (await admin.get("/api/v1/changes?open=true")).json()
    assert [r["number"] for r in open_rows] == [c2["number"]]


async def test_dns_can_be_left_unchanged(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer
) -> None:
    body = {k: v for k, v in BR0_SECONDARY.items() if not k.startswith("dns")}
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["summary"] == "br0: static 10.11.0.1/24, 10.11.0.2/24 · no gateway · DNS unchanged"
    plan = next(p for cmd, p in fake.calls if cmd == Cmd.HELPER_PLAN)
    assert plan is not None and plan["dns"] is None


# ---- connections through an address that is not the server's own -------------------------------------
# Lanvex in a container on the server itself (rootless Podman: host.containers.internal = 169.254.1.2,
# forwarded to the server's loopback), port forwarding or NAT: signing in again after a change proves
# nothing about the server's network.

FORWARDED = "169.254.1.2"


async def forwarded_server(admin: httpx.AsyncClient) -> str:
    r = await admin.post("/api/v1/servers", json=server_body(address=FORWARDED, port=22))
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).status_code == 200
    assert (await admin.post(f"/api/v1/servers/{sid}/changes/helper/check")).status_code == 200
    assert (
        await admin.put(f"/api/v1/servers/{sid}/changes/enabled", json={"enabled": True})
    ).status_code == 200
    return sid


async def test_an_indirect_connection_needs_a_verification_address(
    admin: httpx.AsyncClient, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await forwarded_server(admin)
    r = (await admin.get(f"/api/v1/servers/{sid}/changes/readiness")).json()
    assert r["ready"] is False and r["connection"]["interfaces"] == []
    assert any("not an address of any of its interfaces" in b for b in r["blockers"])
    assert (await admin.post(f"/api/v1/servers/{sid}/changes", json=BR0_SECONDARY)).status_code == 409

    await admin.patch(f"/api/v1/servers/{sid}", json={"verify_address": "10.99.0.1"})
    r = (await admin.get(f"/api/v1/servers/{sid}/changes/readiness")).json()
    assert r["ready"] is False and any("10.99.0.1 is not an address of" in b for b in r["blockers"])

    s = (await admin.patch(f"/api/v1/servers/{sid}", json={"verify_address": MGMT})).json()
    assert s["verify_address"] == MGMT
    r = (await admin.get(f"/api/v1/servers/{sid}/changes/readiness")).json()
    assert r["ready"] is True, r["blockers"]
    assert r["connection"] == {
        "address": FORWARDED,
        "interfaces": [],
        "verify_address": MGMT,
        "verify_interfaces": ["eth0"],
    }
    # The verification address is protected like the address Lanvex connects to.
    r = await admin.post(
        f"/api/v1/servers/{sid}/changes",
        json=BR0_SECONDARY | {"interface": "eth0", "addresses": ["172.30.0.12/24"], "gateway": "172.30.0.1"},
    )
    assert r.status_code == 422 and "172.30.0.11" in r.json()["error"]["message"]

    done = await approve_and_run(admin, runner, await prepare(admin, sid))
    assert done["status"] == "succeeded"
    assert [(c["id"], c["ok"]) for c in done["checks"]][:2] == [("lanvex", True), ("lanvex_direct", True)]


async def test_a_change_that_cuts_the_server_off_its_network_is_rolled_back(
    admin: httpx.AsyncClient, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await forwarded_server(admin)
    await admin.patch(f"/api/v1/servers/{sid}", json={"verify_address": MGMT})
    change = await prepare(admin, sid)
    fake.down_after_apply = {MGMT}  # the forwarded connection still works; the server's own address does not
    done = await approve_and_run(admin, runner, change)
    assert done["status"] == "rolled_back"
    direct = next(c for c in done["checks"] if c["id"] == "lanvex_direct")
    assert direct["ok"] is False and "No SSH session to 172.30.0.11" in direct["detail"]
    assert Cmd.HELPER_CONFIRM not in fake.commands() and Cmd.HELPER_ROLLBACK in fake.commands()


async def test_unreachable_verification_address_refuses_the_change(
    admin: httpx.AsyncClient, fake: FakeServer, automation: None, runner: ChangeRunner
) -> None:
    sid = await forwarded_server(admin)
    await admin.patch(f"/api/v1/servers/{sid}", json={"verify_address": MGMT})
    fake.down = {MGMT}  # Lanvex in a rootless container cannot reach its own host's LAN address
    r = await admin.post(f"/api/v1/servers/{sid}/changes", json=BR0_SECONDARY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cannot_verify"
    assert "run Lanvex on another machine" in r.json()["error"]["message"]
    assert Cmd.HELPER_PLAN not in fake.commands()


async def test_protected_interfaces_are_never_changed(
    admin: httpx.AsyncClient, ready_server: str, fake: FakeServer
) -> None:
    s = (
        await admin.patch(
            f"/api/v1/servers/{ready_server}", json={"protected_interfaces": ["br*", " ", "vlan-bw"]}
        )
    ).json()
    assert s["protected_interfaces"] == ["br*", "vlan-bw"]
    r = (await admin.get(f"/api/v1/servers/{ready_server}/changes/readiness")).json()
    assert r["protected_interfaces"] == ["br*", "vlan-bw"]
    r = await admin.post(f"/api/v1/servers/{ready_server}/changes", json=BR0_SECONDARY)
    assert r.status_code == 422 and "br0 is protected on web-01 (br*)" in r.json()["error"]["message"]
    assert Cmd.HELPER_PLAN not in fake.commands()
    bad = await admin.patch(f"/api/v1/servers/{ready_server}", json={"protected_interfaces": ["eth0; rm"]})
    assert bad.status_code == 422
