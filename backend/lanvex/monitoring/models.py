"""Monitoring: alerts, their notifications, webhooks, settings and the servers' probe state."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from lanvex.core.db import Base, TimestampMixin, utcnow
from lanvex.switches import models as _switches  # noqa: F401  (alerts.switch_id refers to switches)


class AlertState(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Alert(Base):
    """A condition found on a server or a switch. One open alert per (kind, server or switch, key); it is
    resolved when the condition is gone, or, for events (a failed change, an unknown device), when
    acknowledged."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index(
            "uq_alerts_open",
            "kind",
            "server_id",
            "key",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        Index(
            "uq_alerts_open_switch",
            "kind",
            "switch_id",
            "key",
            unique=True,
            postgresql_where=text("state = 'open' AND switch_id IS NOT NULL"),
        ),
        Index("ix_alerts_opened_at", "opened_at"),
        Index("ix_alerts_server_kind_key", "server_id", "kind", "key"),
        Index("ix_alerts_switch_kind_key", "switch_id", "kind", "key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE")
    )
    switch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("switches.id", ondelete="CASCADE")
    )
    # The server's or switch's name when the alert was opened.
    server_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default=AlertState.OPEN, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))


class AlertNotification(Base):
    """One message about an alert (opened, changed, resolved) to one channel, sent with retries."""

    __tablename__ = "alert_notifications"
    __table_args__ = (Index("ix_alert_notifications_pending", "sent_at", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)  # opened | changed | resolved
    channel: Mapped[str] = mapped_column(String(64), nullable=False)  # email | webhook:<id>
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))


class Webhook(TimestampMixin, Base):
    """Where alerts are posted. The URL usually carries a token, so it is stored encrypted."""

    __tablename__ = "alert_webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    url_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    url_hint: Mapped[str] = mapped_column(String(200), nullable=False)  # scheme and host, safe to show
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    min_severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO, nullable=False)


class ServerProbe(Base):
    """Whether a server's SSH port answers, probed without signing in."""

    __tablename__ = "server_probes"

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True
    )
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))


class MonitoringConfig(Base):
    """The installation's monitoring settings: one row."""

    __tablename__ = "monitoring_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(64))
