"""monitoring: alerts, notifications, webhooks, settings, probes

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "server_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("servers.id", ondelete="CASCADE")
        ),
        sa.Column("server_name", sa.String(100), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(64)),
    )
    op.create_index(
        "uq_alerts_open",
        "alerts",
        ["kind", "server_id", "key"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index("ix_alerts_opened_at", "alerts", ["opened_at"])
    op.create_index("ix_alerts_server_kind_key", "alerts", ["server_id", "kind", "key"])
    op.create_table(
        "alert_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500)),
    )
    op.create_index("ix_alert_notifications_alert_id", "alert_notifications", ["alert_id"])
    op.create_index("ix_alert_notifications_pending", "alert_notifications", ["sent_at", "next_attempt_at"])
    op.create_table(
        "alert_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("url_ciphertext", sa.Text(), nullable=False),
        sa.Column("url_hint", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("min_severity", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "server_probes",
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(300)),
    )
    op.create_table(
        "monitoring_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(64)),
    )


def downgrade() -> None:
    op.drop_table("monitoring_config")
    op.drop_table("server_probes")
    op.drop_table("alert_webhooks")
    op.drop_index("ix_alert_notifications_pending", table_name="alert_notifications")
    op.drop_index("ix_alert_notifications_alert_id", table_name="alert_notifications")
    op.drop_table("alert_notifications")
    op.drop_index("ix_alerts_server_kind_key", table_name="alerts")
    op.drop_index("ix_alerts_opened_at", table_name="alerts")
    op.drop_index("uq_alerts_open", table_name="alerts")
    op.drop_table("alerts")
