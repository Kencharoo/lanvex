import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from lanvex.audit.models import AuditEvent
from lanvex.deps import DB, CurrentUser
from lanvex.schemas import ApiModel, Page

router = APIRouter(tags=["audit"])


class AuditEventOut(ApiModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_username: str | None
    action: str
    target_type: str | None
    target_id: str | None
    target_repr: str | None
    outcome: str
    detail: dict[str, Any] | None
    ip_address: str | None
    request_id: str | None


@router.get("/audit-events", response_model=Page[AuditEventOut])
async def list_audit_events(
    db: DB,
    _: CurrentUser,
    q: Annotated[str | None, Query(max_length=100)] = None,
    action: str | None = None,
    outcome: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditEventOut]:
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action))
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(
            or_(
                AuditEvent.actor_username.ilike(like),
                AuditEvent.target_repr.ilike(like),
                AuditEvent.action.ilike(like),
            )
        )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.scalars(stmt.order_by(AuditEvent.occurred_at.desc()).limit(limit).offset(offset))
    return Page(
        count=int(total or 0),
        limit=limit,
        offset=offset,
        results=[AuditEventOut.model_validate(r) for r in rows],
    )
