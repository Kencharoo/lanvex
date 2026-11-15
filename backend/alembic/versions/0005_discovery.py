"""discovered addresses

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovered_addresses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("family", sa.Integer(), nullable=False),
        sa.Column("interface", sa.String(length=64), nullable=False),
        sa.Column("mac", sa.String(length=64), nullable=True),
        sa.Column("router", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "address", name="uq_discovered_addresses_server_address"),
    )
    op.create_index("ix_discovered_addresses_address", "discovered_addresses", ["address"])


def downgrade() -> None:
    op.drop_index("ix_discovered_addresses_address", table_name="discovered_addresses")
    op.drop_table("discovered_addresses")
