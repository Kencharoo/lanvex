"""switch changes: port VLANs through the change pipeline

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "switches", sa.Column("changes_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("switches", sa.Column("write_credential_ciphertext", sa.Text()))
    op.add_column("switches", sa.Column("write_username", sa.String(64)))
    op.create_table(
        "switch_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Numbered with the server changes: "change #n" is one change, whatever it changes.
        sa.Column(
            "number",
            sa.Integer(),
            server_default=sa.text("nextval('server_changes_number_seq')"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "switch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("switches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("switch_name", sa.String(100), nullable=False),
        sa.Column("if_index", sa.Integer(), nullable=False),
        sa.Column("port", sa.String(64), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("before", JSONB, nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("warnings", JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "requested_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("requested_by_name", sa.String(64), nullable=False),
        sa.Column(
            "approved_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("approved_by_name", sa.String(64)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by_name", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("log", JSONB, nullable=False),
        sa.Column("outcome", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_switch_changes_switch_created", "switch_changes", ["switch_id", "created_at"])
    op.create_index("ix_switch_changes_status", "switch_changes", ["status"])
    op.create_index(
        "uq_switch_changes_one_open",
        "switch_changes",
        ["switch_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_approval', 'approved', 'running', 'verifying')"),
    )


def downgrade() -> None:
    op.drop_index("uq_switch_changes_one_open", table_name="switch_changes")
    op.drop_index("ix_switch_changes_status", table_name="switch_changes")
    op.drop_index("ix_switch_changes_switch_created", table_name="switch_changes")
    op.drop_table("switch_changes")
    op.drop_column("switches", "write_username")
    op.drop_column("switches", "write_credential_ciphertext")
    op.drop_column("switches", "changes_enabled")
