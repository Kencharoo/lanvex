"""Scheduled reads stop by themselves when every attempt makes things worse: repeated failed sign-ins,
or OpenSSH refusing Lanvex's address after them (PerSourcePenalties)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncssh
import httpx
import pytest

from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.integrations.linux import transport
from lanvex.servers.scheduler import ReadScheduler

from . import test_server_history
from .test_server_history import FakeServer
from .test_servers_api import body

fake = test_server_history.fake


async def due_read(database: Database, when: datetime) -> list[uuid.UUID]:
    scheduler = ReadScheduler(database, get_settings(), lambda: None)
    ids = await scheduler.claim(when)
    for sid in ids:
        await scheduler.read(sid)
    return ids


async def test_reads_pause_after_repeated_failed_sign_ins_and_resume_after_a_good_read(
    admin: httpx.AsyncClient, database: Database, fake: FakeServer
) -> None:
    sid = (await admin.post("/api/v1/servers", json=body(address="192.0.2.10"))).json()["server"]["id"]
    fake.error = transport.SSHError(
        "The server rejected the credentials for user 'lanvex'.", code="auth_failed"
    )
    t = datetime.now(UTC)
    assert await due_read(database, t + timedelta(hours=2)) == [uuid.UUID(sid)]
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert s["reads_paused"] is None and s["last_status"] == "auth_failed"  # one failure may be a fluke
    assert await due_read(database, t + timedelta(hours=4)) == [uuid.UUID(sid)]
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert s["reads_paused"].startswith("The server rejected Lanvex's credentials 2 times in a row.")
    assert s["next_read_at"] is None
    assert await due_read(database, t + timedelta(days=3)) == []  # nothing more is tried
    assert fake.reads == 2

    fake.error = None  # credentials fixed; one read by hand
    assert (await admin.post(f"/api/v1/servers/{sid}/collect")).json()["status"] == "ok"
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert s["reads_paused"] is None and s["next_read_at"] is not None


async def test_a_refused_address_pauses_reads_at_once(
    admin: httpx.AsyncClient, database: Database, fake: FakeServer
) -> None:
    sid = (await admin.post("/api/v1/servers", json=body(address="192.0.2.10"))).json()["server"]["id"]
    fake.error = transport.SSHError(transport.PENALIZED, code="refused")
    await due_read(database, datetime.now(UTC) + timedelta(hours=2))
    s = (await admin.get(f"/api/v1/servers/{sid}")).json()
    assert s["last_status"] == "refused" and "PerSourcePenalties" in s["reads_paused"]
    # A read by hand that fails the same way does not end the pause.
    await admin.post(f"/api/v1/servers/{sid}/collect")
    assert (await admin.get(f"/api/v1/servers/{sid}")).json()["reads_paused"] is not None


@pytest.fixture
async def penalizing_sshd() -> tuple[str, int]:
    """Answers like OpenSSH 9.8+ does to a penalized address: one line, then it closes."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"Not allowed at this time\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield "127.0.0.1", port
    server.close()


async def test_openssh_penalty_is_recognised(penalizing_sshd: tuple[str, int]) -> None:
    address, port = penalizing_sshd
    key = asyncssh.generate_private_key("ssh-ed25519")
    target = transport.Target(
        address=address,
        port=port,
        username="lanvex",
        host_key=key.export_public_key().decode(),
        private_key=asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode(),
    )
    with pytest.raises(transport.SSHError) as e:
        async with transport.connect(target, 3):
            pass
    assert e.value.code == "refused" and "Not allowed at this time" in e.value.message
