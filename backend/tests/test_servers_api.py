"""Server management API: validation, authorization and secret handling. No SSH server needed."""

import asyncssh
import httpx
import pytest
from sqlalchemy import select

from lanvex.audit.models import AuditEvent
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.servers.models import Server

from .conftest import login

HOST_KEY = asyncssh.generate_private_key("ssh-ed25519")
HK = {
    "public_key": HOST_KEY.export_public_key("openssh").decode().strip(),
    "fingerprint_sha256": HOST_KEY.get_fingerprint("sha256"),
}


def body(**over: object) -> dict:
    b = {
        "name": "web-01",
        "address": "127.0.0.1",
        "port": 1,
        "username": "lanvex",
        "host_key": HK,
        "credentials": {"method": "generated_key"},
    }
    b.update(over)
    return b


async def test_create_with_generated_key_never_exposes_secrets(
    admin: httpx.AsyncClient, database: Database
) -> None:
    r = await admin.post("/api/v1/servers", json=body())
    assert r.status_code == 201, r.text
    s = r.json()["server"]
    assert s["auth_method"] == "generated_key" and s["public_key"].startswith("ssh-ed25519 ")
    assert s["authorized_keys_line"] == f"restrict {s['public_key']}"
    assert s["host_key_fingerprint"] == HK["fingerprint_sha256"] and s["host_key_algorithm"] == "ssh-ed25519"
    assert "read.interfaces" in s["capabilities"] and not any(
        c.startswith("write") for c in s["capabilities"]
    )
    assert "authorized_keys" in r.json()["next_step"]
    for text in (
        r.text,
        (await admin.get(f"/api/v1/servers/{s['id']}")).text,
        (await admin.get("/api/v1/servers")).text,
    ):
        assert "PRIVATE KEY" not in text and "ciphertext" not in text and "gAAAA" not in text
    async with database.sessionmaker() as db:
        row = await db.scalar(select(Server))
        assert row is not None and "PRIVATE" not in row.credential_ciphertext
        ev = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "server.create"))).one()
        assert ev.outcome == "success" and "PRIVATE" not in str(ev.detail)


async def test_password_and_uploaded_key(admin: httpx.AsyncClient) -> None:
    r = await admin.post(
        "/api/v1/servers", json=body(name="pw", credentials={"method": "password", "password": "s3cret-pass"})
    )
    assert r.status_code == 201 and r.json()["server"]["public_key"] is None and "s3cret" not in r.text

    k = asyncssh.generate_private_key("ssh-ed25519")
    enc = k.export_private_key("openssh", passphrase="pp-12345").decode()
    r = await admin.post(
        "/api/v1/servers", json=body(name="k1", credentials={"method": "private_key", "private_key": enc})
    )
    assert r.status_code == 422 and "passphrase" in r.json()["error"]["fields"]
    r = await admin.post(
        "/api/v1/servers",
        json=body(
            name="k1", credentials={"method": "private_key", "private_key": enc, "passphrase": "wrong"}
        ),
    )
    assert r.status_code == 422 and r.json()["error"]["fields"]["passphrase"] == ["Incorrect passphrase."]
    r = await admin.post(
        "/api/v1/servers",
        json=body(
            name="k1", credentials={"method": "private_key", "private_key": enc, "passphrase": "pp-12345"}
        ),
    )
    assert r.status_code == 201
    assert r.json()["server"]["public_key"] == k.export_public_key("openssh").decode().strip()
    r = await admin.post(
        "/api/v1/servers",
        json=body(name="k2", credentials={"method": "private_key", "private_key": "not a key"}),
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "over,field",
    [
        ({"host_key": {**HK, "fingerprint_sha256": "SHA256:somethingelse"}}, "host_key"),
        ({"host_key": {**HK, "public_key": "ssh-ed25519 AAAAnotakey"}}, "host_key"),
        ({"username": "root; rm -rf /"}, "username"),
        ({"address": "10.0.0.1; reboot"}, "address"),
        ({"address": "-oProxyCommand=sh"}, "address"),
        ({"port": 70000}, "port"),
        ({"credentials": {"method": "password"}}, "credentials"),
    ],
)
async def test_validation(admin: httpx.AsyncClient, over: dict, field: str) -> None:
    r = await admin.post("/api/v1/servers", json=body(**over))
    assert r.status_code == 422, r.text
    assert any(k.startswith(field) for k in r.json()["error"]["fields"]), r.json()


async def test_duplicate_name(admin: httpx.AsyncClient) -> None:
    assert (await admin.post("/api/v1/servers", json=body())).status_code == 201
    assert (await admin.post("/api/v1/servers", json=body(name="WEB-01"))).status_code == 409


async def test_readonly_can_view_but_not_connect(admin: httpx.AsyncClient, users: dict[str, str]) -> None:
    sid = (await admin.post("/api/v1/servers", json=body())).json()["server"]["id"]
    reader = admin
    await login(reader, "reader", users["reader"])
    assert (await reader.get(f"/api/v1/servers/{sid}")).status_code == 200
    for method, path in (
        ("post", f"/api/v1/servers/{sid}/test"),
        ("post", f"/api/v1/servers/{sid}/collect"),
        ("post", "/api/v1/servers/scan-host-key"),
        ("delete", f"/api/v1/servers/{sid}"),
        ("put", f"/api/v1/servers/{sid}/credentials"),
    ):
        r = await getattr(reader, method)(
            path,
            **({"json": {"address": "127.0.0.1", "method": "generated_key"}} if method != "delete" else {}),
        )
        assert r.status_code == 403, (path, r.text)


async def test_unreachable_server_is_recorded(admin: httpx.AsyncClient) -> None:
    sid = (await admin.post("/api/v1/servers", json=body())).json()["server"]["id"]
    r = await admin.post(f"/api/v1/servers/{sid}/test")
    assert r.status_code == 200 and r.json()["status"] == "unreachable"
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert s["last_status"] == "unreachable" and s["last_checked_at"]
    snap = (await admin.post(f"/api/v1/servers/{sid}/collect")).json()
    assert snap["status"] == "unreachable" and snap["facts"] is None
    assert len((await admin.get(f"/api/v1/servers/{sid}/snapshots")).json()) == 1
    scan = await admin.post("/api/v1/servers/scan-host-key", json={"address": "127.0.0.1", "port": 1})
    assert scan.status_code == 502 and scan.json()["error"]["code"] == "unreachable"


async def test_allowed_networks_are_enforced(admin: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "server_allowed_networks", ["10.0.0.0/8"])
    r = await admin.post("/api/v1/servers/scan-host-key", json={"address": "127.0.0.1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed"
    r = await admin.post("/api/v1/servers", json=body())
    assert r.status_code == 403


async def test_moving_a_server_requires_a_new_host_key(admin: httpx.AsyncClient) -> None:
    sid = (await admin.post("/api/v1/servers", json=body())).json()["server"]["id"]
    r = await admin.patch(f"/api/v1/servers/{sid}", json={"address": "127.0.0.2"})
    assert r.status_code == 422 and "host_key" in r.json()["error"]["fields"]
    r = await admin.patch(
        f"/api/v1/servers/{sid}", json={"address": "127.0.0.2", "host_key": HK, "description": "moved"}
    )
    assert r.status_code == 200 and r.json()["address"] == "127.0.0.2"


async def test_credential_storage_requires_secret_key(admin: httpx.AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "secret_key", None)
    r = await admin.post("/api/v1/servers", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "secret_key_not_configured"


async def test_delete(admin: httpx.AsyncClient) -> None:
    sid = (await admin.post("/api/v1/servers", json=body())).json()["server"]["id"]
    await admin.post(f"/api/v1/servers/{sid}/collect")
    assert (await admin.delete(f"/api/v1/servers/{sid}")).status_code == 204
    assert (await admin.get(f"/api/v1/servers/{sid}")).status_code == 404
