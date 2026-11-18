"""Scheduled switch reads, read-only, the same way as servers' (servers/scheduler.py): a due switch is
claimed with SELECT … FOR UPDATE SKIP LOCKED, so several Lanvex processes read it once."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from lanvex.core.config import Settings
from lanvex.core.db import Database
from lanvex.core.logging import get_logger
from lanvex.monitoring.service import check_after_switch_read
from lanvex.netbox.client import NetBoxClient
from lanvex.switches.models import Switch
from lanvex.switches.service import SwitchService, read_interval

log = get_logger("lanvex.switches.scheduler")


class SwitchReadScheduler:
    POLL_SECONDS = 30
    BATCH = 20
    CONCURRENCY = 4

    def __init__(self, db: Database, settings: Settings, netbox: Callable[[], NetBoxClient | None]) -> None:
        self.db = db
        self.settings = settings
        self.netbox = netbox
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lanvex-switch-reads")

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
                log.exception("scheduled_switch_reads_failed")
            await asyncio.sleep(self.POLL_SECONDS)

    async def claim(self, now: datetime | None = None) -> list[uuid.UUID]:
        now = now or datetime.now(UTC)
        default = self.settings.switch_read_interval_minutes
        if not default:
            return []
        async with self.db.sessionmaker() as session:
            due = (
                await session.scalars(
                    select(Switch)
                    .where(
                        func.coalesce(Switch.read_interval_minutes, default) > 0,
                        or_(Switch.next_read_at.is_(None), Switch.next_read_at <= now),
                    )
                    .order_by(Switch.next_read_at.asc().nulls_first())
                    .limit(self.BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for sw in due:
                sw.next_read_at = now + timedelta(minutes=read_interval(sw, self.settings))
            await session.commit()
            return [sw.id for sw in due]

    async def read(self, switch_id: uuid.UUID) -> None:
        async with self.db.sessionmaker() as session:
            svc = SwitchService(session, self.settings, self.netbox())
            try:
                sw = await svc.get(switch_id)
                facts = await svc.read(sw)
                await check_after_switch_read(session, self.settings, self.netbox(), sw, facts, svc.moves)
                await session.commit()
                log.info("scheduled_switch_read", switch=sw.name, status=sw.last_status)
            except Exception:
                await session.rollback()
                log.exception("scheduled_switch_read_failed", switch_id=str(switch_id))

    async def run_due(self, now: datetime | None = None) -> int:
        ids = await self.claim(now)
        if ids:
            limit = asyncio.Semaphore(self.CONCURRENCY)

            async def one(switch_id: uuid.UUID) -> None:
                async with limit:
                    await self.read(switch_id)

            await asyncio.gather(*(one(i) for i in ids))
        return len(ids)
