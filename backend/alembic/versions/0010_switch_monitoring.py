"""switches: first read and watched ports, for port and new-device alerts

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("switches", sa.Column("first_read_at", sa.DateTime(timezone=True)))
    op.add_column(
        "switches",
        sa.Column(
            "watched_ports",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Switches read before: what they have seen so far is their starting point.
    op.execute("UPDATE switches SET first_read_at = last_read_at")


def downgrade() -> None:
    op.drop_column("switches", "watched_ports")
    op.drop_column("switches", "first_read_at")
