"""switches read over SNMP, their latest read, MAC addresses seen; alerts about switches

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "switches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("snmp_version", sa.String(4), nullable=False),
        sa.Column("snmp_username", sa.String(64)),
        sa.Column("credential_ciphertext", sa.Text(), nullable=False),
        sa.Column("netbox_device_id", sa.Integer()),
        sa.Column("read_interval_minutes", sa.Integer()),
        sa.Column("next_read_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(32)),
        sa.Column("last_status_message", sa.String(500)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_read_at", sa.DateTime(timezone=True)),
        sa.Column("sys_name", sa.String(255)),
        sa.Column("sys_description", sa.String(500)),
        sa.Column("ports_total", sa.Integer()),
        sa.Column("ports_up", sa.Integer()),
        sa.Column("mac_count", sa.Integer()),
        sa.Column("probe_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_probe_error", sa.String(300)),
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_switches_name_lower", "switches", [sa.text("lower(name)")], unique=True)
    op.create_table(
        "switch_facts",
        sa.Column(
            "switch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("switches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_table(
        "switch_macs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "switch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("switches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mac", sa.String(17), nullable=False),
        sa.Column("vlan", sa.Integer(), nullable=False),
        sa.Column("if_index", sa.Integer(), nullable=False),
        sa.Column("port", sa.String(64), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("switch_id", "mac", "vlan", name="uq_switch_macs_switch_mac_vlan"),
    )
    op.create_index("ix_switch_macs_mac", "switch_macs", ["mac"])

    op.add_column(
        "alerts",
        sa.Column(
            "switch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("switches.id", ondelete="CASCADE")
        ),
    )
    op.create_index(
        "uq_alerts_open_switch",
        "alerts",
        ["kind", "switch_id", "key"],
        unique=True,
        postgresql_where=sa.text("state = 'open' AND switch_id IS NOT NULL"),
    )
    op.create_index("ix_alerts_switch_kind_key", "alerts", ["switch_id", "kind", "key"])


def downgrade() -> None:
    op.drop_index("ix_alerts_switch_kind_key", table_name="alerts")
    op.drop_index("uq_alerts_open_switch", table_name="alerts")
    op.drop_column("alerts", "switch_id")
    op.drop_index("ix_switch_macs_mac", table_name="switch_macs")
    op.drop_table("switch_macs")
    op.drop_table("switch_facts")
    op.drop_index("uq_switches_name_lower", table_name="switches")
    op.drop_table("switches")
