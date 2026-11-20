"""users who sign in with a directory (LDAP, Active Directory) or single sign-on (OpenID Connect)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-20 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_source", sa.String(16), server_default="local", nullable=False))
    op.add_column("users", sa.Column("external_id", sa.String(512), nullable=True))
    op.create_index("uq_users_external", "users", ["auth_source", "external_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_external", table_name="users")
    op.drop_column("users", "external_id")
    op.drop_column("users", "auth_source")
