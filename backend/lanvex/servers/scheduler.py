"""Scheduled reads: every server is read on its interval, read-only, without anyone asking.

Safe with several Lanvex processes (uvicorn workers): a due server is claimed in the database with
SELECT … FOR UPDATE SKIP LOCKED and its next read time moved forward in the same transaction, so
each due read happens once. Reads that find nothing new only confirm the latest snapshot.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from lanvex.core.config import Settings
from lanvex.core.db import Database
from lanvex.core.logging import get_logger
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import Server
from lanvex.servers.service import ServerService, read_interval

log = get_logger("lanvex.servers.scheduler")


class ReadScheduler:
    POLL_SECONDS = 30
    BATCH = 20
    CONCURRENCY = 4

    def __init__(self, db: Database, settings: Settings, netbox: Callable[[], NetBoxClient | None]) -> None:
        self.db = db
        self.settings = settings
        self.netbox = netbox
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lanvex-read-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:  # never let one bad round end the schedule
                log.exception("scheduled_reads_failed")
            await asyncio.sleep(self.POLL_SECONDS)

    async def claim(self, now: datetime | None = None) -> list[uuid.UUID]:
        """Take the servers whose read is due and move their next read forward. Returns their ids."""
        now = now or datetime.now(UTC)
        default = self.settings.server_read_interval_minutes
        if not default:
            return []
        async with self.db.sessionmaker() as session:
            due = (
                await session.scalars(
                    select(Server)
                    .where(
                        func.coalesce(Server.read_interval_minutes, default) > 0,
                        or_(Server.next_read_at.is_(None), Server.next_read_at <= now),
                    )
                    .order_by(Server.next_read_at.asc().nulls_first())
                    .limit(self.BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for s in due:
                s.next_read_at = now + timedelta(minutes=read_interval(s, self.settings))
            await session.commit()
            return [s.id for s in due]

    async def read(self, server_id: uuid.UUID) -> None:
        async with self.db.sessionmaker() as session:
            svc = ServerService(session, self.settings, self.netbox())
            try:
                s = await svc.get(server_id)
                snap = await svc.collect(s, None, scheduled=True)
                await session.commit()
                log.info("scheduled_read", server=s.name, status=snap.status.value)
            except Exception:
                await session.rollback()
                log.exception("scheduled_read_failed", server_id=str(server_id))

    async def run_due(self, now: datetime | None = None) -> int:
        ids = await self.claim(now)
        if ids:
            limit = asyncio.Semaphore(self.CONCURRENCY)

            async def one(server_id: uuid.UUID) -> None:
                async with limit:
                    await self.read(server_id)

            await asyncio.gather(*(one(i) for i in ids))
        return len(ids)
