from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from lanvex.audit.models import AuditEvent
from lanvex.auth.models import Session
from lanvex.core.db import Database
from lanvex.core.errors import Conflict

from .conftest import ADMIN_PW, login


async def test_health_needs_no_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/health/live")).json()["status"] == "ok"
    ready = await client.get("/api/v1/health/ready")
    assert ready.status_code == 200 and ready.json()["database"] is True


async def test_protected_endpoint_requires_session(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/prefixes")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_authenticated"


async def test_login_sets_hardened_cookie(client: httpx.AsyncClient, users: dict[str, str]) -> None:
    r = await client.post("/api/v1/auth/login", json={"username": "ADMIN", "password": ADMIN_PW})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    body = r.json()
    assert body["user"]["role"] == "admin" and body["csrf_token"]
    assert "password_hash" not in r.text


async def test_wrong_password_is_rejected_and_audited(
    client: httpx.AsyncClient, users: dict[str, str], database: Database
) -> None:
    r = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "nope-nope-nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r2 = await client.post("/api/v1/auth/login", json={"username": "ghost", "password": "nope-nope-nope"})
    assert r2.json()["error"]["message"] == r.json()["error"]["message"]  # no username enumeration
    async with database.sessionmaker() as s:
        outcomes = (
            await s.scalars(select(AuditEvent.outcome).where(AuditEvent.action == "auth.login"))
        ).all()
    assert outcomes == ["denied", "denied"]


async def test_login_is_rate_limited(client: httpx.AsyncClient, users: dict[str, str]) -> None:
    codes = []
    for _ in range(12):
        r = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
        codes.append(r.status_code)
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_csrf_required_for_writes(client: httpx.AsyncClient, users: dict[str, str]) -> None:
    await login(client, "admin", users["admin"])
    token = client.headers.pop("X-CSRF-Token")
    r = await client.post("/api/v1/prefixes/validate", json={"prefix": "10.0.0.0/24"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    client.headers["X-CSRF-Token"] = token + "x"
    r = await client.post("/api/v1/prefixes/validate", json={"prefix": "10.0.0.0/24"})
    assert r.status_code == 403


async def test_readonly_cannot_write(reader: httpx.AsyncClient) -> None:
    r = await reader.post("/api/v1/prefixes", json={"prefix": "10.0.0.0/24"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    r = await reader.get("/api/v1/users")
    assert r.status_code == 403


async def test_logout_revokes_session(admin: httpx.AsyncClient) -> None:
    assert (await admin.post("/api/v1/auth/logout")).status_code == 204
    assert (await admin.get("/api/v1/auth/session")).status_code == 401


async def test_idle_session_expires(admin: httpx.AsyncClient, database: Database) -> None:
    async with database.sessionmaker() as s:
        await s.execute(update(Session).values(last_seen_at=Session.last_seen_at - timedelta(hours=3)))
        await s.commit()
    r = await admin.get("/api/v1/auth/session")
    assert r.status_code == 401


async def test_user_admin_and_last_admin_guard(admin: httpx.AsyncClient) -> None:
    r = await admin.post("/api/v1/users", json={"username": "ops", "password": "short", "role": "readonly"})
    assert r.status_code == 422 and "password" in r.json()["error"]["fields"]
    r = await admin.post(
        "/api/v1/users", json={"username": "ops", "password": "long-enough-password", "role": "admin"}
    )
    assert r.status_code == 201
    r = await admin.post("/api/v1/users", json={"username": "OPS", "password": "long-enough-password"})
    assert r.status_code == 409

    users = {u["username"]: u for u in (await admin.get("/api/v1/users")).json()}
    # Demote the second admin: allowed. Then demoting the last one must fail.
    assert (
        await admin.patch(f"/api/v1/users/{users['ops']['id']}", json={"role": "readonly"})
    ).status_code == 200
    r = await admin.patch(f"/api/v1/users/{users['admin']['id']}", json={"role": "readonly"})
    assert r.status_code == 409


async def test_netbox_not_configured(admin: httpx.AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.netbox = None
    r = await admin.get("/api/v1/prefixes")
    assert r.status_code == 503 and r.json()["error"]["code"] == "netbox_not_configured"
    s = (await admin.get("/api/v1/system/netbox")).json()
    assert s["configured"] is False


async def test_secure_cookie_mode_refuses_plain_http_with_guidance(
    app,
    users: dict[str, str],
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    from lanvex.core.config import get_settings

    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://lanvex.example.net"
    ) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 422 and r.json()["error"]["code"] == "insecure_transport"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://lanvex.example.net"
    ) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


async def test_users_can_be_deleted_but_not_by_themselves(admin: httpx.AsyncClient, database) -> None:  # type: ignore[no-untyped-def]
    from lanvex.auth.service import retire_user

    r = await admin.post(
        "/api/v1/users", json={"username": "claude-tmp", "password": "long-enough-password", "role": "admin"}
    )
    tmp = r.json()
    me = next(u for u in (await admin.get("/api/v1/users")).json() if u["username"] == "admin")
    r = await admin.delete(f"/api/v1/users/{me['id']}")
    assert r.status_code == 409 and "lanvex delete-user" in r.text
    assert (await admin.delete(f"/api/v1/users/{tmp['id']}")).status_code == 204
    assert "claude-tmp" not in {u["username"] for u in (await admin.get("/api/v1/users")).json()}
    event = (await admin.get("/api/v1/audit-events", params={"action": "user.delete"})).json()["results"][0]
    assert event["target_repr"] == "claude-tmp" and event["actor_username"] == "admin"

    # What `lanvex deactivate-user` / `delete-user` run: never the last active administrator.
    async with database.sessionmaker() as s:
        with pytest.raises(Conflict):
            await retire_user(s, "ADMIN", delete_it=False)
        reader = await retire_user(s, "reader", delete_it=False)
        assert reader.is_active is False
        await s.commit()
    r = await admin.post(
        "/api/v1/users", json={"username": "ops2", "password": "long-enough-password", "role": "admin"}
    )
    async with database.sessionmaker() as s:
        await retire_user(s, "admin", delete_it=False)  # another active admin exists now
        await s.commit()
    assert (await admin.get("/api/v1/users")).status_code == 401  # its sessions ended


def test_cli_retires_users(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from lanvex import cli

    calls: list[tuple[str, bool]] = []

    async def fake(username: str, delete_it: bool) -> None:
        calls.append((username, delete_it))

    monkeypatch.setattr(cli, "_retire_user", fake)
    assert cli.main(["deactivate-user", "claude-tmp"]) == 0
    assert cli.main(["delete-user", "claude-tmp"]) == 0
    assert calls == [("claude-tmp", False), ("claude-tmp", True)]
    assert "Deleted user 'claude-tmp'" in capsys.readouterr().out
