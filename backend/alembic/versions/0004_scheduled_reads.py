"""scheduled reads and server history

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15 22:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("read_interval_minutes", sa.Integer(), nullable=True))
    op.add_column("servers", sa.Column("next_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("server_snapshots", sa.Column("fingerprint", sa.String(length=64), nullable=True))
    op.add_column("server_snapshots", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "server_snapshots", sa.Column("reads", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("server_snapshots", "reads")
    op.drop_column("server_snapshots", "confirmed_at")
    op.drop_column("server_snapshots", "fingerprint")
    op.drop_column("servers", "next_read_at")
    op.drop_column("servers", "read_interval_minutes")
