"""Change requests for server configuration, from preparation to their verified outcome."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Identity, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from lanvex.core.db import Base, TimestampMixin


class ChangeStatus(enum.StrEnum):
    PENDING_APPROVAL = "pending_approval"  # prepared and previewed; nothing changed yet
    APPROVED = "approved"  # waiting for the runner
    RUNNING = "running"  # being applied
    VERIFYING = "verifying"  # applied; the server rolls back by itself unless Lanvex confirms
    SUCCEEDED = "succeeded"  # applied, verified and confirmed: the new configuration is permanent
    ROLLED_BACK = "rolled_back"  # applied, then undone; the previous configuration is back
    FAILED = "failed"  # stopped before anything was changed
    CANCELLED = "cancelled"  # withdrawn before it ran
    NEEDS_ATTENTION = "needs_attention"  # Lanvex cannot tell what state the server is in


OPEN_STATUSES = (
    ChangeStatus.PENDING_APPROVAL,
    ChangeStatus.APPROVED,
    ChangeStatus.RUNNING,
    ChangeStatus.VERIFYING,
)
IN_PROGRESS = (ChangeStatus.RUNNING, ChangeStatus.VERIFYING)


class ServerChange(TimestampMixin, Base):
    __tablename__ = "server_changes"
    __table_args__ = (
        Index("ix_server_changes_server_created", "server_id", "created_at"),
        Index("ix_server_changes_status", "status"),
        # At most one change per server between preparation and its outcome.
        Index(
            "uq_server_changes_one_open",
            "server_id",
            unique=True,
            postgresql_where=text("status IN ('pending_approval', 'approved', 'running', 'verifying')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    number: Mapped[int] = mapped_column(Integer, Identity(), unique=True, nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    server_name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="interface_ipv4", nullable=False)
    interface: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)

    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # validated desired settings
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the interface as last read
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the helper's plan: files, commands

    status: Mapped[ChangeStatus] = mapped_column(
        Enum(ChangeStatus, name="server_change_status", values_callable=lambda x: [m.value for m in x]),
        default=ChangeStatus.PENDING_APPROVAL,
        nullable=False,
    )
    rollback_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    requested_by_name: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by_name: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_name: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runner: Mapped[str | None] = mapped_column(String(100))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # server-side rollback

    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(1000))
