"""changes confirmed in the administrator's browser

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "servers", sa.Column("confirm_in_browser", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "server_changes",
        sa.Column("confirm_in_browser", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "server_changes", sa.Column("browser", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("server_changes", "browser")
    op.drop_column("server_changes", "confirm_in_browser")
    op.drop_column("servers", "confirm_in_browser")
