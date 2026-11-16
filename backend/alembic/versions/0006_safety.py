"""verification address, protected interfaces, paused reads, refused connections

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE server_connection_status ADD VALUE IF NOT EXISTS 'refused'")
    op.add_column("servers", sa.Column("reads_paused", sa.String(length=500), nullable=True))
    op.add_column("servers", sa.Column("verify_address", sa.String(length=64), nullable=True))
    op.add_column(
        "servers",
        sa.Column(
            "protected_interfaces",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "protected_interfaces")
    op.drop_column("servers", "verify_address")
    op.drop_column("servers", "reads_paused")
    # PostgreSQL cannot drop an enum value: rows that use it become 'error', and the value stays unused.
    op.execute("UPDATE servers SET last_status = 'error' WHERE last_status = 'refused'")
    op.execute("UPDATE server_snapshots SET status = 'error' WHERE status = 'refused'")
