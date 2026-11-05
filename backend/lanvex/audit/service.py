from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.audit.models import AuditEvent
from lanvex.auth.models import User
from lanvex.core.http import client_ip
from lanvex.core.logging import get_logger

log = get_logger("lanvex.audit")

Outcome = Literal["success", "failure", "denied"]


@dataclass
class Tracked:
    target_id: object | None = None
    target_repr: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Auditor:
    """Writes audit events in their own transaction so a failed operation is still recorded."""

    def __init__(self, db: AsyncSession, request: Request, user: User | None):
        self.db = db
        self.request = request
        self.user = user

    async def record(
        self,
        action: str,
        outcome: Outcome,
        *,
        target_type: str | None = None,
        target_id: object | None = None,
        target_repr: str | None = None,
        detail: dict[str, Any] | None = None,
        actor: User | None = None,
        _actor_snapshot: tuple[Any, str | None] | None = None,
    ) -> None:
        actor_id, actor_name = _actor_snapshot or self._actor(actor)
        event = AuditEvent(
            actor_id=actor_id,
            actor_username=actor_name,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            target_repr=(target_repr or "")[:256] or None,
            outcome=outcome,
            detail=detail,
            ip_address=client_ip(self.request),
            request_id=getattr(self.request.state, "request_id", None),
        )
        self.db.add(event)
        await self.db.commit()
        await self._log(event, action, outcome, target_type)

    @asynccontextmanager
    async def track(
        self,
        action: str,
        target_type: str,
        *,
        target_id: object | None = None,
        target_repr: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AsyncIterator[Tracked]:
        """Record success or failure of the wrapped operation. Exceptions are re-raised unchanged."""
        t = Tracked(target_id=target_id, target_repr=target_repr, detail=dict(detail or {}))
        snapshot = self._actor(None)  # taken before a rollback can expire the user object
        try:
            yield t
        except Exception as e:
            t.detail["error"] = getattr(e, "message", None) or type(e).__name__
            await self.db.rollback()
            await self.record(
                action,
                "failure",
                target_type=target_type,
                target_id=t.target_id,
                target_repr=t.target_repr,
                detail=t.detail,
                _actor_snapshot=snapshot,
            )
            raise
        await self.record(
            action,
            "success",
            target_type=target_type,
            target_id=t.target_id,
            target_repr=t.target_repr,
            detail=t.detail,
            _actor_snapshot=snapshot,
        )

    def _actor(self, actor: User | None) -> tuple[Any, str | None]:
        who = actor or self.user or getattr(self.request.state, "user", None)
        return (who.id, who.username) if who else (None, None)

    async def _log(self, event: AuditEvent, action: str, outcome: str, target_type: str | None) -> None:
        log.info(
            "audit",
            action=action,
            outcome=outcome,
            actor=event.actor_username,
            target_type=target_type,
            target_id=event.target_id,
        )
